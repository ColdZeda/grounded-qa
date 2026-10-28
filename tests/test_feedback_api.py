"""纠错反馈的接口测试（`app/api/feedback.py`）。

⚠️ 这里只验**边界与权限**，不验"界面长什么样"。四条重点：
1. 快照是**服务端取**的（前端伪造不了）；
2. 同一个人重复提交是**覆盖**，不是堆成多条；
3. 只能对**助手**消息提；
4. 越权一律 **404**（不是 403）。

后半段（W22 / D11）是**「采纳入库」的草稿 → 人工发布**，验四件事：
① 普通成员进不去；② 草稿**不参与检索**；③ 发布后**立刻**能被检索到；④ 全程留操作人。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.core.config import get_settings
from app.db.models.conversation import Message, MessageRole
from app.db.models.feedback import AnswerFeedback
from app.db.models.knowledge import Chunk, ContentStatus, Document, DocumentStatus
from app.db.models.user import UserRole
from app.rag.retrieval import HybridRetriever
from tests.test_chat_api import _new_conversation

PREFIX = get_settings().api_prefix
FEEDBACK = f"{PREFIX}/feedback"

#: 草稿正文里放一个**别处不可能出现**的代号，用来做"这份文档被检索到了吗"的判据。
#: ⚠️ 不用"退货"这种词 —— 它到处都是，检索命中了也说明不了是哪一份。
MARKER = "QINGFENG-7"


class FakeEmbedder:
    """假 embedding 客户端（约定 9：测试不许真调模型 API）。"""

    model = "fake-embedding"
    dim = 1024

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.01] * self.dim for _ in texts]


@pytest.fixture
def with_models(app):
    """把模型客户端换成假的（采纳要用 embedding 切块入库）。"""
    app.dependency_overrides[get_clients] = lambda: (None, FakeEmbedder(), None)
    return app


async def _seed_answer(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, text: str
) -> Message:
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content=text,
        citations=[
            {
                "index": 1,
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "score": 0.91,
                "score_kind": "rerank",
                "snippet": "家具主体保修 2 年",
                "ordinal": 0,
            }
        ],
        refused=False,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def _seed_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    text: str,
    content_status: ContentStatus = ContentStatus.ACTIVE,
) -> Document:
    """直接塞一篇**已入库**的文档（含一块），给冲突体检与检索当背景。"""
    document = Document(
        tenant_id=tenant_id,
        filename=filename,
        content_type="text/markdown",
        size_bytes=len(text.encode("utf-8")),
        content_hash=uuid.uuid4().hex * 2,
        status=DocumentStatus.READY,
        content_status=content_status,
    )
    session.add(document)
    await session.flush()
    session.add(Chunk(tenant_id=tenant_id, document_id=document.id, ordinal=0, content=text))
    await session.commit()
    await session.refresh(document)
    return document


async def _search(session: AsyncSession, tenant_id: uuid.UUID, query: str) -> list[uuid.UUID]:
    """只用全文路检索一次（不调任何模型）。返回命中的文档 id。"""
    retriever = HybridRetriever(session, tenant_id=tenant_id, scope="current")
    hits, _ = await retriever.search(
        query=query,
        query_embedding=None,
        top_k=5,
        rrf_k=60,
        fulltext_min_similarity=0.0,
    )
    return [hit.document_id for hit in hits]


async def _adopt(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    feedback_id: uuid.UUID,
    content: str,
) -> dict[str, Any]:
    resp = await client.post(
        f"{FEEDBACK}/{feedback_id}/adopt",
        json={"title": "退货期限（核对稿）", "content": content},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.integration
async def test_feedback_stores_a_snapshot_on_the_server(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """提交反馈 → 快照由服务端取，工时端改不了。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="家具主体保修 2 年[1]。",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer", "note": "应该是 3 年"},
        headers=account.auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["verdict"] == "wrong_answer"
    assert body["answer_snapshot"] == "家具主体保修 2 年[1]。"
    assert body["citations_snapshot"][0]["score_kind"] == "rerank"


@pytest.mark.integration
async def test_second_feedback_creates_a_new_row(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """同一个人对同一条消息报两次 → **两条记录**（2026-10-28 改的口径）。

    ⚠️ 以前是"同一人 + 同一消息唯一，重复提交覆盖"。但同一条回答可以有不同问题
        （答错一处、又引错一处）；更要紧的是：一条**已经采纳/发布**的反馈被后来一次
        误点改写，`adopted_at` / `published_at` 的留痕就对不上了。
        防误点交给前端（提交后短时禁用 +「再报一次」），后端不再替人吞证据。
    """
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="x",
    )
    payload = {"message_id": str(message.id), "verdict": "wrong_answer"}

    first = await client.post(FEEDBACK, json=payload, headers=account.auth(token))
    assert first.status_code == 201
    second = await client.post(
        FEEDBACK,
        json={**payload, "verdict": "wrong_citation", "note": "串号了"},
        headers=account.auth(token),
    )
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"], "第二次上报必须是新的一条"

    rows = (
        await session.scalars(select(AnswerFeedback).where(AnswerFeedback.message_id == message.id))
    ).all()
    assert len(rows) == 2


@pytest.mark.integration
async def test_only_assistant_messages_are_accepted(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """对**用户自己**的消息提反馈 → 422（那不是在纠错）。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    user_message = Message(
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        role=MessageRole.USER,
        content="保修多久",
        citations=[],
        refused=False,
    )
    session.add(user_message)
    await session.commit()

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(user_message.id), "verdict": "wrong_answer"},
        headers=account.auth(token),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_other_peoples_message_is_404(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """**别人会话里的消息 → 404 而不是 403**（403 等于承认它存在，可被枚举）。"""
    owner = await make_account(label="fb-a")
    other = await make_account(label="fb-b")
    owner_token = await login(owner.email, owner.password)
    other_token = await login(other.email, other.password)
    conversation = await _new_conversation(client, owner_token)
    message = await _seed_answer(
        session,
        tenant_id=owner.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="y",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer"},
        headers=other.auth(other_token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
async def test_other_requires_a_note(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """选「其他」必须写一句 —— 否则这条记录对未来毫无用处。"""
    account = await make_account(label="fb")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="z",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "other"},
        headers=account.auth(token),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_list_is_admin_only(client: Any, make_account, login) -> None:
    """清单只给管理员（那是治理清单，不是个人收藏夹）。"""
    admin = await make_account(label="fb-admin", role=UserRole.ADMIN)
    member = await make_account(label="fb-member", role=UserRole.MEMBER)
    admin_token = await login(admin.email, admin.password)
    member_token = await login(member.email, member.password)

    ok = await client.get(FEEDBACK, headers=admin.auth(admin_token))
    assert ok.status_code == 200
    denied = await client.get(FEEDBACK, headers=member.auth(member_token))
    assert denied.status_code == 403


# ------------------------------------------------------------
# 快照要含"客户问的那句" ＋ 原会话；清单可按类过滤（2026-10-28 D2/D3）
# ------------------------------------------------------------
async def _seed_question(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, text: str
) -> Message:
    """在助手回答**之前**塞一条用户提问（提问的时间早于答案）。"""
    question = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=text,
    )
    session.add(question)
    await session.commit()
    await session.refresh(question)
    return question


@pytest.mark.integration
async def test_feedback_snapshots_the_question_and_conversation(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """快照里要有**客户问的那一句** ＋ 会话 id。

    ⚠️ 只存答案是判不了的：很多"答错了"其实是问题本身不清楚，或者上一轮的上下文没带上。
        看不到提问，三个月后就只剩一句"这里错了"（那就等于没有记录）。
    """
    account = await make_account(label="fb-q")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    conversation_id = uuid.UUID(conversation["id"])
    await _seed_question(
        session, tenant_id=account.tenant_id, conversation_id=conversation_id, text="保修多久？"
    )
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=conversation_id,
        text="家具主体 2 年。",
    )

    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer", "note": "要分部件"},
        headers=account.auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["question_snapshot"] == "保修多久？"
    assert body["conversation_id"] == str(conversation_id)


@pytest.mark.integration
async def test_list_feedback_can_filter_by_verdict(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """按类分组（D2）：`?verdict=` 只回那一类，三种处理动作本来就不一样。"""
    admin = await make_account(label="fb-group", role=UserRole.ADMIN)
    token = await login(admin.email, admin.password)
    headers = admin.auth(token)
    conversation = await _new_conversation(client, token)
    conversation_id = uuid.UUID(conversation["id"])

    created: dict[str, uuid.UUID] = {}
    for verdict in ("wrong_answer", "wrong_citation"):
        message = await _seed_answer(
            session,
            tenant_id=admin.tenant_id,
            conversation_id=conversation_id,
            text=f"{verdict} 的答案",
        )
        resp = await client.post(
            FEEDBACK,
            json={"message_id": str(message.id), "verdict": verdict},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        created[verdict] = uuid.UUID(resp.json()["id"])

    only_answer = await client.get(f"{FEEDBACK}?verdict=wrong_answer", headers=headers)
    assert only_answer.status_code == 200
    ids = [row["id"] for row in only_answer.json()]
    assert ids == [str(created["wrong_answer"])]

    both = await client.get(FEEDBACK, headers=headers)
    assert len(both.json()) == 2


# ------------------------------------------------------------
# 采纳入库：草稿 → 人工发布（W22 / D11）
# ------------------------------------------------------------
async def _account_with_feedback(client, make_account, login, session, text: str):
    """建一个管理员账号 ＋ 一条已存在的反馈，返回 (headers, account, feedback)。"""
    account = await make_account(label="adopt")
    token = await login(account.email, account.password)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=account.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text=text,
    )
    resp = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "wrong_answer", "note": "应为 15 天"},
        headers=account.auth(token),
    )
    assert resp.status_code == 201, resp.text
    return account.auth(token), account, resp.json()


@pytest.mark.integration
async def test_admin_can_delete_feedback_but_member_cannot(
    client: Any, make_account, login, session: AsyncSession
) -> None:
    """反馈是**治理清单**：假阳性要能清掉，但只有管理员能清（2026-10-28 用户提）。

    ⚠️ 为什么普通坐席不能删：他能**提交**（那是他的判断），但不能把记录**抹掉** ——
        否则"这条我报错了"可以被当事人撤回，badcase 就不可追溯了。
    ⚠️ 另一个租户的管理员来删 → **404**（不是 403，约定 14）。
    """
    admin = await make_account(label="fb-del", role=UserRole.ADMIN)
    token = await login(admin.email, admin.password)
    headers = admin.auth(token)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=admin.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="这条是误报",
    )
    created = await client.post(
        FEEDBACK,
        json={"message_id": str(message.id), "verdict": "other", "note": "误点"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    feedback_id = uuid.UUID(created.json()["id"])

    member = await make_account(label="fb-del-member", role=UserRole.MEMBER)
    member_token = await login(member.email, member.password)
    denied = await client.delete(f"{FEEDBACK}/{feedback_id}", headers=member.auth(member_token))
    assert denied.status_code == 403

    stranger = await make_account(label="fb-del-stranger", role=UserRole.ADMIN)
    stranger_token = await login(stranger.email, stranger.password)
    missing = await client.delete(
        f"{FEEDBACK}/{feedback_id}", headers=stranger.auth(stranger_token)
    )
    assert missing.status_code == 404

    ok = await client.delete(f"{FEEDBACK}/{feedback_id}", headers=headers)
    assert ok.status_code == 204
    left = (
        await session.scalars(select(AnswerFeedback).where(AnswerFeedback.id == feedback_id))
    ).all()
    assert left == []


@pytest.mark.integration
async def test_draft_holds_only_the_corrected_text(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """草稿正文**只放"更正后的说法"** —— 内部信息不许跟着进知识库（2026-10-28 走查）。

    ⚠️ 以前草稿里还有三段：顶上一句"由纠错反馈采纳生成…（反馈 id）"、`## 坐席指出的问题`
        （坐席的吐槽）、`## 依据`（引用摘要 + **chunk id**）。它们会跟着正文一起被切块、进检索 ——
        顾客问"保修多久"，命中的可能是「应说明分部件」或者一串 UUID。
    ⚠️ 正文里模型写的行内编号 `[1]` 也要洗掉：那是"刚才那次回答"的编号，进了知识库没有对齐对象。
    """
    headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 2 年[1]。"
    )
    created = await _adopt(
        client,
        headers=headers,
        feedback_id=uuid.UUID(feedback["id"]),
        content=f"家具主体保修 2 年，布艺 6 个月[1]。{MARKER}",
    )
    assert created["document_name"].startswith("feedback/")

    draft = await client.get(f"{FEEDBACK}/{feedback['id']}/draft", headers=headers)
    assert draft.status_code == 200, draft.text
    body = draft.json()["content"]

    assert MARKER in body, "更正后的说法必须在"
    assert "[1]" not in body, "行内编号要洗掉（它在知识库里无所指）"
    assert "坐席指出的问题" not in body, "坐席的吐槽是内部信息，不进知识库"
    assert "依据" not in body, "引用摘要 / chunk id 不进知识库正文"


@pytest.mark.integration
async def test_member_cannot_adopt_or_publish(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """① 普通成员进不去：三个写/读草稿的接口都要拦。

    ⚠️ 这里要的是 **403**（"你登录了但角色不够"），不是 404 ——
        集合类接口上没有"某条资源存不存在"可泄漏。
    """
    _headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    member = await make_account(label="adopt-member", role=UserRole.MEMBER)
    member_token = await login(member.email, member.password)
    weak = member.auth(member_token)

    for method, path, body in (
        ("post", f"{FEEDBACK}/{feedback['id']}/adopt", {"content": "x"}),
        ("get", f"{FEEDBACK}/{feedback['id']}/draft", None),
        ("post", f"{FEEDBACK}/{feedback['id']}/publish", {}),
    ):
        if method == "post":
            resp = await client.post(path, json=body, headers=weak)
        else:
            resp = await client.get(path, headers=weak)
        assert resp.status_code == 403, f"{path} 应该 403，实际 {resp.status_code}"


@pytest.mark.integration
async def test_adopt_creates_a_draft_that_retrieval_cannot_see(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """② 草稿**不参与检索** —— 这是"先人工确认再入库"的全部意义。

    ⚠️ 断言走**真的检索**（全文路，不调模型），不是只看 `content_status` 字段：
        那个字段只是手段，"搜不到"才是要的结果。想看字段的测试挡不住
        "有人在召回 SQL 里把 draft 放回来"这种改动。
    """
    headers, account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    body = await _adopt(
        client,
        headers=headers,
        feedback_id=uuid.UUID(feedback["id"]),
        content=f"无理由退货：签收后 15 天内可申请，代号 {MARKER}。",
    )
    assert body["content_status"] == "draft"
    assert body["chunks"] >= 1

    draft_id = uuid.UUID(body["document_id"])
    hits = await _search(session, account.tenant_id, MARKER)
    assert draft_id not in hits, "草稿被检索到了 —— 等于未经审阅就入库"
    # 反过来也要能证明"搜索本身是好使的"，否则上面那句断言可能只是因为搜不到任何东西
    active = await _seed_document(
        session,
        tenant_id=account.tenant_id,
        filename="policies/已发布.md",
        text=f"已发布口径：无理由退货签收后 7 天内可申请，代号 {MARKER}。",
    )
    assert active.id in await _search(session, account.tenant_id, MARKER)


@pytest.mark.integration
async def test_publish_makes_the_draft_retrievable_immediately(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """③ 发布后**立刻**能检索到（块与向量在采纳时就生成好了）。"""
    headers, account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    body = await _adopt(
        client,
        headers=headers,
        feedback_id=uuid.UUID(feedback["id"]),
        content=f"定制商品不适用无理由退货，代号 {MARKER}。",
    )
    draft_id = uuid.UUID(body["document_id"])

    resp = await client.post(f"{FEEDBACK}/{feedback['id']}/publish", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["content_status"] == "active"
    assert draft_id in await _search(session, account.tenant_id, MARKER)


@pytest.mark.integration
async def test_publish_is_recorded_with_the_operator(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """④ 全程留操作人：采纳人 / 发布时间都要能从清单里看到（不是只在数据库里）。"""
    admin = await make_account(label="adopt-trail", role=UserRole.ADMIN)
    token = await login(admin.email, admin.password)
    headers = admin.auth(token)
    conversation = await _new_conversation(client, token)
    message = await _seed_answer(
        session,
        tenant_id=admin.tenant_id,
        conversation_id=uuid.UUID(conversation["id"]),
        text="家具主体保修 3 年[1]。",
    )
    feedback = (
        await client.post(
            FEEDBACK,
            json={"message_id": str(message.id), "verdict": "wrong_answer"},
            headers=headers,
        )
    ).json()

    await _adopt(
        client, headers=headers, feedback_id=uuid.UUID(feedback["id"]), content="保修 2 年。"
    )
    await client.post(f"{FEEDBACK}/{feedback['id']}/publish", json={}, headers=headers)

    listing = (await client.get(FEEDBACK, headers=headers)).json()
    row = next(item for item in listing if item["id"] == feedback["id"])
    assert row["adopted_by_email"] == admin.email
    assert row["published_by_email"] == admin.email
    assert row["adopted_at"] is not None
    assert row["published_at"] is not None
    assert row["adopted_document_status"] == "active"


@pytest.mark.integration
async def test_second_adopt_is_rejected(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """同一条反馈只采纳一次 —— 重复点会造出两份内容相同的知识（而且以后各改各的）。"""
    headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    await _adopt(
        client, headers=headers, feedback_id=uuid.UUID(feedback["id"]), content="保修 2 年。"
    )
    again = await client.post(
        f"{FEEDBACK}/{feedback['id']}/adopt", json={"content": "保修 2 年。"}, headers=headers
    )
    assert again.status_code == 409


@pytest.mark.integration
async def test_draft_can_be_read_before_publishing(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """审阅入口：草稿不进检索，所以必须能从这里读到全文。"""
    headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    await _adopt(
        client,
        headers=headers,
        feedback_id=uuid.UUID(feedback["id"]),
        content="家具主体保修 2 年。",
    )
    resp = await client.get(f"{FEEDBACK}/{feedback['id']}/draft", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "家具主体保修 2 年。" in body["content"]
    assert body["content_status"] == "draft"
    # ⚠️ 原答案（3 年）不许出现在正文里：草稿是要进知识库的，
    #    留着"我原来答错了"这种话会被检索出来引给顾客。
    assert "3 年" not in body["content"]


@pytest.mark.integration
async def test_publish_blocks_on_conflict_unless_forced(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """发布前体检：新口径和现行口径数字打架 → **默认拒绝发布**。

    ⚠️ 这是"体检必须在发布之前"的那条：发布完再检查，文档已经能被真实用户搜到了。
    """
    headers, account, feedback = await _account_with_feedback(
        client, make_account, login, session, "无理由退货 15 天[1]。"
    )
    await _seed_document(
        session,
        tenant_id=account.tenant_id,
        filename="policies/退换货总则.md",
        text="# 退换货总则\n\n无理由退货：签收后 7 天内可申请。\n",
    )
    body = await _adopt(
        client,
        headers=headers,
        feedback_id=uuid.UUID(feedback["id"]),
        content="无理由退货：签收后 15 天内可申请。",
    )
    draft_id = uuid.UUID(body["document_id"])

    blocked = await client.post(f"{FEEDBACK}/{feedback['id']}/publish", json={}, headers=headers)
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["conflicts"], "409 里必须带上冲突原文，否则人没法判断"
    assert detail["conflicts"][0]["topic"] == "退货期限"

    forced = await client.post(
        f"{FEEDBACK}/{feedback['id']}/publish", json={"force": True}, headers=headers
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True
    assert draft_id in await _search(session, account.tenant_id, "无理由退货")


@pytest.mark.integration
async def test_publish_twice_is_rejected(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """发布是**幂等失败**，不是重复成功 —— 重放一次不该悄悄再改一次时间。"""
    headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    await _adopt(
        client, headers=headers, feedback_id=uuid.UUID(feedback["id"]), content="保修 2 年。"
    )
    first = await client.post(f"{FEEDBACK}/{feedback['id']}/publish", json={}, headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post(f"{FEEDBACK}/{feedback['id']}/publish", json={}, headers=headers)
    assert second.status_code == 409


@pytest.mark.integration
async def test_another_tenants_feedback_is_404(
    client: AsyncClient, with_models, make_account, login, session: AsyncSession
) -> None:
    """别的租户的反馈：**404**（403 等于承认"这条反馈存在"）。"""
    _headers, _account, feedback = await _account_with_feedback(
        client, make_account, login, session, "家具主体保修 3 年[1]。"
    )
    other = await make_account(label="adopt-other")
    other_token = await login(other.email, other.password)

    resp = await client.post(
        f"{FEEDBACK}/{feedback['id']}/adopt",
        json={"content": "x"},
        headers=other.auth(other_token),
    )
    assert resp.status_code == 404
