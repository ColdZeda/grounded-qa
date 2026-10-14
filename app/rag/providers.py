"""三个外部模型 API 的客户端：**对话 / embedding / rerank**。

⚠️ **硬约束：模型全部走 API，不落本地模型**（AGENTS §2.2）。所以这里只有 HTTP，没有权重。

⚠️ **不引入任何厂商 SDK，只用 httpx 拼请求。**
    目的：让"换供应商"退化成**改 `.env`**，而不是改代码。
    代价是每个接口的请求体要自己拼 —— 但只有三个，且都很短。

⚠️ 三个客户端的**形状不一样**，不要试图统一：
    · 对话 / embedding 是 **OpenAI 兼容**（`/chat/completions`、`/embeddings`）
    · rerank **不是** OpenAI 标准的一部分，各家各写各的（这里按阿里云百炼的 DashScope 形状）
    硬凑一个"统一接口"只会把差异藏起来，等换供应商时更痛。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

import httpx

from app.core.config import get_settings

# ------------------------------------------------------------
# 共享的 HTTP 客户端
# ------------------------------------------------------------
# ⚠️ 复用同一个 client 而不是每次请求新建：新建 = 每次重做 TCP 握手 + TLS 协商。
#    2C2G 上省下的那点 CPU 不算什么，但**连接数**是实打实的。
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """取（必要时创建）全局 HTTP 客户端。"""
    global _http_client
    if _http_client is None:
        settings = get_settings()
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.model_request_timeout_seconds),
            # 连不上就快速失败，别让一个请求把连接池占满
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


async def aclose_http_client() -> None:
    """关闭全局 HTTP 客户端（应用退出时调用）。"""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None


class ModelAPIError(RuntimeError):
    """模型 API 调用失败。

    ⚠️ 单独一个异常类型，是为了让上层能**区分**"模型炸了"和"我们自己的 bug"：
        前者应该给用户一个体面的错误（并计入可观测指标），后者应该 500。
    """


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


# ------------------------------------------------------------
# 对话（OpenAI 兼容：POST {base}/chat/completions）
# ------------------------------------------------------------
class ChatClient:
    """对话模型。支持流式与非流式。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # 去掉尾部斜杠，避免拼出 `//chat/completions`
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client

    def _client_or_default(self) -> httpx.AsyncClient:
        return self._client if self._client is not None else get_http_client()

    async def stream(self, messages: Sequence[dict[str, str]]) -> AsyncIterator[str]:
        """流式生成，逐段 yield 文本增量。

        ⚠️ **OpenAI 兼容的 SSE 格式不是标准 SSE 语义**：它是 `data:` 行里塞 JSON，
            流末尾固定来一个 `data: [DONE]`。所以这里手工解析，
            不套用通用的 SSE 解析器（会解析不出来）。
        """
        payload = {"model": self._model, "messages": list(messages), "stream": True}
        client = self._client_or_default()

        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=_auth_headers(self._api_key),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise ModelAPIError(f"对话 API 返回 {response.status_code}：{body[:300]}")

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    chunk = _parse_stream_chunk(data)
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"对话 API 连接失败：{exc}") from exc

    async def complete(self, messages: Sequence[dict[str, str]]) -> str:
        """非流式生成（评测与测试里更好断言）。"""
        payload = {"model": self._model, "messages": list(messages), "stream": False}
        client = self._client_or_default()

        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=_auth_headers(self._api_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"对话 API 连接失败：{exc}") from exc

        if response.status_code >= 400:
            raise ModelAPIError(f"对话 API 返回 {response.status_code}：{response.text[:300]}")

        body = response.json()
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelAPIError(f"对话 API 返回结构不符合预期：{body}") from exc


def _parse_stream_chunk(data: str) -> str:
    """从一行 `data: {...}` 里取出增量文本。取不到就返回空串（不抛异常）。

    ⚠️ 这里**故意容忍脏数据**：流式响应的中间帧可能只有 role、没有 content。
        为了一个空帧把整条流打断，是最亏的失败方式。
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return ""
    try:
        return str(payload["choices"][0]["delta"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        return ""


# ------------------------------------------------------------
# embedding（OpenAI 兼容：POST {base}/embeddings）
# ------------------------------------------------------------
class EmbeddingClient:
    """把文本转成向量。

    ⚠️ **`model` 与 `dim` 是元数据锁的两半**（AGENTS §7.1 约定 3）：
        换模型必须全量重嵌入，否则新旧向量混在一起，检索质量**静默变差**。
        所以这里把 model 一起返回，入库时写进 `chunks.embedding_model`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        #: 配置声明的维度。**与实际返回不一致就报错** —— 见 `_assert_dim`
        self.dim = dim
        self._client = client

    @property
    def model(self) -> str:
        """模型名。入库时要连同向量一起写进 `chunks.embedding_model`（元数据锁）。"""
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码。返回顺序与输入一致。"""
        if not texts:
            return []

        payload = {"model": self._model, "input": list(texts)}
        client = self._client if self._client is not None else get_http_client()

        try:
            response = await client.post(
                f"{self._base_url}/embeddings",
                headers=_auth_headers(self._api_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"embedding API 连接失败：{exc}") from exc

        if response.status_code >= 400:
            raise ModelAPIError(f"embedding API 返回 {response.status_code}：{response.text[:300]}")

        body = response.json()
        try:
            # ⚠️ 按 `index` 排序，**不要**假设返回顺序等于输入顺序。
            #    并发批处理的服务端不保证顺序，靠"它应该是顺序的"迟早出错。
            items = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise ModelAPIError(f"embedding API 返回结构不符合预期：{body}") from exc

        self._assert_dim(vectors)
        return vectors

    def _assert_dim(self, vectors: list[list[float]]) -> None:
        """维度校验 —— 宁可当场炸，也不要让错的向量进库。

        ⚠️ 维度不符的向量**照样能存进去**（代价是检索结果全是噪声），
            而且**不会报错**。所以这道校验必须在这里，不能靠"记得检查"。
        """
        for vector in vectors:
            if len(vector) != self.dim:
                raise ModelAPIError(
                    f"embedding 维度不符：配置 EMBEDDING_DIM={self.dim}，"
                    f"实际返回 {len(vector)}。改了模型就必须同步改配置并**全量重嵌入**。"
                )


# ------------------------------------------------------------
# rerank（**不是** OpenAI 标准；这里按阿里云百炼 DashScope 的形状）
# ------------------------------------------------------------
class RerankClient:
    """交叉编码精排：把 (query, doc) 成对打分。

    ⚠️ **它和 embedding 不是一回事**（计划书 §4.2）：embedding 是双塔，query 与 doc
        **分别**编码、彼此不见面；rerank 是交叉编码，**把两者拼在一起**过一遍模型。
        所以"用 embedding 模型当 rerank"是无效的 —— 它给不出比向量检索更细的排序。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        """返回 `[(原始下标, 相关性分数), ...]`，按分数从高到低。

        ⚠️ 返回的是**原始下标**而不是文档本身 —— 让调用方自己回查，
            避免在大对象上来回拷贝，也便于调用方保留原始元数据。
        """
        if not documents:
            return []

        payload = {
            "model": self._model,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {"top_n": min(top_n, len(documents)), "return_documents": False},
        }
        client = self._client if self._client is not None else get_http_client()

        try:
            response = await client.post(
                f"{self._base_url}/services/rerank/text-rerank/text-rerank",
                headers=_auth_headers(self._api_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ModelAPIError(f"rerank API 连接失败：{exc}") from exc

        if response.status_code >= 400:
            raise ModelAPIError(f"rerank API 返回 {response.status_code}：{response.text[:300]}")

        body = response.json()
        try:
            results = body["output"]["results"]
            return [(int(item["index"]), float(item["relevance_score"])) for item in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelAPIError(f"rerank API 返回结构不符合预期：{body}") from exc
