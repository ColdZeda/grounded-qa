"""答案纠错反馈的接口。

⚠️ **三种角色看到的不是同一件事**：
* 坐席（任何登录用户）：能对**自己有权访问的会话**里的助手消息提一条反馈；
* 管理员：能看**本租户全部**反馈（这是"治理清单"，不是个人收藏夹）；
* 管理员（非演示账号）：能**采纳**成草稿、**发布**进知识库（D11）。

⚠️ **不做点赞**：点踩只产生一个数字，对治理没有用；这里要的是"哪里不对 + 当时的答案"。

⚠️ **「采纳」不是「自动写库」**（W23 记着没做）：一条反馈变成知识库内容要过两道人的关 ——
    先由管理员**改写**成正确说法（草稿），再由他**看过体检结果**点发布。
    自动写库的代价是"错的被写进去、而且没人知道是谁让写的"，那比不写更糟。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.chat import ClientsDep
from app.api.deps import AdminWriter, CurrentUser, SessionDep, require_admin
from app.core.config import get_settings
from app.db.models.conversation import Conversation, Message, MessageRole
from app.db.models.feedback import AnswerFeedback, FeedbackVerdict
from app.db.models.knowledge import Chunk, ContentStatus, Document, DocumentStatus
from app.db.models.user import User
from app.db.repository import TenantScopedRepository
from app.kb.conflicts import build_report, collect
from app.kb.ingest import ingest_bytes
from app.schemas.feedback import (
    AdoptIn,
    AdoptOut,
    DraftOut,
    FeedbackAdminOut,
    FeedbackIn,
    FeedbackOut,
    PublishIn,
    PublishOut,
)

router = APIRouter(prefix="/feedback", tags=["feedback"])

#: 草稿文档的文件名前缀。
#: ⚠️ **刻意不用 `upload/`**：`upload/` 是"人传上来的原件"，`feedback/` 是"从 badcase 长出来的"。
#:    两者混在一起后，"这条知识是谁加的、依据是什么"就查不回来了。
_DRAFT_PREFIX = "feedback/"


async def _load_message(session: SessionDep, user: CurrentUser, message_id: uuid.UUID) -> Message:
    """取出这条消息，并确认**当前用户有权访问它所在的会话**。

    ⚠️ 越权一律 404（`AGENTS §7.1` 约定 14）—— 403 等于承认"这条消息存在"。
    """
    message = await session.get(Message, message_id)
    if message is None or message.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is None or conversation.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    from app.db.models.user import UserRole

    if user.role is not UserRole.ADMIN and conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    return message


@router.post(
    "",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    summary="对一条助手回答提交纠错反馈",
)
async def create_feedback(
    payload: FeedbackIn,
    session: SessionDep,
    current_user: CurrentUser,
) -> AnswerFeedback:
    """提交纠错。**同一个人对同一条消息重复提交 = 覆盖**（不堆成多条 badcase）。

    ⚠️ 快照（答案原文 + 引用）在**这里**存，不是在前端传 ——
        前端传的话，改一行请求就能伪造一条"当时答案是 xxx"的记录。
    """
    message = await _load_message(session, current_user, payload.message_id)
    if message.role is not MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="只能对助手回答提反馈",
        )
    if payload.verdict is FeedbackVerdict.OTHER and not (payload.note or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="选「其他」时请写一句说明",
        )

    existing = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.message_id == message.id,
            AnswerFeedback.user_id == current_user.id,
        )
    )
    question = await _question_before(session, message)
    if existing is not None:
        # 覆盖：同一个人改了主意，不该留两条互相矛盾的说法
        existing.verdict = payload.verdict
        existing.note = (payload.note or "").strip() or None
        existing.answer_snapshot = message.content
        existing.citations_snapshot = list(message.citations or [])
        existing.question_snapshot = question
        existing.conversation_id = message.conversation_id
        await session.commit()
        await session.refresh(existing)
        return existing

    feedback = AnswerFeedback(
        tenant_id=current_user.tenant_id,
        message_id=message.id,
        user_id=current_user.id,
        verdict=payload.verdict,
        note=(payload.note or "").strip() or None,
        answer_snapshot=message.content,
        # ⚠️ 提问也一起冻住（2026-10-28 D3）：只给答案，很多"错"根本判不了 ——
        #    可能是问得不清楚、也可能是上一轮的上下文没带上。
        question_snapshot=question,
        conversation_id=message.conversation_id,
        citations_snapshot=list(message.citations or []),
    )
    session.add(feedback)
    await session.commit()
    await session.refresh(feedback)
    return feedback


async def _question_before(session: SessionDep, message: Message) -> str | None:
    """取这条助手回答**对应的那句提问**（同一会话里，它之前最近的一条 user）。

    ⚠️ 用 `created_at <= 助手消息` 而不是"往前数一条"：中间可能插进系统提示或另一轮
        并发问答，按角色找更稳。
    ⚠️ 找不到就返回 `None`（老数据 / 奇怪的会话），界面上会明说"没存到提问"。
    """
    stmt = (
        select(Message.content)
        .where(
            Message.tenant_id == message.tenant_id,
            Message.conversation_id == message.conversation_id,
            Message.role == MessageRole.USER,
            Message.created_at <= message.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


@router.get(
    "",
    response_model=list[FeedbackAdminOut],
    summary="本租户的纠错反馈清单（仅管理员）",
    dependencies=[Depends(require_admin)],
)
async def list_feedback(
    session: SessionDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    verdict: Annotated[FeedbackVerdict | None, Query()] = None,
) -> list[FeedbackAdminOut]:
    """按时间倒序的反馈清单。

    ⚠️ **只用 `TenantScopedRepository`**（约定 12）：这张表里的每一行都带租户，
        忘了加 `WHERE tenant_id` 在这里等于把别家的问题清单给出去。
    ⚠️ `verdict` 过滤是给**按类分组**用的（2026-10-28 D2）：三种"哪里不对"的处理方式
        本来就不一样（答案错 → 改内容；引用错 → 看串号；其他 → 先读），
        混成一条流之后，管理员每次都要自己挑。分页在**组内**做（`limit` 逐组递增）。
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=AnswerFeedback)
    stmt = repo.select()
    if verdict is not None:
        stmt = stmt.where(AnswerFeedback.verdict == verdict)
    stmt = stmt.order_by(AnswerFeedback.created_at.desc()).limit(limit)
    return await _decorate(
        session, current_user.tenant_id, list((await session.scalars(stmt)).all())
    )


async def _decorate(
    session: SessionDep,
    tenant_id: uuid.UUID,
    rows: list[AnswerFeedback],
) -> list[FeedbackAdminOut]:
    """补上"谁做的 + 草稿什么状态"。

    ⚠️ 两次批量查询（用户、文档）而不是每条各查一次：50 条清单 = 1 + 2 次 SQL，
        不是 1 + 100 次。列表页慢起来是最难被注意到的那类退化。
    """
    user_ids = {row.adopted_by for row in rows} | {row.published_by for row in rows}
    user_ids.discard(None)
    emails: dict[uuid.UUID, str] = {}
    if user_ids:
        users = (
            await session.scalars(
                select(User).where(User.tenant_id == tenant_id, User.id.in_(user_ids))
            )
        ).all()
        emails = {user.id: user.email for user in users}

    doc_ids = {row.adopted_document_id for row in rows}
    doc_ids.discard(None)
    documents: dict[uuid.UUID, Document] = {}
    if doc_ids:
        found = (
            await session.scalars(
                select(Document).where(Document.tenant_id == tenant_id, Document.id.in_(doc_ids))
            )
        ).all()
        documents = {doc.id: doc for doc in found}

    out: list[FeedbackAdminOut] = []
    for row in rows:
        document = documents.get(row.adopted_document_id) if row.adopted_document_id else None
        out.append(
            FeedbackAdminOut(
                **FeedbackOut.model_validate(row).model_dump(),
                adopted_by_email=emails.get(row.adopted_by) if row.adopted_by else None,
                published_by_email=emails.get(row.published_by) if row.published_by else None,
                adopted_document_status=document.content_status.value if document else None,
                adopted_document_name=document.filename if document else None,
            )
        )
    return out


# ------------------------------------------------------------
# 「采纳入库」：草稿 → 人工发布（D11）
# ------------------------------------------------------------
async def _load_feedback(
    session: SessionDep, user: CurrentUser, feedback_id: uuid.UUID
) -> AnswerFeedback:
    """按租户取一条反馈。越权/不存在一律 404（约定 14）。"""
    feedback = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.id == feedback_id,
            AnswerFeedback.tenant_id == user.tenant_id,
        )
    )
    if feedback is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="反馈不存在")
    return feedback


def _guess_title(feedback: AnswerFeedback) -> str:
    """没给标题时，从原答案的第一句里猜一个 —— 有标题才像一页知识，没标题像一段聊天记录。"""
    first = (feedback.answer_snapshot or "").strip().splitlines()
    head = first[0] if first else "纠错采纳"
    head = head.strip("#* >").strip()
    return (head[:40] or "纠错采纳").rstrip("。，；：,.;:")


def _draft_markdown(feedback: AnswerFeedback, *, title: str, content: str) -> str:
    """把一条反馈拼成一页 markdown。

    ⚠️ **不把"原答案"和"更正后的说法"混成一段**：草稿是要进知识库的，
        里面留着"我原来答错了"这种话，会被检索出来引给顾客。
        所以原答案只作为**引用来源**出现（`## 依据`），正文只有更正后的说法。
    """
    sources = []
    for index, cite in enumerate(feedback.citations_snapshot or [], start=1):
        snippet = str(cite.get("snippet") or "").replace("\n", " ")[:120]
        document = str(cite.get("document") or cite.get("document_id") or "")
        sources.append(f"- [{index}] {snippet}" + (f"（`{document}`）" if document else ""))
    if not sources:
        sources.append("- （原始回答没有引用来源，请人工补上依据）")

    parts = [
        f"# {title}",
        "",
        "> 由纠错反馈采纳生成，**已人工审阅后发布**。"
        f"口径源自反馈 `{feedback.id.hex[:8]}`（{feedback.verdict.value}）。",
        "",
        "## 结论",
        "",
        content.strip(),
        "",
    ]
    if feedback.note:
        parts += ["## 坐席指出的问题", "", feedback.note.strip(), ""]
    parts += ["## 依据", "", *sources, ""]
    return "\n".join(parts)


@router.post(
    "/{feedback_id}/adopt",
    response_model=AdoptOut,
    status_code=status.HTTP_201_CREATED,
    summary="采纳一条反馈 → 生成**草稿**文档（不进检索）",
)
async def adopt_feedback(
    feedback_id: uuid.UUID,
    payload: AdoptIn,
    session: SessionDep,
    current_admin: AdminWriter,
    clients: ClientsDep,
) -> AdoptOut:
    """把 badcase 变成一份**还没进检索**的草稿（D11：草稿 → 人工发布）。

    ⚠️ **草稿在这一行被建出来时就是 `draft`**（`content_status` 覆盖，见 `prepare_document`）——
        不能"先 active 再改成 draft"，那中间有一个能被真实用户搜到的窗口。

    ⚠️ **块与向量现在就生成好**：发布是翻一个枚举，不是重新入库。
        否则"发布后立刻能搜到"就变成"发布后过一会儿（取决于队列）才能搜到"，
        而验收时要的正是**立刻**。
    """
    settings = get_settings()
    feedback = await _load_feedback(session, current_admin, feedback_id)
    if feedback.adopted_document_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这条反馈已经采纳过（要重新生成，请先到知识库页删掉那份草稿）",
        )

    title = (payload.title or "").strip() or _guess_title(feedback)
    content = (payload.content or "").strip() or feedback.answer_snapshot.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="原答案快照是空的，请填写「更正后的说法」再采纳",
        )

    filename = f"{_DRAFT_PREFIX}{feedback.id}.md"
    text = _draft_markdown(feedback, title=title, content=content)
    _, embedder, _ = clients
    result = await ingest_bytes(
        session,
        tenant_id=current_admin.tenant_id,
        filename=filename,
        data=text.encode("utf-8"),
        embedder=embedder,
        settings=settings,
        content_status=ContentStatus.DRAFT,
    )
    if result.status != "ok":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"草稿没入库：{result.note or result.status}",
        )

    document = await session.scalar(
        select(Document).where(
            Document.tenant_id == current_admin.tenant_id, Document.filename == filename
        )
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="入库报告成功但找不到文档 —— 请重试并查看 api 日志",
        )

    feedback.adopted_document_id = document.id
    feedback.adopted_by = current_admin.id
    feedback.adopted_at = datetime.now(UTC)
    await session.commit()

    note = None
    if embedder is None:
        # ⚠️ 如实说出来，不要让"草稿只有全文路能搜到"变成发布后才发现的事。
        note = "没有配置 embedding key：草稿只有全文路能召回，向量路搜不到"
    return AdoptOut(
        document_id=document.id,
        document_name=document.filename,
        content_status=document.content_status.value,
        chunks=result.chunks,
        note=note,
    )


async def _draft_document(
    session: SessionDep, user: CurrentUser, feedback: AnswerFeedback
) -> Document:
    """取出这条反馈对应的草稿文档（带租户校验）。"""
    if feedback.adopted_document_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="这条反馈还没被采纳，没有草稿"
        )
    document = await session.scalar(
        select(Document).where(
            Document.id == feedback.adopted_document_id,
            Document.tenant_id == user.tenant_id,
        )
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="草稿文档已被删除（反馈记录仍在），可重新采纳",
        )
    return document


@router.get(
    "/{feedback_id}/draft",
    response_model=DraftOut,
    summary="看草稿正文（审阅用，仅管理员）",
    dependencies=[Depends(require_admin)],
)
async def read_draft(
    feedback_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> DraftOut:
    """草稿**不进检索**，所以它只能从这里看 —— 这就是"审阅"这一步的入口。"""
    feedback = await _load_feedback(session, current_user, feedback_id)
    document = await _draft_document(session, current_user, feedback)
    chunks = list(
        (
            await session.scalars(
                select(Chunk)
                .where(Chunk.tenant_id == current_user.tenant_id, Chunk.document_id == document.id)
                .order_by(Chunk.ordinal)
            )
        ).all()
    )
    body = "\n\n".join(chunk.content for chunk in chunks)
    title = body.splitlines()[0].lstrip("# ").strip() if body else document.filename
    return DraftOut(
        document_id=document.id,
        document_name=document.filename,
        content_status=document.content_status.value,
        title=title,
        content=body,
        adopted_at=feedback.adopted_at,
        published_at=feedback.published_at,
        chunks=len(chunks),
    )


@router.post(
    "/{feedback_id}/publish",
    response_model=PublishOut,
    summary="发布草稿（先做一次冲突体检）",
)
async def publish_feedback(
    feedback_id: uuid.UUID,
    payload: PublishIn,
    session: SessionDep,
    current_admin: AdminWriter,
) -> PublishOut:
    """把草稿翻成 `active`，**之前**先拿它和全库现算一次冲突。

    ⚠️ **体检必须在"发布之前"这条路里做**：发布完再体检，等于"先污染再报警"，
        而这份文档从发布那一刻起就已经能被真实用户检索到了。
    ⚠️ 有冲突**默认拒绝发布**（409，把冲突原文一起返回）；
        `force=true` 才发 —— "我看过了，这个冲突是有意的"。留痕在 `published_by`。
    """
    feedback = await _load_feedback(session, current_admin, feedback_id)
    document = await _draft_document(session, current_admin, feedback)

    if document.content_status is ContentStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="这条草稿已经发布过了")
    if document.status is not DocumentStatus.READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"草稿还没入库完成（status={document.status.value}），现在发布等于发一份搜不到的知识",
        )

    report = build_report(
        await collect(session, tenant_id=current_admin.tenant_id, promote={document.id})
    )
    mine = [
        item
        for item in report["items"]
        if document.filename in item["left"]["docs"] or document.filename in item["right"]["docs"]
    ]
    if mine and not payload.force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": f"这份草稿会和知识库里 {len(mine)} 处口径冲突，确认后可以强行发布",
                "conflicts": mine,
            },
        )

    document.content_status = ContentStatus.ACTIVE
    feedback.published_by = current_admin.id
    feedback.published_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(feedback)

    return PublishOut(
        document_id=document.id,
        document_name=document.filename,
        content_status=document.content_status.value,
        published_at=feedback.published_at,
        forced=bool(mine),
        conflicts=mine,
    )
