"""agent 的 4 个工具 —— **手写 Function Calling 里的那双手**（`PLAN §13.3` D8）。

## 这里只有 4 个工具，而且**故意**只有 4 个

| 工具 | 干什么 | 终止？ |
|---|---|---|
| `search_kb` | 混合检索（全文 ＋ 向量 → RRF → 精排），每条带回**出处 ＋ 有效期** | 否 |
| `get_order` | 按单号 / 最近几张查订单事实（**不含任何 PII**） | 否 |
| `ask_user` | 反问 —— **全程只允许问一次** | ✅ |
| `finish` | 交卷（答案 ＋ 引用） —— **不带引用不许收尾** | ✅ |

⚠️ **为什么没有 `check_conflicts`**（D8）：① 它要现算（36 篇两两配对，百毫秒~秒级），
   放进 6 步预算里是灾难；② 它是**运维视角** —— 顾客问"保修多久"时，
   它回答"库里有两处矛盾"，对顾客是纯噪音。它继续只留在「知识库」页给人看。

⚠️ **为什么"政策版本"不单开一个工具**：它和检索是**同一件事** ——
   版本信息本来就跟在那条 chunk 的文档上。开成两个工具，模型要先判断"这题算不算版本题"，
   而工具的**描述越相似，选错率越高**。并进 `search_kb` 的返回，零成本。

## 三条护栏写在代码里，不写在提示词里

1. **白名单 ＋ 参数校验**：`TOOL_NAMES` 之外的调用直接拒；参数逐个验（类型/长度/取值范围），
   并且不允许出现 `;` / `--` / URL / 路径穿越这类**看着像注入**的东西。
2. **工具输出是"不可信输入"**：chunk 原文里理论上可能写着"忽略以上指令"。
   所以正文一律先过 `mdtext.downgrade()`（洗掉 markdown 结构），**只作为数据回灌**，
   绝不拼进系统提示词。
3. **失败不抛给循环**：参数错 / 工具内部异常 → 返回 `ok=false` 的 payload 让模型自己改，
   **不许把异常抛出 `dispatch`** —— 一次工具失败不该让整次问答死掉。

## 与 `pipeline.py` 的关系

⚠️ **不共享那段检索代码，是刻意的**：`PLAN §13.4` 要求 `ANSWER_MODE=pipeline` 时
   **行为逐字节不变**。把 `_retrieve()` 抽出来共用，等于同时动"已经验证过的老路径"和
   "还没有验证过的新路径"，一旦指标变了就**分不清是谁的责任**。
   所以这里**重新组合同样的积木**（`HybridRetriever` ＋ 两个客户端），逻辑与 `_retrieve` 对齐；
   等评测证明 agent 站得住，再谈合并。

## `ref_time`（"用哪一天的眼光看知识库"）

⚠️ 顺序在 agent 这里**反过来了，这正是它比 pipeline 强的地方**：

    pipeline：先抽单号（正则）→ 定 ref_time → 检索         ← 抽不到就退化
    agent   ：先 `get_order` → 拿到下单时间 → 定 ref_time → `search_kb`

所以 `ToolRuntime` **不预先冻结 `ref_time`**，而是每次 `search_kb` 现算 ——
`get_order` 之后算出来的那个才是对的（"2024 年买的椅子保修多久"要用 2024 年那版条款）。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.metrics import record_citation_repaired, record_retrieval_degraded
from app.db.models.knowledge import ContentStatus, Document
from app.db.models.order import Order
from app.db.repository import TenantScopedRepository
from app.orders import find_order, order_snapshot
from app.rag import citations as citation_module
from app.rag import mdtext
from app.rag import prompt as prompt_module
from app.rag.providers import EmbeddingClient, ModelAPIError, RerankClient
from app.rag.reftime import RefTime, RefTimeSource, resolve_ref_time
from app.rag.retrieval import HybridRetriever, RetrievedChunk

logger = logging.getLogger(__name__)

# 工具名

SEARCH_KB = "search_kb"
GET_ORDER = "get_order"
ASK_USER = "ask_user"
FINISH = "finish"

#: 白名单。**不在这个元组里的名字一律拒绝**（模型编出来的工具名不许进 `dispatch`）。
TOOL_NAMES: tuple[str, ...] = (SEARCH_KB, GET_ORDER, ASK_USER, FINISH)

#: 终止工具：执行完循环必须结束（`ask_user` → 反问，`finish` → 交卷）。
TERMINAL_TOOLS: tuple[str, ...] = (ASK_USER, FINISH)

#: 单条 chunk 进工具返回的正文上限。与 `prompt.build_context` 的默认值一致：
#: 同一条证据在 pipeline 和 agent 两条路上看到的长度应当一样，否则比较不出真差异。
MAX_CHARS_PER_CHUNK = 1200

#: `search_kb` 一次最多给几条（模型可以传更小的 `top_k`，但不能超过这个）。
MAX_TOP_K = 20

#: `get_order(recent=True)` 给几张。
#: ⚠️ 2026-10-26 评测打出来的：给 3 张时，顾客说"上周那个床架"模型**分不清是哪张 → 干脆反问**。
#:    给 5 张让它能靠商品名对上（床架 / 床头柜 / 书桌……）。
RECENT_ORDERS = 5

#: 枚举值的**对客说法** —— 给模型看中文，接口对外仍然是英文枚举（与 `orders._STATUS_ZH` 同一口径）。
_CONTENT_STATUS_HINT: dict[str, str] = {
    ContentStatus.ACTIVE.value: "现行有效",
    ContentStatus.SUPERSEDED.value: "已被新版取代（仅在问及历史订单时才是正确答案）",
}

#: 参数里的"看着像注入"的东西。
#:
#: ⚠️ **这不是主要防线**（主要防线是"参数只当参数用"：`query` 只进 trgm/向量的 SQL 参数位，
#:    `order_no` 走 ORM 的 `==`），它只是一层**廉价的兜底** —— 真出现过就直接记日志。
#: ⚠️ 只抽 ASCII 的危险串：中文全角标点（`；`）是正常提问的一部分，不许误伤。
_SUSPICIOUS_ARG = re.compile(
    r"(;|--|/\*|\*/|https?://|[A-Za-z]:\\|\.\./|<\s*script)", re.IGNORECASE
)

#: 单号允许的字符集（`SF10001` / `202610210001`）。**用白名单而不是黑名单**：
#: 这一列最终进 SQL 的 `==` 比较，收紧字符集是零成本的。
_ORDER_NO_OK = re.compile(r"^[A-Za-z0-9-]{3,32}$")


class ToolArgumentError(ValueError):
    """参数不合法。**不抛给调用方** —— `dispatch` 会把它转成 `ok=false` 的 payload。"""


# 给模型的工具签名（OpenAI `tools` 格式）

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SEARCH_KB,
            "description": (
                "在客服知识库里检索（退货/换货/保修/物流/发票/会员等政策条款）。"
                "返回每条都带出处文件名、内容状态（现行/已被取代）和生效区间。"
                "问政策、问条款、问流程时必须先调它；一次没查到时可以换个说法再查一次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索用的一句话，用顾客的原话或政策里的说法，不要带单号",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": f"要几条，默认 5，最多 {MAX_TOP_K}",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": GET_ORDER,
            "description": (
                "查订单事实：状态、下单/签收时间、距今几天、商品、金额。"
                "顾客提到单号时用它；顾客说'我的单''最近买的'但没给单号时，"
                "可以先 recent=true 看看最近几张，也可以直接用 ask_user 问单号。"
                "它不返回手机号等隐私信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号，如 SF10001"},
                    "recent": {
                        "type": "boolean",
                        "description": "不给单号时，取最近几张订单",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ASK_USER,
            "description": (
                "反问顾客，用来补关键信息（订单号、支付方式、商品型号）。"
                "**一次对话只允许用一次**：问过之后必须自己想办法给答案，不许再问。"
                "只在'缺了这条信息就一定答不对'时才用，别拿它当开场白。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要问顾客的一句话，只问一件事",
                    }
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FINISH,
            "description": (
                "交卷。**必须带 citations**（本回答用到的检索结果 ref 编号）。"
                "答案里出现的每个数字都要能在引用的证据或订单里找到。"
                "只有'纯订单事实题'（如退款进度）才可以给空 citations。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "给顾客看的最终回答（中文，简洁，必要时分点）",
                    },
                    "citations": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "引用的 ref 编号列表，来自 search_kb 的返回",
                    },
                },
                "required": ["answer", "citations"],
                "additionalProperties": False,
            },
        },
    },
]


# 参数校验

_MAX_LEN: dict[str, int] = {
    "query": 200,
    "question": 200,
    "answer": 4000,
    "order_no": 32,
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    SEARCH_KB: ("query",),
    GET_ORDER: (),
    ASK_USER: ("question",),
    FINISH: ("answer", "citations"),
}

_ALLOWED: dict[str, frozenset[str]] = {
    SEARCH_KB: frozenset({"query", "top_k"}),
    GET_ORDER: frozenset({"order_no", "recent"}),
    ASK_USER: frozenset({"question"}),
    FINISH: frozenset({"answer", "citations"}),
}


def _check_text(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ToolArgumentError(f"`{key}` 必须是字符串")
    text = value.strip()
    if not text:
        raise ToolArgumentError(f"`{key}` 不能为空")
    limit = _MAX_LEN[key]
    if len(text) > limit:
        raise ToolArgumentError(f"`{key}` 太长（{len(text)} > {limit} 字）")
    if _SUSPICIOUS_ARG.search(text):
        # ⚠️ 只记 warning 然后照常放行 —— 拦住它对安全性没有实质帮助（参数本来就只进参数位），
        #    但**误伤一次正常提问**的代价是当场答错。留日志是为了将来能查"真有人试过"。
        logger.warning("工具参数里出现可疑串，已记日志但照常执行：key=%s", key)
    return text


def validate_arguments(name: str, arguments: object) -> dict[str, Any]:
    """校验并**规范化**一次工具调用的参数。不合法 → `ToolArgumentError`。

    ⚠️ **未知参数一律拒绝**（等价于 JSON Schema 的 `additionalProperties: false`）：
        模型很爱顺手塞 `"explanation": "..."` 这种自觉贴心的字段。放过的代价是
        参数越长越容易自相矛盾，而且没人知道哪些字段真的生效。
    ⚠️ **不用引 `jsonschema` 依赖**：4 个工具、不到 10 个字段，手写比引库更清楚，
       而且报错文案能直接写成"模型看得懂的中文"（它要用这个报错自我修正）。
    """
    if name not in TOOL_NAMES:
        raise ToolArgumentError(f"没有这个工具：{name}（只有 {', '.join(TOOL_NAMES)}）")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments 必须是对象")

    allowed = _ALLOWED[name]
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolArgumentError(f"不认识的参数：{', '.join(sorted(unknown))}")

    for key in _REQUIRED[name]:
        if key not in arguments:
            raise ToolArgumentError(f"缺少必填参数 `{key}`")

    clean: dict[str, Any] = {}

    for key in ("query", "question", "answer"):
        if key in arguments:
            clean[key] = _check_text(key, arguments[key])

    if "order_no" in arguments:
        raw = arguments["order_no"]
        if not isinstance(raw, str):
            raise ToolArgumentError("`order_no` 必须是字符串")
        order_no = raw.strip().upper()
        if not _ORDER_NO_OK.match(order_no):
            raise ToolArgumentError("`order_no` 只能是字母/数字/连字符，3~32 位")
        clean["order_no"] = order_no

    if "top_k" in arguments:
        raw = arguments["top_k"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentError("`top_k` 必须是整数")
        if not 1 <= raw <= MAX_TOP_K:
            raise ToolArgumentError(f"`top_k` 必须在 1~{MAX_TOP_K} 之间")
        clean["top_k"] = raw

    if "recent" in arguments:
        raw = arguments["recent"]
        if not isinstance(raw, bool):
            raise ToolArgumentError("`recent` 必须是布尔值")
        clean["recent"] = raw

    if "citations" in arguments:
        raw = arguments["citations"]
        if not isinstance(raw, list):
            raise ToolArgumentError("`citations` 必须是整数数组")
        refs: list[int] = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ToolArgumentError("`citations` 里只能放整数 ref 编号")
            if item not in refs:
                refs.append(item)
        clean["citations"] = refs

    return clean


# 证据


@dataclass(frozen=True)
class Evidence:
    """一条可被引用的证据（一块 chunk ＋ 它所属文档的版本信息）。

    ⚠️ `ref` 是**跨多次 `search_kb` 连续递增**的编号，不是每次从 1 开始 ——
        否则"先查 A 拿到 1，再查 B 又拿到 1"之后，`finish` 里的 `[1]` 指向谁就说不清了。
    ⚠️ `content_status` / 生效区间**必须一起返回**：同一句话在现行版和废止版里可能
        一字不差，唯一能区别它们的就是这两个字段（`AGENTS §7.1` 约定 20）。
    """

    ref: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int
    filename: str
    content_status: str
    effective_from: date | None
    effective_to: date | None
    score: float
    #: `rerank` / `vector_cosine` / `fulltext_trgm` —— **量纲不同，必须标出来**。
    score_kind: str
    #: 引用卡片用的一行摘要。
    snippet: str
    #: 洗过的正文（回灌给模型）。
    content: str

    def as_payload(self) -> dict[str, Any]:
        """转成给模型看的 JSON（**这就是它的全部世界**）。"""
        return {
            "ref": self.ref,
            "source": self.filename,
            "content_status": self.content_status,
            "content_status_hint": _CONTENT_STATUS_HINT.get(
                self.content_status, self.content_status
            ),
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "score": round(self.score, 4),
            "score_kind": self.score_kind,
            "chunk_ordinal": self.ordinal,
            "content": self.content,
        }

    def window_text(self) -> str:
        """`2024-01-01 ~ 2025-06-30` 这种一行式有效期，给 trace / 前端用。"""
        if self.effective_from is None and self.effective_to is None:
            return ""
        left = self.effective_from.isoformat() if self.effective_from else "—"
        right = self.effective_to.isoformat() if self.effective_to else "—"
        return f"{left} ~ {right}"


@dataclass
class ToolResult:
    """一次工具执行的结果。**循环只认这个结构**，不关心工具内部怎么实现的。"""

    name: str
    ok: bool
    #: 回灌给模型的内容（会被 `json.dumps` 成 tool message）。
    payload: dict[str, Any]
    #: 给 SSE `tool` 帧的一句话（**不含 PII**，前端直接显示"正在查订单…"）。
    summary: str
    ms: int = 0
    #: 本次新产生的证据（只有 `search_kb` 会填）。
    new_evidence: tuple[Evidence, ...] = ()
    #: 终止标记：`ask_user` / `finish` 之后循环必须结束。
    terminal: bool = False
    finish_answer: str | None = None
    finish_refs: tuple[int, ...] = ()
    #: `ask_user` 要问的那句话（落 `conversations.meta.awaiting`）。
    awaiting: str | None = None


# 运行时


@dataclass
class _OrderHit:
    """一张查到的订单（快照 ＋ 原始时间，后者给 `ref_time` 用）。"""

    snapshot: dict[str, Any]
    placed_at: datetime


class ToolRuntime:
    """一次问答内的工具执行器。**一个实例只服务一轮问答**（它攒证据、攒状态）。

    ⚠️ 为什么是"运行时对象"而不是 4 个裸函数：`search_kb` 的 `ref` 编号、
        `ask_user` 的"只许问一次"、`get_order` 查到的下单时间（影响 `ref_time`），
        这些都是**跨调用累积的状态**。塞进函数参数会让每个调用点都要记着传。
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        question: str,
        user_id: uuid.UUID | None = None,
        embedder: EmbeddingClient | None = None,
        reranker: RerankClient | None = None,
        settings: Settings | None = None,
        ref_time: date | None = None,
        now: datetime | None = None,
        max_searches: int | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._user_id = user_id
        #: 用户原话。**只用于定 `ref_time`**，不直接进检索（检索用模型给的 query）。
        self._question = question
        self._embedder = embedder
        self._reranker = reranker
        self._settings = settings or get_settings()
        #: 冻结的参照时刻（评测用）。非空则不再看订单、也不读问句。
        self._frozen_ref_time = ref_time
        self._now = now

        #: 本轮全部证据，`ref` 从 1 开始连续编号。
        self._evidence: list[Evidence] = []
        #: 是否已经问过用户（**全程只允许一次**，`PLAN §13.3`）。
        self._asked = False
        #: 查到过的订单（进 trace ＋ 允许"纯订单题"空引用收尾）。
        self._orders: list[_OrderHit] = []
        #: 上一次 `search_kb` 的置信度 —— 自愈判断要用（循环读它，不用重算）。
        self.last_confidence: float | None = None
        self.last_score_kind: str = "none"
        #: 已经搜过几次（轨迹指标）。
        self.search_count = 0
        #: 本轮最多许查几次。`agent_max_retries=1` 时就是 **2 次**：
        #: 查一次 + 允许改写重试一次（`PLAN §13.3` 的自愈）。
        #: ⚠️ 卡上限的是**代码**不是提示词 —— 否则模型会在"再查一次也许就有了"里耗光预算。
        self._max_searches = max_searches or (self._settings.agent_max_retries + 1)
        #: **白查过几次**（空结果 / 分数不够）。自愈的分母是它，不是总检索次数。
        self.retry_count = 0
        #: 交卷自查用过没有（**只许拦一次**，拦第二次就可能死循环）。
        self._self_check_used = False
        #: “已查到订单却还反问”这件事拦过没有（**同样只拦一次**）。
        #: ⚠️ 为什么只拦一次：确实存在“查到了单，但顾客问的是另一件事”的情况，
        #:    彻底禁止会让它只能硬答。拦一次 = 强制它先试着用手里的订单回答；
        #:    真不行，它第二次调 `ask_user` 照样能过。
        self._asked_after_order_blocked = False
        #: 顾客**明确给了单号**时查到的那张单（`recent` 不算）。
        self._explicit_order_no: str | None = None
        #: 最近一次交卷自查的结果（`Answer.extra["citation_audit"]`）。
        self.last_audit: Any = None
        #: 修法 D：本轮**自动改正**过的编号（改前 / 改后 / 命中块）。
        self.last_repairs: list[dict[str, Any]] = []

    # 只读属性

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(self._evidence)

    @property
    def orders(self) -> tuple[dict[str, Any], ...]:
        return tuple(hit.snapshot for hit in self._orders)

    @property
    def asked(self) -> bool:
        return self._asked

    @property
    def ref_time(self) -> RefTime:
        """这次检索**用哪一天的眼光**（现算，不是构造时定）。

        ⚠️ **每次 `search_kb` 都重算**：`get_order` 之后拿到的下单时间才是准的 ——
            这正是 agent 相对 pipeline 的那点优势（见模块 docstring）。
        """
        if self._frozen_ref_time is not None:
            return RefTime(self._frozen_ref_time, RefTimeSource.FROZEN)
        return resolve_ref_time(
            question=self._question,
            order_placed_at=[hit.placed_at for hit in self._orders],
        )

    @property
    def now(self) -> datetime:
        if self._now is None:
            self._now = datetime.now().astimezone()
        return self._now

    # 入口

    async def dispatch(self, name: str, arguments: object) -> ToolResult:
        """执行一次调用。**任何异常都在这里被兜住**，绝不抛给循环。

        ⚠️ 为什么要兜住一切：agent 的可靠性来自"工具挂了模型还能换个法子"，
            而一个冒泡出去的 `KeyError` 会让整次问答变成 500。
        ⚠️ 但**日志必须留全**：静默兜底 ＋ 看不见的通道 ＝ 本项目最讨厌的那种坑。
        """
        started = time.perf_counter()
        try:
            args = validate_arguments(name, arguments)
            result = await self._route(name, args)
        except ToolArgumentError as exc:
            result = _failed(name, str(exc), tip="按正确的参数格式再调一次")
        except Exception:
            logger.exception("工具执行失败：%s", name)
            result = _failed(name, "工具内部出错了（不是你的参数问题）", tip="换个办法继续")
        result.ms = int((time.perf_counter() - started) * 1000)
        return result

    async def _route(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == SEARCH_KB:
            return await self._search_kb(args["query"], top_k=args.get("top_k"))
        if name == GET_ORDER:
            return await self._get_order(args.get("order_no"), recent=args.get("recent", False))
        if name == ASK_USER:
            return self._ask_user(args["question"])
        return self._finish(args["answer"], args["citations"])

    # search_kb

    async def _search_kb(self, query: str, *, top_k: int | None = None) -> ToolResult:
        settings = self._settings
        # ⚠️ **卡的是"白查了几次"，不是"查了几次"。**
        #    2026-10-26 评测实测：多诉求题（"退款多久到 + 运费谁出"）合理地要查
        #    **两次不同的话**，按总次数卡会把第二次正常检索当成重试拦掉，
        #    模型只好去 `ask_user` —— 一个该答的题被拦成了反问。
        if (
            self._settings.agent_max_retries >= 0
            and self.retry_count > self._settings.agent_max_retries
        ):
            return ToolResult(
                name=SEARCH_KB,
                ok=False,
                payload={
                    "ok": False,
                    "error": f"这一轮已经查过 {self.search_count} 次，不允许再查",
                    "hint": f"用现有结果收尾（{FINISH}）；确实缺关键信息就用 {ASK_USER} 问一次。",
                },
                summary="检索次数用完",
            )
        self.search_count += 1
        ref_time = self.ref_time

        retriever = HybridRetriever(
            self._session,
            tenant_id=self._tenant_id,
            scope=settings.retrieval_scope,
            ref_time=ref_time.value,
        )

        # ⚠️ embedding 挂了不该让工具挂：退化成纯全文检索（可用性 > 功能齐全）。
        query_embedding: list[float] | None = None
        if self._embedder is not None:
            try:
                vectors = await self._embedder.embed([query])
                query_embedding = vectors[0] if vectors else None
            except ModelAPIError:
                logger.warning("search_kb：embedding 失败，退化为纯全文检索")
                record_retrieval_degraded("embedding")

        hits, vector_scores = await retriever.search(
            query=query,
            query_embedding=query_embedding,
            top_k=settings.retrieve_top_k,
            rrf_k=settings.rrf_k,
            fulltext_min_similarity=settings.fulltext_min_similarity,
            embedding_model=self._embedder.model if self._embedder is not None else None,
        )

        want = top_k or settings.rerank_top_n
        hits, confidence, score_kind = await self._rerank(query, hits, vector_scores)
        hits = hits[:want]

        self.last_confidence = confidence
        self.last_score_kind = score_kind

        if not hits:
            self.retry_count += 1
            return ToolResult(
                name=SEARCH_KB,
                ok=True,
                payload={
                    "ok": True,
                    "results": [],
                    "confidence": None,
                    "score_kind": "none",
                    "hint": "知识库里没检索到。可以换个说法（用更接近政策原文的词）再查一次。",
                },
                summary="知识库没有命中",
            )

        docs = await self._load_documents([hit.document_id for hit in hits])
        citation_kind = "fulltext_trgm" if score_kind == "none" else score_kind
        fresh: list[Evidence] = []
        for hit in hits:
            meta = docs.get(hit.document_id)
            if meta is None:
                # 文档被删了 / 不在本租户 —— 证据链断了，宁可少一条也不给一条无出处的。
                continue
            fresh.append(
                Evidence(
                    ref=len(self._evidence) + len(fresh) + 1,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    ordinal=hit.ordinal,
                    filename=meta["filename"],
                    content_status=meta["content_status"],
                    effective_from=meta["effective_from"],
                    effective_to=meta["effective_to"],
                    score=hit.score,
                    score_kind=citation_kind,
                    snippet=prompt_module.make_snippet(hit.content),
                    content=_prepare_content(hit.content),
                )
            )
        self._evidence.extend(fresh)

        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "confidence": None if confidence is None else round(confidence, 4),
            "confidence_kind": score_kind,
            "effective_on": ref_time.value.isoformat(),
            "results": [item.as_payload() for item in fresh],
        }
        if confidence is not None and confidence < settings.answer_min_score and fresh:
            self.retry_count += 1
            payload["hint"] = (
                "这批结果的相关度偏低。可以换个说法再查一次，或者用 ask_user 请顾客补充信息。"
            )
        return ToolResult(
            name=SEARCH_KB,
            ok=True,
            payload=payload,
            summary=f"查知识库「{query}」· {len(fresh)} 条",
            new_evidence=tuple(fresh),
        )

    async def _rerank(
        self,
        query: str,
        hits: list[RetrievedChunk],
        vector_scores: dict[uuid.UUID, float],
    ) -> tuple[list[RetrievedChunk], float | None, str]:
        """与 `pipeline.RagPipeline._retrieve` 同一口径（`rerank` → `vector_cosine` → `none`）。

        ⚠️ 顺序与退化路径必须一致，否则同一条证据在两条路上**分数口径不同**，
            评测出来的差异就说不清是"agent 更聪明"还是"阈值口径变了"。
        """
        if not hits:
            return [], None, "none"
        settings = self._settings
        if self._reranker is not None:
            try:
                ranked = await self._reranker.rerank(
                    query, [hit.content for hit in hits], top_n=settings.rerank_top_n
                )
            except ModelAPIError:
                logger.warning("search_kb：rerank 失败，本次退化为 RRF 顺序 + 余弦置信度")
                record_retrieval_degraded("rerank")
                ranked = []
            if ranked:
                scores = {
                    hits[index].chunk_id: score for index, score in ranked if 0 <= index < len(hits)
                }
                reranked = [
                    RetrievedChunk(
                        chunk_id=hits[index].chunk_id,
                        document_id=hits[index].document_id,
                        ordinal=hits[index].ordinal,
                        content=hits[index].content,
                        score=score,
                    )
                    for index, score in ranked
                    if 0 <= index < len(hits)
                ]
                if reranked:
                    return reranked, max(scores.values()), "rerank"
        if vector_scores:
            best = max(vector_scores[hit.chunk_id] for hit in hits if hit.chunk_id in vector_scores)
            return hits, best, "vector_cosine"
        return hits, None, "none"

    async def _load_documents(
        self, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """一次查回这批 chunk 所属文档的**出处与版本信息**。

        ⚠️ 不能走 `chunk.document` 关系：那个关系是 `lazy="raise"`（异步下隐式懒加载必炸），
            而且这里只要 4 个字段，不值得把整行 documents 拉回来。
        """
        if not document_ids:
            return {}
        stmt = select(
            Document.id,
            Document.filename,
            Document.content_status,
            Document.effective_from,
            Document.effective_to,
        ).where(
            Document.tenant_id == self._tenant_id,
            Document.id.in_(list(document_ids)),
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            row.id: {
                "filename": row.filename,
                "content_status": (
                    row.content_status.value
                    if isinstance(row.content_status, ContentStatus)
                    else str(row.content_status)
                ),
                "effective_from": row.effective_from,
                "effective_to": row.effective_to,
            }
            for row in rows
        }

    # get_order

    async def _get_order(self, order_no: str | None, *, recent: bool) -> ToolResult:
        """查订单。**不返回任何 PII** —— `order_snapshot` 里本来就只有可核对的事实。"""
        if order_no:
            order = await find_order(self._session, tenant_id=self._tenant_id, order_no=order_no)
            found = [order] if order is not None else []
            if order is not None:
                # ⚠️ 只有**顾客亲口报的单号**才算“你已经知道是哪一单”。
                #    `recent=True` 拿到的几张是**猜**的，那时反问仍然合理。
                self._explicit_order_no = order.order_no
            summary = f"查订单 {order_no}"
        elif recent:
            found = await self._recent_orders()
            summary = "查最近订单"
        else:
            # 既没单号也没 recent —— 让模型自己决定下一步，别猜。
            return ToolResult(
                name=GET_ORDER,
                ok=False,
                payload={
                    "ok": False,
                    "error": "要么给 order_no，要么 recent=true",
                    "hint": f"不知道单号的话，用 {ASK_USER} 问顾客。",
                },
                summary="订单参数不足",
            )

        if not found:
            # ⚠️ **一个字都不给**（与 `app/orders.py` 模块说明同一个理由）：
            #    返回"没查到订单"会让模型顺着这句往下猜；空数组它就没有材料可编。
            return ToolResult(
                name=GET_ORDER,
                ok=True,
                payload={"ok": True, "orders": [], "note": "没有匹配的订单"},
                summary=f"{summary} · 没查到",
            )

        snapshots: list[dict[str, Any]] = []
        for order in found:
            snapshot = order_snapshot(order, now=self.now)
            snapshots.append(snapshot)
            self._orders.append(_OrderHit(snapshot=snapshot, placed_at=order.placed_at))

        return ToolResult(
            name=GET_ORDER,
            ok=True,
            payload={
                "ok": True,
                "orders": snapshots,
                # ⚠️ 这句不是客套话：不说的话模型会为了“确认是哪一张”而反问，
                #    而反问是**终止工具** —— 一个能答的题就变成了一句反问。
                "hint": "这是该顾客最近的订单。商品名能和顾客描述对上就直接用，不要为了确认而反问。",
            },
            summary=f"{summary} · {len(snapshots)} 张",
        )

    async def _recent_orders(self) -> list[Order]:
        """本租户最近的几张单。

        ⚠️ 口径与 `find_order` 一致（**只按租户**，`PLAN §12.1` D5）：Web 是内部坐席工作台，
            坐席手里往往只有顾客口述的模糊线索。**但只给 3 张**，且工具描述里写明了
            "可能不是提问者的" —— 给多了模型会随便挑一张当成结论。
        """
        repo = TenantScopedRepository(self._session, tenant_id=self._tenant_id, model=Order)
        stmt = repo.select().order_by(Order.placed_at.desc()).limit(RECENT_ORDERS)
        return list((await self._session.scalars(stmt)).all())

    # ask_user

    def _ask_user(self, question: str) -> ToolResult:
        """反问。**全程只允许一次**。

        ⚠️ 为什么要卡死"一次"：不卡的话模型会退化成"问一句、等一句"的聊天机器人，
            而顾客那边只看到一串反问。真正该做的是**问一次拿到关键信息，然后自己查完**。
        """
        # ⚠️ 2026-10-26（评测打出来的一条行为规则）：
        #    它已经用 `get_order` 查到了订单，却去问顾客“请确认是哪一单”。
        #    对**内部坐席**而言这是废话：他手里就有这张单。
        #    所以先拦一次、要求它用现有事实回答。
        if self._explicit_order_no and not self._asked_after_order_blocked:
            self._asked_after_order_blocked = True
            return ToolResult(
                name=ASK_USER,
                ok=False,
                payload={
                    "ok": False,
                    "error": "你已经查到订单了，不该再问顾客确认",
                    "orders_at_hand": self._explicit_order_no,
                    "hint": f"先基于已查到的订单事实给答案（必要时再 {SEARCH_KB} 查条款），然后 {FINISH}。",
                },
                summary="反问被拒绝（已查到订单）",
            )

        if self._asked:
            return ToolResult(
                name=ASK_USER,
                ok=False,
                payload={
                    "ok": False,
                    "error": "已经问过顾客一次了，不能问第二次",
                    "hint": f"用现有信息尽量回答，然后调 {FINISH}。",
                },
                summary="反问被拒绝（已问过一次）",
            )
        self._asked = True
        return ToolResult(
            name=ASK_USER,
            ok=True,
            payload={"ok": True, "status": "awaiting_user", "question": question},
            summary=f"反问：{question}",
            terminal=True,
            awaiting=question,
        )

    # finish

    def _audit_hits(self) -> list[RetrievedChunk]:
        """把本轮证据转成核对器要的形状（`ref` 天然就是 1..N 连续编号）。"""
        return [
            RetrievedChunk(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                ordinal=item.ordinal,
                content=item.content,
                score=item.score,
            )
            for item in self._evidence
        ]

    def _extra_facts(self) -> str:
        """**不占编号**的附加证据：订单事实（签收日 / 天数 / 金额 / 商品）。

        ⚠️ 拼法与 `_audit` 原来的完全一致，别改 —— 改一下"8 天"就可能又核不上了。
        """
        if not self._orders:
            return ""
        lines: list[str] = []
        for hit in self._orders:
            item = hit.snapshot
            names = " ".join(str(entry.get("name", "")) for entry in (item.get("items") or []))
            lines.append(
                " ".join(
                    str(value)
                    for value in (
                        item.get("order_no"),
                        item.get("status"),
                        item.get("placed_at"),
                        item.get("delivered_at"),
                        item.get("days_since_delivery"),
                        item.get("amount"),
                        names,
                    )
                    if value not in (None, "")
                )
            )
        return " ".join(lines)

    def _audit(self, answer: str) -> Any:
        """拿已有证据去核答案里的数字。

        ⚠️ **订单事实也是证据，但它不占 `[n]` 编号**。
            2026-10-26 本地端到端实测打出来：答案写"签收日 2026-10-17，
            目前签收 8 天[1][2]"，两个数字都来自订单快照，却被判成“串号”——
            因为核对器只看引用的 chunk。后果不只是界面上多一条噪音：
            交卷自查会**因为这个误报把正确答案退回去重写**。
            所以把它作为**不占编号的附加证据**传进去（`extra_evidence`）。
        """
        return citation_module.audit(answer, self._audit_hits(), extra_evidence=self._extra_facts())

    def _repair(self, answer: str) -> Any:
        """修法 D：**交卷前**把指错的编号指回真正写着那个数字的块。

        ⚠️ 为什么放在交卷这里：agent 的回答是**一整段**参数，此刻文字一个字都还没发出去
            （`_finish` 之后才 `yield Delta`）—— 这是唯一能"改而不被看见"的时机。
        ⚠️ 它顺带救掉一类**误退**：错块号的答案本来要被退回重写一轮（多花一次模型调用），
            现在直接改对编号就过了。
        """
        return citation_module.repair(
            answer, self._audit_hits(), extra_evidence=self._extra_facts()
        )

    def _finish(self, answer: str, citations: list[int]) -> ToolResult:
        """交卷。**引用必须能对上** —— 对不上就退回去让它重来（循环只给一次机会）。

        ⚠️ **空引用只允许一种情况**：本轮查过订单，且答案里确实没有政策结论
            （比如"SF10001 退款处理中，预计 3 个工作日"）。这时证据是订单事实，
            不是知识库条款 —— 硬要它引一条不相干的 policy 反而更糟。
        """
        known = {item.ref for item in self._evidence}
        unknown = [ref for ref in citations if ref not in known]
        if unknown:
            return ToolResult(
                name=FINISH,
                ok=False,
                payload={
                    "ok": False,
                    "error": f"这些 ref 不存在：{unknown}",
                    "available_refs": sorted(known),
                    "hint": f"只能引用 search_kb 返回过的 ref；没有证据就先调 {SEARCH_KB}。",
                },
                summary="交卷被拒绝（引用对不上）",
            )
        if not citations and not self._orders:
            return ToolResult(
                name=FINISH,
                ok=False,
                payload={
                    "ok": False,
                    "error": "没有引用就不许收尾",
                    "hint": f"先调 {SEARCH_KB} 拿到证据；如果缺关键信息，调 {ASK_USER}。",
                },
                summary="交卷被拒绝（没有引用）",
            )
        # 交卷前自查（`PLAN §13.3` 的"交卷前自查"）：
        # 答案里的数字必须能在引用里找到。复用 pipeline 那个串号审计器，不另写一套。
        #
        # ⚠️ **只拦一次**（`_self_check_used`）：拦第二次就可能
        #    “修不对 → 被拦 → 再修”无限循环，而步数是有限的。
        # ⚠️ **查过订单就不拦**：金额 / 签收天数本来就在订单事实里，
        #    不在知识库条款里 —— 硬拦会把正确答案卡死。
        # 修法 D：**先修编号，再自查** —— 顺序不能反。
        # 反过来的话，一个只是编号错一格的答案会被判"核对不上"、退回重写一轮，
        # 而它其实一个字都不用改，只是 `[3]` 该写成 `[5]`。
        repaired = self._repair(answer)
        if repaired.changed:
            answer = repaired.text
            self.last_repairs = [fix.as_dict() for fix in repaired.fixes]
            record_citation_repaired(len(repaired.fixes))
            for fix in repaired.fixes:
                logger.info("agent 交卷前改正引用编号：%s", fix.describe())

        report = self._audit(answer)
        self.last_audit = {
            "checked": report.checked,
            "unsupported": report.unsupported,
            "issues": [item.as_dict() for item in report.issues],
        }
        if report.unsupported and not self._orders and not self._self_check_used:
            self._self_check_used = True
            numbers = sorted({atom for item in report.issues for atom in item.atoms})[:6]
            return ToolResult(
                name=FINISH,
                ok=False,
                payload={
                    "ok": False,
                    "error": "答案里有数字在引用里找不到",
                    "unsupported": numbers,
                    "hint": f"先用 {SEARCH_KB} 补证据，或者刚掉查不到的那句话。",
                },
                summary="交卷被拒绝（数字核对不上）",
            )

        return ToolResult(
            name=FINISH,
            ok=True,
            payload={"ok": True, "status": "finished"},
            summary="交卷",
            terminal=True,
            finish_answer=answer,
            finish_refs=tuple(citations),
        )


def _failed(name: str, error: str, *, tip: str) -> ToolResult:
    return ToolResult(
        name=name,
        ok=False,
        payload={"ok": False, "error": error, "hint": tip},
        summary=f"{name} 失败",
    )


def _prepare_content(text: str) -> str:
    """把 chunk 原文洗成"可以安全回灌给模型的数据"。

    ⚠️ **两步都要**：① `mdtext.downgrade()` 洗掉表格/标题这类结构（模型会把它们当格式指令）；
        ② 截断 —— 一条 chunk 上千字，6 步预算里塞满正文会把提示词预算吃光。
    ⚠️ 洗完仍然只是**数据**：调用方负责把它放进 `role=tool` 的消息里，不许拼进系统提示词。
    """
    cleaned = mdtext.downgrade(text or "").strip()
    if len(cleaned) > MAX_CHARS_PER_CHUNK:
        cleaned = cleaned[:MAX_CHARS_PER_CHUNK].rstrip() + "…"
    return cleaned


__all__ = [
    "ASK_USER",
    "FINISH",
    "GET_ORDER",
    "MAX_TOP_K",
    "RECENT_ORDERS",
    "SEARCH_KB",
    "TERMINAL_TOOLS",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "Evidence",
    "ToolArgumentError",
    "ToolResult",
    "ToolRuntime",
    "validate_arguments",
]
