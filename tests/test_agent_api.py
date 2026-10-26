"""agent 模式的 **API 层**测试：SSE `tool` 帧、`messages.meta` 轨迹、scratchpad。

与 `tests/test_agent_loop.py` 的分工：
* 那个文件测**循环本身**（预算 / 回落 / 护栏），不经过 HTTP；
* 这个文件测**接进接口之后**的样子 —— 前端真正拿到的东西（SSE 帧、落库字段）。

⚠️ 全程 `ANSWER_MODE` 用 `monkeypatch` 换，**不动 `.env`**，也不调真模型。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.core.config import Settings
from app.db.models.conversation import Conversation, Message
from app.db.models.order import Order, OrderStatus
from app.rag.providers import ModelAPIError, ToolCall, ToolChatResult
from app.rag.tools import FINISH, GET_ORDER, SEARCH_KB
from tests.test_chat_api import (
    UNIT_VECTOR,
    FakeEmbedder,
    _ask,
    _new_conversation,
    _seed_chunk,
    events_of,
)


class AgentFakeChat:
    """既支持 pipeline 的 `stream`（回落要用），也支持 agent 的 `complete_with_tools`。"""

    def __init__(
        self, turns: Sequence[ToolChatResult | Exception] = (), *, text: str = "快速模式"
    ) -> None:
        self._turns = list(turns)
        self._text = text
        self.tool_calls: list[list[dict[str, Any]]] = []

    async def complete_with_tools(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        *,
        request_timeout: float | None = None,
        tool_choice: str = "auto",
    ) -> ToolChatResult:
        self.tool_calls.append([dict(item) for item in messages])
        if not self._turns:
            raise ModelAPIError("脚本用完了")
        item = self._turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def stream(
        self, messages: Sequence[dict[str, str]], usage: dict[str, int] | None = None
    ) -> AsyncIterator[str]:
        if usage is not None:
            usage.update({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        yield self._text


def _turn(calls: Sequence[ToolCall]) -> ToolChatResult:
    return ToolChatResult(
        content="",
        tool_calls=list(calls),
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        finish_reason="tool_calls",
    )


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=f"c_{name}",
        name=name,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
        arguments=arguments,
    )


def _agent_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    settings = Settings().model_copy(update={"answer_mode": "agent", **overrides})
    monkeypatch.setattr("app.api.chat.get_settings", lambda: settings)
    return settings


async def _last_assistant_message(session: AsyncSession, conversation_id: str) -> Message:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    message = await session.scalar(stmt)
    assert message is not None, "助手消息必须落库"
    return message


# ------------------------------------------------------------
# ① agent 模式的 SSE 形状
# ------------------------------------------------------------


async def test_agent_mode_emits_tool_frames_and_persists_trace(
    client: Any,
    app: Any,
    make_account,
    login,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool` 帧要发、答案要落、轨迹要进 `messages.meta`。"""
    _agent_settings(monkeypatch)
    account = await make_account(label="agentapi")
    await _seed_chunk(
        session, tenant_id=account.tenant_id, content="家具主体保修 2 年。", embedding=UNIT_VECTOR
    )
    chat = AgentFakeChat(
        [
            _turn([_call(SEARCH_KB, {"query": "家具保修多久"})]),
            _turn([_call(FINISH, {"answer": "家具主体保修 2 年。[1]", "citations": [1]})]),
        ]
    )
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    status_code, events = await _ask(client, token, conversation["id"], "家具保修多久")

    assert status_code == 200

    tools = events_of(events, "tool")
    assert [item["name"] for item in tools] == [SEARCH_KB, FINISH]
    assert tools[0]["ok"] is True
    assert isinstance(tools[0]["ms"], int)

    done = events_of(events, "done")[0]
    assert done["refused"] is False

    message = await _last_assistant_message(session, conversation["id"])
    assert "保修 2 年" in message.content
    meta = message.meta or {}
    assert meta["agent_steps"] == 2
    assert isinstance(meta["agent_trace"], list) and meta["agent_trace"]
    assert "degraded" not in meta


async def test_pipeline_mode_has_no_tool_frames_and_no_agent_meta(
    client: Any, app: Any, make_account, login, session: AsyncSession
) -> None:
    """**默认 pipeline 一个字都不变**：没有 `tool` 帧、消息上没有 agent 轨迹。"""
    account = await make_account(label="agentapi")
    await _seed_chunk(
        session, tenant_id=account.tenant_id, content="家具主体保修 2 年。", embedding=UNIT_VECTOR
    )
    chat = AgentFakeChat(
        [_turn([_call(SEARCH_KB, {"query": "不该被调用"})])], text="保修 2 年[1]。"
    )
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    status_code, events = await _ask(client, token, conversation["id"], "家具保修多久")

    assert status_code == 200
    assert events_of(events, "tool") == []
    assert chat.tool_calls == [], "pipeline 模式绝不许走 agent 的循环"
    message = await _last_assistant_message(session, conversation["id"])
    assert message.meta is None


# ------------------------------------------------------------
# ② 回落要看得出来
# ------------------------------------------------------------


async def test_agent_fallback_is_marked_and_recorded(
    client: Any,
    app: Any,
    make_account,
    login,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型炸了 → 回落 pipeline；**答案末尾明说**，`meta.degraded` 记原因。"""
    _agent_settings(monkeypatch)
    account = await make_account(label="agentapi")
    await _seed_chunk(
        session, tenant_id=account.tenant_id, content="家具主体保修 2 年。", embedding=UNIT_VECTOR
    )
    chat = AgentFakeChat([ModelAPIError("炸了")], text="保修 2 年[1]。")
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    _status, events = await _ask(client, token, conversation["id"], "家具保修多久")

    streamed = "".join(item["text"] for item in events_of(events, "delta"))
    assert "（本次用了快速模式）" in streamed

    message = await _last_assistant_message(session, conversation["id"])
    assert "（本次用了快速模式）" in message.content, "落库文本必须与流式看到的一致"
    assert (message.meta or {}).get("degraded") == "模型调用失败"


# ------------------------------------------------------------
# ③ scratchpad 跨轮
# ------------------------------------------------------------


async def test_scratchpad_is_saved_and_reused_next_turn(
    client: Any,
    app: Any,
    make_account,
    login,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一轮查到单号 → 存进 `conversations.meta`；第二轮**句子没有单号**也要带上订单。"""
    _agent_settings(monkeypatch)
    account = await make_account(label="agentapi")
    session.add(
        Order(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            order_no="SF40001",
            status=OrderStatus.DELIVERED,
            placed_at=datetime.now(UTC) - timedelta(days=40),
            delivered_at=datetime.now(UTC) - timedelta(days=10),
            items=[{"name": "云朵单人沙发"}],
            amount_cents=328000,
            currency="CNY",
        )
    )
    await session.commit()

    chat = AgentFakeChat(
        [
            _turn([_call(GET_ORDER, {"order_no": "SF40001"})]),
            _turn([_call(FINISH, {"answer": "SF40001 已签收 10 天。", "citations": []})]),
        ]
    )
    app.dependency_overrides[get_clients] = lambda: (chat, None, None)

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    await _ask(client, token, conversation["id"], "SF40001 到了吗")

    stored = await session.scalar(
        select(Conversation).where(Conversation.id == uuid.UUID(conversation["id"]))
    )
    assert stored is not None
    scratchpad = (stored.meta or {}).get("scratchpad") or {}
    assert scratchpad.get("order_no") == "SF40001"
    assert "云朵单人沙发" in (scratchpad.get("items") or [])
