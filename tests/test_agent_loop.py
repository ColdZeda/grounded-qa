"""agent 单循环（`app/rag/agent.py`）的测试。

⚠️ **不调任何真实模型 API**（`AGENTS §7.1` 约定 9）：对话客户端整只换成
`ScriptedChat` —— 它按脚本吐 `ToolChatResult`，所以"模型走了哪几步"是**可复现**的。
数据库是真的（工具要真查），所以这些用例是 `integration`。

重点验四件事：
1. **交卷**：`finish` 之后能正确还原引用；
2. **预算**：步数用完 / 总预算用完 / 模型炸了 → 一律**回落 pipeline**，且**不静默**；
3. **并行**：一步里的多个 `tool_calls` 只算一步；
4. **护栏**：低置信度仍然拒答，纯订单题不被误拒。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.order import Order, OrderStatus
from app.rag.agent import AgentRunner, ToolEvent
from app.rag.pipeline import Answer, Citation, Delta, Sources
from app.rag.providers import ModelAPIError, ToolCall, ToolChatResult
from app.rag.tools import ASK_USER, FINISH, GET_ORDER, SEARCH_KB
from tests.test_agent_tools import _seed_doc

# ------------------------------------------------------------
# 替身
# ------------------------------------------------------------


def _call(name: str, arguments: dict[str, object], *, call_id: str | None = None) -> ToolCall:
    return ToolCall(
        id=call_id or f"c_{name}_{uuid.uuid4().hex[:6]}",
        name=name,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
        arguments=arguments,
    )


def _model_turn(calls: Sequence[ToolCall] = (), *, content: str = "") -> ToolChatResult:
    return ToolChatResult(
        content=content,
        tool_calls=list(calls),
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        finish_reason="tool_calls" if calls else "stop",
    )


class ScriptedChat:
    """按脚本一条条返回；脚本用完之后**抛错**（那正好测"模型炸了要回落"）。"""

    def __init__(self, turns: Sequence[ToolChatResult | Exception]) -> None:
        self._turns = list(turns)
        #: 每次调用收到的 messages（用来断言"工具结果真的回灌了"）。
        self.seen: list[list[dict[str, object]]] = []
        self.timeouts: list[float | None] = []

    async def complete_with_tools(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
        *,
        usage: dict[str, int] | None = None,
        request_timeout: float | None = None,
        tool_choice: str = "auto",
    ) -> ToolChatResult:
        self.seen.append([dict(item) for item in messages])
        self.timeouts.append(request_timeout)
        assert tools, "循环必须把工具签名传下去，否则模型根本不知道有工具"
        if not self._turns:
            raise ModelAPIError("脚本用完了")
        item = self._turns.pop(0)
        if isinstance(item, Exception):
            raise item
        if usage is not None:
            usage.clear()
            usage.update(item.usage)
            return item
        return item


class FakePipeline:
    """回落目标。**只实现 `answer()`** —— agent 对 pipeline 的全部要求就这些。"""

    def __init__(self, *, text: str = "快速模式的答案", refused: bool = False) -> None:
        self._text = text
        self._refused = refused
        self.called = 0

    def answer(self, *, question: str, history: Sequence[dict[str, str]] = ()) -> AsyncIterator:
        self.called += 1
        text, refused = self._text, self._refused

        async def _gen() -> AsyncIterator[object]:
            yield Sources(citations=[], confidence=None, refused=refused)
            yield Delta(text=text)
            yield Answer(
                text=text,
                refused=refused,
                citations=[],
                confidence=None,
                latency_ms=1,
                extra={},
            )

        return _gen()


class FakeReranker:
    """永远给一个**低分** —— 用来验"低置信度仍然拒答"。"""

    def __init__(self, score: float = 0.05) -> None:
        self._score = score

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int = 5
    ) -> list[tuple[int, float]]:
        return [(index, self._score) for index in range(min(top_n, len(documents)))]


def _settings(**overrides: object) -> Settings:
    base = Settings()
    return base.model_copy(update=overrides) if overrides else base


def _runner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    chat: ScriptedChat,
    question: str,
    pipeline: FakePipeline,
    settings: Settings | None = None,
    reranker: object | None = None,
    user_id: uuid.UUID | None = None,
    scratchpad: dict[str, object] | None = None,
) -> AgentRunner:
    return AgentRunner(
        session=session,
        tenant_id=tenant_id,
        chat=chat,  # type: ignore[arg-type] —— 只用到 complete_with_tools
        question=question,
        fallback=lambda: pipeline,  # type: ignore[arg-type]
        reranker=reranker,  # type: ignore[arg-type]
        settings=settings or _settings(),
        user_id=user_id,
        scratchpad=scratchpad,
    )


async def _drain(runner: AgentRunner) -> list[object]:
    return [event async for event in runner.answer()]


def _answers(events: Sequence[object]) -> list[Answer]:
    return [item for item in events if isinstance(item, Answer)]


def _tool_events(events: Sequence[object]) -> list[ToolEvent]:
    return [item for item in events if isinstance(item, ToolEvent)]


# ------------------------------------------------------------
# ① 交卷
# ------------------------------------------------------------


@pytest.mark.integration
async def test_finish_ends_the_loop_and_restores_citations(
    session: AsyncSession, make_account
) -> None:
    """`search_kb` → `finish(citations=[1])`：答案、引用、轨迹都要落地。"""
    account = await make_account(label="agent")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="09-退换货政策.md",
        content="家具主体保修 2 年，自签收之日起计算。",
        effective_from=date(2026, 1, 1),
    )
    chat = ScriptedChat(
        [
            _model_turn([_call(SEARCH_KB, {"query": "家具保修多久"})]),
            _model_turn([_call(FINISH, {"answer": "家具主体保修 2 年。", "citations": [1]})]),
        ]
    )
    pipeline = FakePipeline()
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="这家具保修多久",
        pipeline=pipeline,
    )

    events = await _drain(runner)
    answers = _answers(events)

    assert pipeline.called == 0, "正常交卷不该回落"
    assert len(answers) == 1
    answer = answers[0]
    assert answer.text == "家具主体保修 2 年。"
    assert answer.refused is False
    assert [item.index for item in answer.citations] == [1]
    assert answer.citations[0].snippet
    assert answer.extra["agent_steps"] == 2
    assert "degraded" not in answer.extra
    assert answer.usage["total_tokens"] == 30

    # 工具结果真的回灌给了模型（否则第二步不可能引用到 1）
    assert any(message.get("role") == "tool" for message in chat.seen[1]), (
        "第一次检索的结果必须出现在第二次调用的上下文里"
    )


@pytest.mark.integration
async def test_parallel_tool_calls_cost_one_step(session: AsyncSession, make_account) -> None:
    """一步里发两个 `tool_calls` ＝ **一步**（并行是为了省步数，不该反过来罚它）。"""
    account = await make_account(label="agent")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="a.md",
        content="家具主体保修 2 年。",
        effective_from=date(2026, 1, 1),
    )
    chat = ScriptedChat(
        [
            _model_turn(
                [
                    _call(SEARCH_KB, {"query": "家具保修多久"}),
                    _call(GET_ORDER, {"order_no": "SF10001"}),
                ]
            ),
            _model_turn([_call(FINISH, {"answer": "保修 2 年。", "citations": [1]})]),
        ]
    )
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="保修多久，顺带看下我的单 SF10001",
        pipeline=FakePipeline(),
    )

    events = await _drain(runner)
    answer = _answers(events)[0]

    assert answer.extra["agent_steps"] == 2
    assert [item.name for item in _tool_events(events)] == [SEARCH_KB, GET_ORDER, FINISH]


# ------------------------------------------------------------
# ② 反问
# ------------------------------------------------------------


@pytest.mark.integration
async def test_ask_user_becomes_the_reply(session: AsyncSession, make_account) -> None:
    """反问：要问的那句话**就是**给用户的回复，并且要落到 `extra["awaiting"]`。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([_model_turn([_call(ASK_USER, {"question": "请问订单号是多少？"})])])
    pipeline = FakePipeline()
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="我退款怎么还没到",
        pipeline=pipeline,
    )

    events = await _drain(runner)
    answer = _answers(events)[0]

    assert pipeline.called == 0
    assert answer.text == "请问订单号是多少？"
    assert answer.extra["awaiting"] == "请问订单号是多少？"
    assert answer.citations == []


# ------------------------------------------------------------
# ③ 预算与回落（**验收标准 5**：必须有测试守住）
# ------------------------------------------------------------


@pytest.mark.integration
async def test_max_steps_falls_back_to_pipeline(session: AsyncSession, make_account) -> None:
    """步数用完 → 回落 pipeline，**并且答案末尾要说明**（不静默降级）。"""
    account = await make_account(label="agent")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="a.md",
        content="家具主体保修 2 年。",
        effective_from=date(2026, 1, 1),
    )
    chat = ScriptedChat([_model_turn([_call(SEARCH_KB, {"query": "保修"})]) for _ in range(5)])
    pipeline = FakePipeline(text="来自 pipeline 的答案")
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="保修多久",
        pipeline=pipeline,
        settings=_settings(agent_max_steps=2),
    )

    events = await _drain(runner)
    answer = _answers(events)[0]

    assert pipeline.called == 1
    # ⚠️ 2026-10-26：那句“（本次用了快速模式）”**不再进正文**，
    #    改成结构化的 `extra["degraded"]` 由界面画角标。
    #    “不静默降级”这条纪律不变，变的只是它出现的地方。
    assert "快速模式" not in answer.text
    assert answer.extra["degraded"] == "步骤用完了"
    assert answer.extra["agent_steps"] == 2
    # 回落时也把 agent 走过的轨迹带上，否则事后查不出"为什么退化"
    assert answer.extra["agent_trace"]


@pytest.mark.integration
async def test_model_error_falls_back_to_pipeline(session: AsyncSession, make_account) -> None:
    """模型 API 炸了 → 回落，不是 500。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([ModelAPIError("连接失败")])
    pipeline = FakePipeline()
    runner = _runner(
        session, tenant_id=account.tenant_id, chat=chat, question="保修多久", pipeline=pipeline
    )

    answer = _answers(await _drain(runner))[0]
    assert pipeline.called == 1
    assert answer.extra["degraded"] == "模型调用失败"


@pytest.mark.integration
async def test_zero_budget_falls_back_immediately(session: AsyncSession, make_account) -> None:
    """总预算极小 → **一次模型都不调**就回落（预算是在调用之前检查的）。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([])
    pipeline = FakePipeline()
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="保修多久",
        pipeline=pipeline,
        settings=_settings(agent_total_budget_seconds=0.0),
    )

    answer = _answers(await _drain(runner))[0]
    assert chat.seen == []
    assert pipeline.called == 1
    assert answer.extra["degraded"] == "总预算用完"


@pytest.mark.integration
async def test_refused_fallback_is_not_decorated(session: AsyncSession, make_account) -> None:
    """pipeline 自己拒答时**不加**"快速模式"那句 —— 否则拒答文案会变得莫名其妙。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([ModelAPIError("炸了")])
    pipeline = FakePipeline(text="这个问题我暂时答不上来", refused=True)
    runner = _runner(
        session, tenant_id=account.tenant_id, chat=chat, question="保修多久", pipeline=pipeline
    )

    answer = _answers(await _drain(runner))[0]
    assert answer.refused is True
    assert "快速模式" not in answer.text


@pytest.mark.integration
async def test_text_only_reply_gets_one_nudge(session: AsyncSession, make_account) -> None:
    """模型光说话不调工具 → 推它一把（下一步还在上下文里），但不无限纵容。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([_model_turn(content="我觉得应该能退"), _model_turn(content="真的能退")])
    pipeline = FakePipeline()
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="能退吗",
        pipeline=pipeline,
        settings=_settings(agent_max_steps=2),
    )

    answer = _answers(await _drain(runner))[0]
    assert pipeline.called == 1
    assert answer.extra["degraded"] == "步骤用完了"
    assert any(
        message.get("role") == "user" and FINISH in str(message.get("content"))
        for message in chat.seen[1]
    ), "第二句之前必须补一句'请调 finish'"


# ------------------------------------------------------------
# ④ 护栏：拒答与"纯订单题"
# ------------------------------------------------------------


@pytest.mark.integration
async def test_low_confidence_finish_still_refuses(session: AsyncSession, make_account) -> None:
    """**下结论这一步不自由**：检索分数不够时，agent 也不许硬答。"""
    account = await make_account(label="agent")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="a.md",
        content="家具主体保修 2 年。",
        effective_from=date(2026, 1, 1),
    )
    chat = ScriptedChat(
        [
            _model_turn([_call(SEARCH_KB, {"query": "家具保修多久"})]),
            _model_turn([_call(FINISH, {"answer": "保修 5 年。", "citations": [1]})]),
        ]
    )
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="保修多久",
        pipeline=FakePipeline(),
        reranker=FakeReranker(0.05),
    )

    answer = _answers(await _drain(runner))[0]
    assert answer.refused is True
    assert "保修 5 年" not in answer.text
    # 拒答也带候选引用：这是 badcase 归因的入口（与 pipeline 口径一致）
    assert answer.citations


@pytest.mark.integration
async def test_order_only_answer_is_not_refused(session: AsyncSession, make_account) -> None:
    """纯订单事实题（"退款到账没"）：**没有条款引用也不算没证据**。"""
    account = await make_account(label="agent")
    session.add(
        Order(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            order_no="SF30001",
            status=OrderStatus.REFUNDING,
            placed_at=datetime.now(UTC) - timedelta(days=2),
            delivered_at=None,
            items=[{"name": "云朵单人沙发"}],
            amount_cents=328000,
            currency="CNY",
        )
    )
    await session.commit()

    chat = ScriptedChat(
        [
            _model_turn([_call(GET_ORDER, {"order_no": "SF30001"})]),
            _model_turn(
                [
                    _call(
                        FINISH,
                        {"answer": "SF30001 退款处理中，预计 3 个工作日到账。", "citations": []},
                    )
                ]
            ),
        ]
    )
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="退款到账没",
        pipeline=FakePipeline(),
    )

    answer = _answers(await _drain(runner))[0]
    assert answer.refused is False
    assert "退款处理中" in answer.text


# ------------------------------------------------------------
# ⑤ 上下文构造
# ------------------------------------------------------------


@pytest.mark.integration
async def test_scratchpad_facts_go_into_the_system_prompt(
    session: AsyncSession, make_account
) -> None:
    """跨轮事实进系统提示的【上一轮已确认的事实】块，且**不进历史消息**。"""
    account = await make_account(label="agent")
    chat = ScriptedChat([_model_turn([_call(ASK_USER, {"question": "是哪一款？"})])])
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="那保修呢",
        pipeline=FakePipeline(),
        scratchpad={"order_no": "SF10001", "model": "云朵单人沙发"},
    )

    await _drain(runner)
    system = chat.seen[0][0]
    assert system["role"] == "system"
    assert "上一轮已确认的事实" in str(system["content"])
    assert "SF10001" in str(system["content"])


@pytest.mark.integration
async def test_tool_args_are_previewed_without_leaking(session: AsyncSession, make_account) -> None:
    """`ToolEvent.args_preview` 给前端显示"在干嘛"，必须**截断**。"""
    account = await make_account(label="agent")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="a.md",
        content="家具主体保修 2 年。",
        effective_from=date(2026, 1, 1),
    )
    long_query = "保修" * 200
    chat = ScriptedChat(
        [
            _model_turn([_call(SEARCH_KB, {"query": long_query})]),
            _model_turn([_call(FINISH, {"answer": "保修 2 年。", "citations": [1]})]),
        ]
    )
    runner = _runner(
        session,
        tenant_id=account.tenant_id,
        chat=chat,
        question="保修多久",
        pipeline=FakePipeline(),
    )

    events = await _drain(runner)
    preview = _tool_events(events)[0].args_preview
    assert len(preview) <= 80


def test_citation_index_equals_ref() -> None:
    """引用编号必须**等于 ref** —— 模型答案里写的是 `[3]`，前端就按 3 去对。"""
    citation = Citation(
        index=3,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=0.9,
        snippet="x",
        score_kind="rerank",
        ordinal=1,
    )
    assert citation.as_dict()["index"] == 3
