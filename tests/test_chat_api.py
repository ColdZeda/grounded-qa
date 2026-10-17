"""问答（SSE 流式）+ 引用溯源 + 拒答（**集成测试**，需要真实数据库）。

⚠️ **绝不真调模型 API**（AGENTS §7.1 约定 9）：烧钱 + 结果不稳定 + 评测失真。
    做法是 `dependency_overrides[get_clients]` 换成假客户端 ——
    这正是 `app/api/chat.py` 把客户端做成依赖的原因。

覆盖的重点（都是"错了就很难发现"的地方）：
    1. **没证据时必须拒答，且不调模型** —— 拒答是确定性代码的判断，不是模型的自由发挥
    2. **跨租户检索隔离** —— 检索是最容易漏 `tenant_id` 的地方（它不走标准 list/get）
    3. **有命中但相似度太低也要拒答** —— 别把"检索到点什么"当成"有证据"
    4. 引用要落库，且编号与提示词一致
    5. **拒答时也要落"我找到了什么 ＋ 在哪个分数上拒的"**（`citations` ＋ `confidence`）——
       否则同一句拒答"**直播有来源、刷新就没了**"，而且连"为什么不知道"都留不下痕迹
       （见 `architecture.md` §9.19）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.core.config import get_settings
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.user import UserRole
from app.rag.prompt import REFUSAL_ANSWER

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix
DIM = get_settings().embedding_dim

#: 一个"和查询向量完全相同"的单位向量 —— 余弦相似度 1.0，必然过阈值。
UNIT_VECTOR = [1.0] + [0.0] * (DIM - 1)
#: 与 `UNIT_VECTOR` **正交** —— 余弦相似度 0.0，必然被拒。
ORTHOGONAL_VECTOR = [0.0, 1.0] + [0.0] * (DIM - 2)


# ------------------------------------------------------------
# 假客户端（绝不联网）
# ------------------------------------------------------------
class FakeChat:
    """假的对话客户端。按 `chunks` 依次吐出，并记录被调用了几次。

    ⚠️ 签名要和真客户端**一致**（`usage=` 关键字）：流水线现在会把一个
        每次回答自己的字典传进来收 token 数。少这个参数，
        所有聊天用例会一起 `TypeError` —— 而且报错点在流水线里，看着像业务 bug。
    """

    def __init__(
        self, chunks: Sequence[str] = ("这是", "一个回答"), *, total_tokens: int = 42
    ) -> None:
        self._chunks = list(chunks)
        self._total_tokens = total_tokens
        self.calls: list[list[dict[str, str]]] = []

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        usage: dict[str, int] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        for piece in self._chunks:
            yield piece
        if usage is not None:
            # 真客户端是"流末尾来一帧 usage"，这里等价地最后写一次
            usage.clear()
            usage.update(
                {
                    "prompt_tokens": 30,
                    "completion_tokens": self._total_tokens - 30,
                    "total_tokens": self._total_tokens,
                }
            )

    @property
    def answer_text(self) -> str:
        return "".join(self._chunks)


class FakeEmbedder:
    """假的 embedding 客户端：**不管输入是什么，都返回同一个向量**。

    ⚠️ 这样测试里"查询向量"是可控的，可以精确构造"相似度够/不够"两种局面。
        真实的 embedding 服务做不到这一点（返回什么取决于模型）。
    """

    def __init__(self, vector: Sequence[float] = UNIT_VECTOR) -> None:
        self._vector = list(vector)
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-embedding"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [list(self._vector) for _ in texts]


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应体拆成 `[(event, data), ...]`。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event = "message"
        data = "{}"
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((event, json.loads(data)))
    return events


def events_of(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [data for event, data in events if event == name]


async def _seed_chunk(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    content: str,
    embedding: Sequence[float] | None = None,
    model: str = "fake-embedding",
) -> Chunk:
    """往知识库里塞一条 chunk（`documents` → `chunks`）。

    ⚠️ `model` 可改是为了测**元数据锁**：造一条"用别的模型嵌入的"块，
        验证向量检索会不会把它当成可比的对象（应该**不会**）。
    """
    document = Document(
        tenant_id=tenant_id,
        filename="faq.md",
        content_type="text/markdown",
        content_hash=uuid.uuid4().hex * 2,  # 64 字符
        status=DocumentStatus.READY,
    )
    session.add(document)
    await session.flush()

    chunk = Chunk(
        tenant_id=tenant_id,
        document_id=document.id,
        ordinal=0,
        content=content,
        embedding=list(embedding) if embedding is not None else None,
        embedding_model=(model if embedding is not None else None),
        embedding_dim=len(embedding) if embedding is not None else None,
    )
    session.add(chunk)
    await session.commit()
    return chunk


async def _new_conversation(client: AsyncClient, token: str, title: str = "测试会话") -> dict:
    resp = await client.post(
        f"{PREFIX}/conversations",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _ask(
    client: AsyncClient, token: str, conversation_id: str, question: str
) -> tuple[int, list[tuple[str, dict[str, Any]]]]:
    async with client.stream(
        "POST",
        f"{PREFIX}/conversations/{conversation_id}/messages",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        body = "".join([piece async for piece in response.aiter_text()])
    return response.status_code, parse_sse(body)


# ------------------------------------------------------------
# 拒答
# ------------------------------------------------------------


async def test_refuses_and_never_calls_the_model_when_kb_is_empty(
    client: AsyncClient, app: Any, make_account, login
) -> None:
    """**空知识库 → 拒答，且一次模型都不调。**

    ⚠️ 这是幻觉治理最核心的一条断言。让模型"自己决定要不要拒答"是不可靠的 ——
        它天然倾向于给一个听起来合理的答案。拒答必须是**调用模型之前**的确定性判断。
    """
    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, None, None)
    account = await make_account()
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    status_code, events = await _ask(client, token, conversation["id"], "你们的退款政策是什么？")

    assert status_code == 200
    sources = events_of(events, "sources")[0]
    assert sources["refused"] is True
    assert sources["citations"] == []

    done = events_of(events, "done")[0]
    assert done["refused"] is True

    # 回答内容就是那段固定文案（逐段流式发出）
    streamed = "".join(d["text"] for d in events_of(events, "delta"))
    assert streamed == REFUSAL_ANSWER

    # ⚠️ **关键断言**：模型一次都没被调用
    assert chat.calls == []


async def test_refuses_when_similarity_is_below_threshold(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession
) -> None:
    """**检索到了，但相似度太低** —— 也要拒答。

    ⚠️ 这条防的是"检索到点什么就当有证据"。全文路能命中（内容确实包含这几个字），
        但向量相似度是 0（正交），说明**语义上根本不相关** → 不能拿来回答。
    """
    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)
    account = await make_account()
    # 内容包含查询词（全文路会命中），但向量与查询正交（相似度 0）
    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="退款政策是另一个话题，这里是完全不相关的内容。",
        embedding=ORTHOGONAL_VECTOR,
    )

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    _status, events = await _ask(client, token, conversation["id"], "退款政策")

    assert events_of(events, "sources")[0]["refused"] is True
    assert chat.calls == []


async def test_refusal_also_persists_citations_and_confidence(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession
) -> None:
    """**拒答时也要把「我找到了什么」和「在哪个分数上拒的」落库。**

    ⚠️ 这条防的是一个很难发现的不一致（`architecture.md` §9.19）：
        改之前 `sources` 事件带着检索到的候选，但落库的 `Answer.citations` 是**空数组** ——
        于是**同一句拒答，看直播有来源、刷新之后来源消失**。

    这两样东西是 badcase 归因与阈值调优的入口：没有它们，每次拒答都只是一句"我不知道"，
    **不留下任何"我为什么不知道"的证据**。
    """
    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)
    account = await make_account()
    # 全文路能命中（内容含查询词）→ hits 非空；但向量正交 → 相似度 0 → 拒答
    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="退款政策是另一个话题，这里是完全不相关的内容。",
        embedding=ORTHOGONAL_VECTOR,
    )
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    _status, events = await _ask(client, token, conversation["id"], "退款政策")

    # ① 直播：sources 带着候选（改之前就是这样）
    sources = events_of(events, "sources")[0]
    assert sources["refused"] is True
    assert len(sources["citations"]) == 1, "拒答之前确实检索到了东西"

    # ② 回放：落库的**也不能是空的** —— 这正是改之前会挂的地方
    resp = await client.get(
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assistant = resp.json()[1]

    assert assistant["refused"] is True
    assert len(assistant["citations"]) == 1, "拒答也要留下'我找到了什么'"
    assert assistant["confidence"] is not None, "拒答也要留下'在哪个分数上拒的'"
    # 是**因为低于阈值**才拒的 —— 这条把"落库"和"拒答判据"钉在一起
    assert assistant["confidence"] < get_settings().answer_min_score


# ------------------------------------------------------------
# 正常回答 + 引用溯源
# ------------------------------------------------------------


async def test_answers_and_emits_citations_when_evidence_exists(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession
) -> None:
    chat = FakeChat(chunks=("签收后 ", "7 天内可以无理由退货[1]。"))
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)
    account = await make_account()
    chunk = await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="本店的退款政策：签收后 7 天内可无理由退货，运费由买家承担。",
        embedding=UNIT_VECTOR,
    )

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    _status, events = await _ask(client, token, conversation["id"], "退款政策")

    sources = events_of(events, "sources")[0]
    assert sources["refused"] is False
    assert len(sources["citations"]) == 1

    citation = sources["citations"][0]
    assert citation["index"] == 1  # 编号从 1 开始，和提示词一致
    assert citation["chunk_id"] == str(chunk.id)
    assert citation["score_kind"] == "vector_cosine"

    assert "".join(d["text"] for d in events_of(events, "delta")) == chat.answer_text
    assert events_of(events, "done")[0]["refused"] is False

    # 模型确实拿到了参考资料（否则就是"无依据硬答"）
    assert chat.calls, "模型应该被调用一次"
    system_prompt = chat.calls[0][0]
    assert system_prompt["role"] == "system"
    assert "签收后 7 天内" in system_prompt["content"]
    assert chat.calls[0][-1] == {"role": "user", "content": "退款政策"}


async def test_citations_and_refused_flag_are_persisted(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession
) -> None:
    """引用与拒答标记**必须落库** —— 否则评测时统计不出拒答率，前端也点不开溯源。"""
    chat = FakeChat(chunks=("有依据的回答[1]",))
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)
    account = await make_account()
    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="产品保修期为两年，自签收之日起算。",
        embedding=UNIT_VECTOR,
    )

    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    await _ask(client, token, conversation["id"], "保修期多久")

    resp = await client.get(
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    messages = resp.json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    assistant = messages[1]
    assert assistant["refused"] is False
    assert len(assistant["citations"]) == 1
    assert assistant["citations"][0]["score_kind"] == "vector_cosine"
    assert assistant["confidence"] is not None
    assert assistant["latency_ms"] is not None

    assert messages[0]["citations"] == []
    # ⚠️ 用户消息**没有置信度可言** —— 这一列的可空性是有含义的，不是漏填
    assert messages[0]["confidence"] is None


# ------------------------------------------------------------
# 隔离
# ------------------------------------------------------------


async def test_retrieval_never_crosses_tenants(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession
) -> None:
    """**租户 A 的知识库，租户 B 一条都不该检索到。**

    ⚠️ 检索是最容易漏 `tenant_id` 的地方：它不走仓储层标准的 `list()` / `get()`，
        而是自己拼了带分数、带排序的查询。漏一个条件 = 把别人的知识库当自己的用。
    """
    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, FakeEmbedder(UNIT_VECTOR), None)

    tenant_a = await make_account(label="knows-the-answer")
    await _seed_chunk(
        session,
        tenant_id=tenant_a.tenant_id,
        content="退款政策：签收后 7 天内可无理由退货。",
        embedding=UNIT_VECTOR,
    )

    tenant_b = await make_account(label="must-not-see-it")
    token_b = await login(tenant_b.email, tenant_b.password)
    conversation_b = await _new_conversation(client, token_b)

    _status, events = await _ask(client, token_b, conversation_b["id"], "退款政策")

    assert events_of(events, "sources")[0]["citations"] == []
    assert events_of(events, "sources")[0]["refused"] is True
    assert chat.calls == []  # 连模型都没调 —— 因为压根没证据
    assert "7 天内" not in json.dumps(events, ensure_ascii=False)


# ------------------------------------------------------------
# 越权与参数校验
# ------------------------------------------------------------


async def test_ask_on_someone_elses_conversation_returns_404(
    client: AsyncClient, app: Any, make_account, login
) -> None:
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    alice = await make_account(label="alice", role=UserRole.MEMBER)
    bob = await make_account(label="bob", role=UserRole.MEMBER)
    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)
    alice_conversation = await _new_conversation(client, alice_token, "alice 的会话")

    async with client.stream(
        "POST",
        f"{PREFIX}/conversations/{alice_conversation['id']}/messages",
        json={"question": "在吗"},
        headers={"Authorization": f"Bearer {bob_token}"},
    ) as response:
        await response.aread()

    assert response.status_code == 404


async def test_empty_question_is_rejected(
    client: AsyncClient, app: Any, make_account, login
) -> None:
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    account = await make_account()
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    async with client.stream(
        "POST",
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        json={"question": "   "},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        await response.aread()

    assert response.status_code == 422


async def test_overlong_question_is_rejected(
    client: AsyncClient, app: Any, make_account, login
) -> None:
    """超长"问题"必须在入口挡住 —— 它会把 prompt 撑爆并把 token 账单拉满。"""
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    account = await make_account()
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)

    async with client.stream(
        "POST",
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        json={"question": "问" * 5000},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        await response.aread()

    assert response.status_code == 422
