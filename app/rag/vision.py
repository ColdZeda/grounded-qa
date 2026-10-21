"""读图：把一张图变成**四个结构化字段**（`scene` / `transcription` / `image_type` / `has_exact_fields`）。

## 为什么是"一次调用、两段输出"

天真做法是"先分类再路由"：文字图走 OCR、情景图走 VL。但那样要**两次模型调用**
（图 token 翻倍），而且分类错了还得回退。这里用一次调用同时产出两类信息：

    · 实拍照片（破损纸箱）→ 主要看 `scene`
    · 文书 / 截图（订单页）→ 主要看 `transcription`

## 为什么模型可换、而且换了不用重嵌入

图**从不进向量空间**（我们的 RAG 是文本库）。所以换读图模型只影响"图 → 文字"这一步，
不影响任何已有向量 —— 这是它和 embedding 模型最大的区别（后者换一次要全量重嵌入）。

## 降级（`PLAN §8.3` D10）

    · 模型 API 挂了 → **抛 `ModelAPIError`**，由调用方明确告诉用户"图片没读成功"
      （绝不静默丢图：那会让用户以为我们看过图了）
    · 返回的不是 JSON → **整段当 `scene`** 并记 `fallback=true`（**不许抛**）
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.orders import extract_order_candidates
from app.rag.prompt import IMAGE_ANALYSIS_PROMPT
from app.rag.providers import ModelAPIError, get_http_client

#: `image_type` 的取值域。模型给了别的值一律归 `other`（不许把脏值原样落库）。
IMAGE_TYPES = ("screenshot", "document", "photo", "handwritten", "chart", "other")

#: 降级时把整段回答当 `scene` 的截断长度。/ 进记忆时 `transcription` 的截断长度。
_MAX_FALLBACK_SCENE = 500
_MAX_MEMORY_CHARS = 240

_HAS_DIGITS = re.compile(r"\d{6,}")


@dataclass(frozen=True)
class ImageAnalysis:
    """一张图的读数。**只有事实，没有业务判断**。"""

    image_type: str
    scene: str
    transcription: str
    has_exact_fields: bool
    model: str
    #: 模型没按 JSON 回、我们把整段当 `scene` 了。
    #: ⚠️ 这个标记必须留着 —— 否则降级是**静默**的（本项目最忌讳的那种坏）。
    fallback: bool = False
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """落到 `images.analysis` 的结构（给排障和评测看，不进提示词）。"""
        return {
            "model": self.model,
            "image_type": self.image_type,
            "scene": self.scene,
            "transcription": self.transcription,
            "has_exact_fields": self.has_exact_fields,
            "fallback": self.fallback,
        }

    @property
    def memory_line(self) -> str:
        """写进 `messages.meta`、供**多轮追问**恢复上下文的那一句话。

        ⚠️ 模型看不到图（历史里只有文字），所以这张图必须在这里变成**一段文字记忆**，
            否则用户追问"那多久到账"时，模型完全不知道刚才有张图。
        ⚠️ 只放**读数**，不放我们的推断（"这是破损件可以退"之类的一律不写）。
        """
        scene = (self.scene or "").strip()[:100]
        parts = [f"用户发来一张图：{scene or '（未描述）'}"]
        text = (self.transcription or "").strip()
        if text:
            clipped = text[:_MAX_MEMORY_CHARS]
            if len(text) > _MAX_MEMORY_CHARS:
                clipped += "…"
            parts.append(f"图中文字：{clipped}")
        return "；".join(parts)


def _coerce_bool(value: Any) -> bool:
    """`true` / `"true"` / `1` 都当 True —— 不同模型的 JSON 习惯不一样。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _looks_like_exact_tokens(text: str) -> bool:
    """降级时**自己判断**"有没有值得查库的东西"（不指望模型的字段了）。"""
    return bool(extract_order_candidates(text)) or bool(_HAS_DIGITS.search(text))


def parse_analysis(
    payload: str, *, model: str, usage: dict[str, int] | None = None
) -> ImageAnalysis:
    """把模型返回的原文解析成 `ImageAnalysis`。**解析失败不抛，走降级。**

    ⚠️ 这里**故意宽容**：模型偶尔会包一层 ```json 代码块，或者加一句"好的，如下："。
        为这种格式问题把一条回答整条丢掉，代价远大于收益 —— 抠出第一个 `{...}` 就够了。
    """
    text = (payload or "").strip()
    body = text
    if "```" in body:
        # 去掉最外层代码块围栏
        body = body.replace("```json", "```")
        if body.count("```") >= 2:
            body = body.split("```")[1]

    parsed: object = None
    try:
        parsed = json.loads(body.strip())
    except (json.JSONDecodeError, ValueError):
        # 退一步：从原文里抠第一个大括号块
        start, end = body.find("{"), body.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(body[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                parsed = None

    if not isinstance(parsed, dict) or not str(parsed.get("scene", "")).strip():
        # 降级：整段当描述。**不抛异常** —— 图读歪了也总比整条问答失败好。
        return ImageAnalysis(
            image_type="other",
            scene=text[:_MAX_FALLBACK_SCENE] or "（模型没有返回内容）",
            transcription="",
            has_exact_fields=_looks_like_exact_tokens(text),
            model=model,
            fallback=True,
            usage=dict(usage or {}),
        )

    image_type = str(parsed.get("image_type", "")).strip().lower()
    if image_type not in IMAGE_TYPES:
        image_type = "other"
    return ImageAnalysis(
        image_type=image_type,
        scene=str(parsed.get("scene", "")).strip(),
        transcription=str(parsed.get("transcription", "") or "").strip(),
        has_exact_fields=_coerce_bool(parsed.get("has_exact_fields")),
        model=model,
        fallback=False,
        usage=dict(usage or {}),
    )


class ImageAnalysisClient:
    """OpenAI 兼容的多模态客户端（**只用 chat/completions + base64 内联图**）。

    ⚠️ 图**必须内联**（`data:image/...;base64,...`）：图在我们自己的卷里，
        模型侧访问不到；为了让它"去下载"而把图暴露到公网，是绝不做的。
    ⚠️ 客户端可注入（`client=`）：测试要能塞 `MockTransport`，否则每个用例都真调模型
        （`AGENTS §7.1` 约定 9）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    def _client_or_default(self) -> httpx.AsyncClient:
        return self._client if self._client is not None else get_http_client()

    async def analyze(self, image: bytes, *, content_type: str) -> ImageAnalysis:
        """调一次模型，拿四个字段。失败抛 `ModelAPIError`（由调用方决定怎么告诉用户）。"""
        data_url = f"data:{content_type};base64,{base64.b64encode(image).decode('ascii')}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            #: 让它直接给 JSON（探针实测 `deepseek-flash` 认这个字段）。
            #: ⚠️ 换模型后如果不认，下面的重试会去掉它再试一次 —— 提示词里同样要求了 JSON。
            "response_format": {"type": "json_object"},
        }
        response = await self._post(payload)
        if response.status_code == 400 and "response_format" in response.text:
            payload.pop("response_format", None)
            response = await self._post(payload)
        if response.status_code >= 400:
            raise ModelAPIError(f"读图 API 返回 {response.status_code}：{response.text[:300]}")

        data = response.json()
        choices = data.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return parse_analysis(content, model=self._model, usage=usage)

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            client = self._client_or_default()
            return await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"读图 API 请求失败：{exc}") from exc


def build_image_client(settings: Settings | None = None) -> ImageAnalysisClient | None:
    """按配置造一个读图客户端；**没配好就返回 `None`**（不是造一个注定失败的）。

    ⚠️ 三个 `image_analysis_*` 都默认回退到 `llm_*`：`deepseek-flash` 本身就是多模态模型，
        同一把 key、同一个端点，**生产 .env 一个字都不用改**（见 `architecture.md` §9.35）。
    """
    current = settings or get_settings()
    if not current.image_analysis_enabled:
        return None
    api_key = current.image_analysis_api_key or current.llm_api_key
    if not api_key:
        return None
    return ImageAnalysisClient(
        base_url=current.image_analysis_base_url or current.llm_base_url,
        api_key=api_key,
        model=current.image_analysis_model or current.llm_model,
        timeout=current.model_request_timeout_seconds,
    )


__all__ = [
    "IMAGE_TYPES",
    "ImageAnalysis",
    "ImageAnalysisClient",
    "build_image_client",
    "parse_analysis",
]
