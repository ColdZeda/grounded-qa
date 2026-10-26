"""agent 单循环 —— **"模型自己选工具、自己收尾，但预算权在代码里"**（`PLAN §13.3` D7）。

## 一句话说清它和 `pipeline.py` 的区别

    pipeline：正则抽单号 → 一次检索 → 精排 → 判拒答 → 一次生成     ← **路径固定**
    agent   ：循环 { 模型选工具 → 执行 → 结果回灌 } 直到 `finish`    ← **路径由模型定**

判据不是"用了几个工具"，而是**那四件事**（`architecture §9.48`）：
自己决定调什么、调几次、什么时候收尾、什么时候该反问。

## 这个循环里，哪些是模型的地盘，哪些是代码的地盘

| 交给模型 | 留在代码里（**不可协商**） |
|---|---|
| 调哪个工具、传什么参数、调几次 | 最多 6 步、总 20 s、单步 8 s |
| 检索没召回时换什么说法再查 | 超预算**立刻回落 pipeline**（最坏情况 = 今天，不会更差） |
| 什么时候反问、什么时候交卷 | 反问**只许一次**、不带引用**不许收尾**（`tools.py`） |
| 答案怎么写 | 最终仍过**阈值拒答**这一关 |

⚠️ 左上角那三条如果不给模型，它就是个 workflow；右下角那三条如果交给模型，
   它就会无限循环、无限花钱、并且没人能预测一次问答的成本。**分界线就在这。**

## 为什么不流式

agent 的每一步都**不给人看**：它要的是"调哪个工具"这四个字，不是一段散文。
所以循环用非流式（`ChatClient.complete_with_tools`），只有最终答案那一下才发 `Delta`。

⚠️ 代价：答案**一次性**到达，没有逐字效果（pipeline 是有的）。
   已记在 `PLAN §13.7` 已知代价里 —— 客服场景下"正在查…"的进度感
   （SSE `tool` 帧）比逐字动画更重要，因为 agent 本来就慢 2~4 倍。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.rag import prompt as prompt_module
from app.rag.pipeline import Answer, Citation, Delta, RagPipeline, Sources, should_refuse
from app.rag.providers import ChatClient, EmbeddingClient, ModelAPIError, RerankClient
from app.rag.retrieval import RetrievedChunk
from app.rag.tools import (
    ASK_USER,
    FINISH,
    GET_ORDER,
    SEARCH_KB,
    TOOL_SCHEMAS,
    ToolResult,
    ToolRuntime,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolEvent:
    """一次工具执行 —— 对应 SSE 的 `tool` 帧（`{name, args, ok, ms}`）。

    ⚠️ **这一帧不是装饰**：agent 延迟是 pipeline 的 2~4 倍，中间几秒什么都不给，
        用户会以为卡死了。前端拿它渲染"正在查订单…"（`PLAN §13.3`）。
    ⚠️ `args_preview` 要**截断且不含 PII** —— 它只用来让人看懂"在干嘛"。
    """

    name: str
    args_preview: str
    ok: bool
    ms: int
    summary: str


AgentEvent = Sources | Delta | Answer | ToolEvent


#: agent 的系统提示词。
#:
#: ⚠️ **与 pipeline 的提示词分开写、不共用**：pipeline 那段是"给你这几条资料，照着答"，
#:    而这里要教的是"你有哪些工具、什么时候用、不许干什么"。混在一份里
#:    会让 `ANSWER_MODE=pipeline` 的行为跟着变 —— 那是 `PLAN §13.4` 明令禁止的。
_SYSTEM_PROMPT = """你是电商客服助手的**调度者**。你不直接回答，而是用工具取材料，最后用 {finish} 交卷。

可用工具：
- `{search_kb}`：查知识库（退换货、保修、物流、发票、会员等政策条款）。**问政策必须先查**。
- `{get_order}`：查订单事实（状态、时间、商品、金额）。顾客给了单号就必须查，别猜。
- `{ask_user}`：反问顾客补信息。**整个对话只许用一次。**
- `{finish}`：交卷。必须带 `citations`（引用 `search_kb` 返回的 ref 编号）。

规则（违反会被工具拒绝，别浪费步数）：
1. 答案里的每个数字都必须能在引用的条目或订单事实里找到，**不许自己算日子、不许推断**；
   引用标记 `[ref]` 必须写在**句号前面**（如 `保修 2 年[1]。`）—— 写在句号后面核对器就认不出来了。
2. 「保修多久」「能不能退」这类**结论性**问题必须先 `search_kb`，再 `finish` 并引用。
3. 顾客没说单号而问题又依赖订单（"我的退款到账没"）→ **要么** `get_order(recent=true)` 先看看，
   **要么** `ask_user` 问一次。问了就不要再问第二次。
   ⚠️ 顾客用"我那张床头柜 / 我三周前买的"这种说法时，**先 `get_order(recent=true)` 看一眼**，
   别开口就问单号；**商品名能和顾客描述对上就直接答**。
   ⚠️ **缺关键信息（单号 / 商品 / 地区）且工具确实查不到时，就用 `ask_user` 问一句，
   不要拿一大堆通用政策去猜——那叫答非所问。
4. 检索结果里 `content_status` 是"已被新版取代"的，只有**问的是当年那笔订单**时才能用，
   并在答案里说明"按当时条款"。
5. 一次可以调多个工具（比如同时查退款和保修），这比串两步快。
6. 检索没查到时，**换个说法再查一次**再决定要不要拒答。

顾客的问题：{question}"""


class AgentRunner:
    """一轮问答的编排器。**一个实例只服务一个问题**（与 `RagPipeline` 一致）。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        chat: ChatClient,
        question: str,
        fallback: Callable[[], RagPipeline],
        embedder: EmbeddingClient | None = None,
        reranker: RerankClient | None = None,
        settings: Settings | None = None,
        history: Sequence[dict[str, str]] = (),
        user_id: uuid.UUID | None = None,
        ref_time: date | None = None,
        scratchpad: dict[str, object] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._chat = chat
        self._question = question
        #: 回落用的工厂。**传工厂而不是实例**：pipeline 要一堆参数（订单事实、图片、
        #: 参照时刻…），让 agent 自己去拼等于把两边的构造逻辑复制一份，迟早漂移。
        self._fallback_factory = fallback
        self._settings = settings or get_settings()
        self._history = tuple(history)
        #: 跨轮事实（`conversations.meta.scratchpad`，阶段 7-③）。空 = 首轮。
        self._scratchpad = dict(scratchpad or {})

        self._runtime = ToolRuntime(
            session=session,
            tenant_id=tenant_id,
            question=question,
            user_id=user_id,
            embedder=embedder,
            reranker=reranker,
            settings=self._settings,
            ref_time=ref_time,
        )
        #: 全轨迹（`Answer.extra["agent_trace"]`）。**没有它，agent 的行为无法归因** ——
        #: 路径不固定，事后只能靠这一串回放"它当时为什么这么答"。
        self._trace: list[dict[str, object]] = []
        self._steps = 0
        self._retries = 0
        self._usage: dict[str, int] = {}
        self._degraded: str | None = None

    # ---------- 对外入口 ----------

    async def answer(self) -> AsyncIterator[AgentEvent]:
        """跑循环，逐个 yield 事件（与 `RagPipeline.answer` 同一套事件类型 ＋ `ToolEvent`）。"""
        started = time.perf_counter()
        deadline = started + self._settings.agent_total_budget_seconds
        messages = self._build_messages()

        finished: ToolResult | None = None
        while finished is None:
            remaining = deadline - time.perf_counter()
            if self._steps >= self._settings.agent_max_steps:
                index = len(self._trace) + 1
                self._trace.append({"step": index, "kind": "budget", "reason": "max_steps"})
                async for event in self._fallback("步骤用完了"):
                    yield event
                return
            if remaining <= 0.05:
                index = len(self._trace) + 1
                self._trace.append({"step": index, "kind": "budget", "reason": "total_timeout"})
                async for event in self._fallback("总预算用完"):
                    yield event
                return

            step_timeout = min(self._settings.agent_step_timeout_seconds, remaining)
            try:
                # ⚠️ **`usage` 必须传进去**（2026-10-26 评测打出来的 bug）：
                #    不传的话客户端拿不到一个可填的字典，
                #    每一步的 token 都会记成 0 —— 而那本账正是配额扣费的依据，
                #    等于 agent 跑多少都**不扣额度**，而且看板上看不出来。
                step_usage: dict[str, int] = {}
                result = await self._chat.complete_with_tools(
                    messages, TOOL_SCHEMAS, usage=step_usage, request_timeout=step_timeout
                )
            except ModelAPIError as exc:
                logger.warning("agent 第 %s 步模型调用失败：%s", self._steps + 1, exc)
                index = len(self._trace) + 1
                self._trace.append({"step": index, "kind": "model_error", "error": str(exc)[:200]})
                async for event in self._fallback("模型调用失败"):
                    yield event
                return

            self._steps += 1
            self._add_usage(result.usage or step_usage)
            index = self._steps
            self._trace.append(
                {
                    "step": index,
                    "kind": "model",
                    "content": result.content[:200],
                    "tool_calls": [call.name for call in result.tool_calls],
                    "finish_reason": result.finish_reason,
                }
            )

            if not result.tool_calls:
                # 模型光说话不调工具。**给它一次改的机会**，但不无限纵容：
                # 下一步还不调工具，步数迟早用完 → 回落 pipeline。
                messages.append({"role": "assistant", "content": result.content})
                messages.append(
                    {"role": "user", "content": f"请用 {FINISH} 工具交卷，或用 {ASK_USER} 反问。"}
                )
                continue

            # ① 把模型这一轮的话原样放回上下文（含 tool_calls，否则下一轮协议不完整）
            messages.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": call.id or f"call_{index}_{pos}",
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments or "{}",
                            },
                        }
                        for pos, call in enumerate(result.tool_calls)
                    ],
                }
            )

            # ② 执行本步的所有 tool_calls。
            #
            #    ⚠️ **一次模型返回里的多个工具调用，省的是"步数"而不是"SQL 时间"。**
            #       本来串两步要调两次模型（贵、慢、还可能跑偏），现在一步就够 ——
            #       这才是 `PLAN §13.3` 里"并行调用"的真实收益。
            #
            #    ⚠️ **这里故意是顺序执行，不是 `asyncio.gather`。** 2026-10-26 实测：
            #       同一个 `AsyncSession` **不允许并发**（SQLAlchemy 直接抛
            #       `concurrent operations are not permitted`）。要真并行就得让每个工具
            #       各拿一个会话，那在 2C2G 上是"为省几毫秒多开两条连接"，不划算。
            #       → 记进 `..\plan\CANDIDATES.md`，等评测显示这一步是瓶颈再说。
            executions: list[ToolResult] = []
            for call in result.tool_calls:
                if call.parse_error:
                    executions.append(_reject_json(call.name, call.parse_error, call.raw_arguments))
                else:
                    executions.append(await self._runtime.dispatch(call.name, call.arguments))

            for call, execution in zip(result.tool_calls, executions, strict=True):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id or f"call_{index}",
                        "content": json.dumps(execution.payload, ensure_ascii=False),
                    }
                )
                self._trace.append(
                    {
                        "step": index,
                        "kind": "tool",
                        "name": execution.name,
                        "args": _preview(call.raw_arguments),
                        "ok": execution.ok,
                        "ms": execution.ms,
                        "summary": execution.summary,
                    }
                )
                yield ToolEvent(
                    name=execution.name,
                    args_preview=_preview(call.raw_arguments),
                    ok=execution.ok,
                    ms=execution.ms,
                    summary=execution.summary,
                )

            terminals = [item for item in executions if item.terminal]
            if terminals:
                # 一步里同时调了 `finish` 和 `ask_user`（模型偶尔会这么干）→ 交卷优先：
                # 交卷是"我答完了"，反问是"我还缺信息"，两者的语义冲突，取前者。
                finished = next((item for item in terminals if item.name == FINISH), terminals[0])

        async for event in self._finish(finished, started=started):
            yield event

    # ---------- 收尾 ----------

    async def _finish(self, finished: ToolResult, *, started: float) -> AsyncIterator[AgentEvent]:
        """把工具的"终止"翻译成对外的 `Sources` / `Delta` / `Answer`。"""
        if finished.name == ASK_USER:
            # 反问：要问的那句话**就是给用户的回复**（前端当普通消息显示）。
            question = finished.awaiting or ""
            self._trace.append({"step": self._steps, "kind": "ask_user", "question": question})
            yield Sources(citations=[], confidence=None, refused=False)
            yield Delta(text=question)
            yield Answer(
                text=question,
                refused=False,
                citations=[],
                confidence=None,
                latency_ms=_elapsed_ms(started),
                extra=self._extra(awaiting=question),
                usage=dict(self._usage),
            )
            return

        text = finished.finish_answer or ""
        citations = self._citations(finished.finish_refs)

        # 护栏 3：**下结论这一步不自由** —— 最终答案仍要过阈值拒答。
        if self._should_refuse(citations):
            self._trace.append({"step": self._steps, "kind": "refused", "reason": "low_confidence"})
            refusal = prompt_module.REFUSAL_ANSWER
            yield Sources(
                citations=citations, confidence=self._runtime.last_confidence, refused=True
            )
            yield Delta(text=refusal)
            yield Answer(
                text=refusal,
                refused=True,
                citations=citations,
                confidence=self._runtime.last_confidence,
                latency_ms=_elapsed_ms(started),
                extra=self._extra(awaiting=None),
                usage=dict(self._usage),
            )
            return

        yield Sources(
            citations=citations,
            confidence=self._runtime.last_confidence,
            refused=False,
        )
        # ⚠️ 整段一次发（见模块说明：agent 的答案来自 `finish` 的参数，没有逐字过程）。
        yield Delta(text=text)
        yield Answer(
            text=text,
            refused=False,
            citations=citations,
            confidence=self._runtime.last_confidence,
            latency_ms=_elapsed_ms(started),
            extra=self._extra(awaiting=None),
            usage=dict(self._usage),
        )

    async def _fallback(self, reason: str) -> AsyncIterator[AgentEvent]:
        """**回落确定性 pipeline**（`PLAN §13.3` / §13.6 的硬要求）。

        ⚠️ 这是整条 agent 路径的"最坏情况保底"：超预算 / 模型抽风 / 步数用完，
            用户得到的都是**今天这条已经验证过的路**，而不是一个 500。
        ⚠️ **不许静默降级**（本项目一贯口径）：答案末尾明说"本次用了快速模式"。
            藏在日志里的降级，等于让用户以为 agent 一直都在工作。
        """
        self._degraded = reason
        logger.warning("agent 回落 pipeline：%s（已走 %s 步）", reason, self._steps)
        pipeline = self._fallback_factory()
        async for event in pipeline.answer(question=self._question, history=self._history):
            if isinstance(event, Answer):
                # ⚠️ **2026-10-26：不再往正文里加那句"（本次用了快速模式）"。**
                #    原因三条：① 它混在答案里，像答案的一部分；
                #    ② 界面上没有视觉标记，坐席要读到句尾才发现；
                #    ③ 把结构化信息写成了自然语言，想统计回落率还要去正文里 grep。
                #    现在只传 `extra["degraded"]`（已经在），由开头那个角标去告诉用户。
                # ⚠️ **但"不静默降级"这条纪律一字未改** —— 只是把它从正文搬到了结构化字段上。
                yield replace(
                    event,
                    extra={
                        **event.extra,
                        "degraded": reason,
                        "agent_steps": self._steps,
                        "agent_trace": list(self._trace),
                    },
                )
            else:
                yield event

    # ---------- 内部 ----------

    def _build_messages(self) -> list[dict[str, object]]:
        system = _SYSTEM_PROMPT.format(
            finish=FINISH,
            search_kb=SEARCH_KB,
            get_order=GET_ORDER,
            ask_user=ASK_USER,
            question=self._question,
        )
        if self._scratchpad:
            # ⚠️ 只放**事实**（单号/型号/日期），不放上一轮的答案原文 ——
            #    放答案会让模型把上一轮的结论当既成事实复述，而不是重新查。
            facts = json.dumps(self._scratchpad, ensure_ascii=False, indent=2)
            system += f"\n\n【上一轮已确认的事实】（可直接用，不必重查）\n{facts}"
        messages: list[dict[str, object]] = [{"role": "system", "content": system}]
        # 历史只带**用户/助手文本**：agent 的中间步（工具调用）不进下一轮上下文，
        # 否则历史会膨胀得比答案快得多。
        for item in self._history:
            role = item.get("role")
            if role in ("user", "assistant") and item.get("content"):
                messages.append({"role": role, "content": item["content"]})
        return messages

    def _citations(self, refs: Sequence[int]) -> list[Citation]:
        """把 ref 编号还原成引用卡片。**只认 `search_kb` 真的返回过的 ref**。"""
        by_ref = {item.ref: item for item in self._runtime.evidence}
        return [
            Citation(
                index=item.ref,
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                score=item.score,
                snippet=item.snippet,
                score_kind=item.score_kind,
                ordinal=item.ordinal,
            )
            for ref in refs
            if (item := by_ref.get(ref)) is not None
        ]

    def _should_refuse(self, citations: Sequence[Citation]) -> bool:
        """阈值拒答：**沿用 pipeline 那一行判断**，不另立一套。

        ⚠️ 两个例外必须放过，否则会把正确答案拒掉：
            ① 本轮**查过订单**且答案没引用条款（"SF10001 退款处理中"）——
               订单事实就是证据，知识库分数低是正常的；
            ② 引用为空但检索压根没跑过（纯订单题）。
        """
        if not citations:
            return not self._runtime.orders
        hits = [
            RetrievedChunk(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                ordinal=item.ordinal,
                content=item.snippet,
                score=item.score,
            )
            for item in citations
        ]
        score_kind = citations[0].score_kind
        return should_refuse(
            hits,
            self._runtime.last_confidence,
            "none" if score_kind == "fulltext_trgm" else score_kind,
            settings=self._settings,
        )

    def _extra(self, *, awaiting: str | None) -> dict[str, object]:
        # 自愈次数 = 总检索次数 - 1（第一次是应该的，之后的才是重试）。
        self._retries = max(self._runtime.search_count - 1, 0)
        extra: dict[str, object] = {
            "agent_steps": self._steps,
            "agent_retries": self._retries,
            "agent_trace": list(self._trace),
        }
        # 交卷自查的计数与明细。键名与 pipeline 一致，
        # `chat.py` 那边就能直接把它当作 `done` 事件的 `citation_audit` 发出去。
        if self._runtime.last_audit is not None:
            extra["citation_audit"] = self._runtime.last_audit
        # 跨轮事实（第二天那句"那保修呢"就靠它）。
        facts = self._scratchpad_delta()
        if facts:
            extra["scratchpad"] = facts
        if self._degraded:
            extra["degraded"] = self._degraded
        if awaiting is not None:
            extra["awaiting"] = awaiting
        return extra

    def _scratchpad_delta(self) -> dict[str, object]:
        """把这一轮**确认过的事实**提炼出来，存进 `conversations.meta`。

        ⚠️ **只存事实，不存结论**：单号 / 商品 / 签收时间 / 参照时刻。
            存上一轮的**答案原文**是错的 —— 模型会把它当成既成结论复述，
            而不是重新去查（"记得住"和"不动脑子重复"是两回事）。
        ⚠️ 也不存上一轮的检索 query：它是过程，并且把"上一轮怎么问的"带进下一轮会把模型带偏。
        """
        facts: dict[str, object] = {}
        orders = self._runtime.orders
        if orders:
            latest = orders[-1]
            if latest.get("order_no"):
                facts["order_no"] = latest["order_no"]
            if latest.get("delivered_at"):
                facts["delivered_at"] = latest["delivered_at"]
            names = [
                str(entry.get("name", ""))
                for entry in (latest.get("items") or [])
                if isinstance(entry, dict) and entry.get("name")
            ]
            if names:
                facts["items"] = names[:3]
        if self._runtime.evidence:
            facts["effective_on"] = self._runtime.ref_time.value.isoformat()
        return facts

    def _add_usage(self, usage: dict[str, int]) -> None:
        for key, value in usage.items():
            self._usage[key] = self._usage.get(key, 0) + value


def _reject_json(name: str, parse_error: str, raw: str) -> ToolResult:
    """模型把参数写成了非法 JSON → **不执行**，把错误和原文一起回灌让它自己改。"""
    return ToolResult(
        name=name or "unknown",
        ok=False,
        payload={
            "ok": False,
            "error": parse_error,
            "your_arguments": raw[:500],
            "hint": "参数必须是合法的 JSON 对象，重写一次。",
        },
        summary="参数不是合法 JSON",
    )


def _preview(raw: str, *, limit: int = 80) -> str:
    text = " ".join((raw or "").split())
    return text[:limit]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["AgentEvent", "AgentRunner", "ToolEvent"]
