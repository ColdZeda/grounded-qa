"""会话内容（消息）与问答（SSE 流式）。

⚠️ **SSE 是"流式"的载体，不是"更快的 HTTP"。** 它解决的是体验问题：
    等 8 秒憋出一整段回答 vs 立刻开始冒字 —— 后者即使总耗时一样，感知也完全不同。
    代价是**错误处理变复杂**：HTTP 状态码在流开始前就发出去了，
    流开始之后再出错，只能**在流里发一个 error 事件**（不可能再改成 500）。
    所以这里的错误分两类，见 `_sse_error` 的注释。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update

from app.api.deps import CurrentUser, SessionDep
from app.core.config import get_settings
from app.db.models.conversation import Conversation, Message, MessageRole
from app.db.models.user import User, UserRole
from app.db.repository import TenantScopedRepository
from app.rag.pipeline import Answer, Delta, RagPipeline, Sources, build_clients
from app.rag.providers import ChatClient, EmbeddingClient, RerankClient
from app.schemas.chat import AskRequest, MessageOut

router = APIRouter(prefix="/conversations", tags=["chat"])

#: `(对话, embedding, rerank)`。后两个**可能为 None**（没配 key 就退化成纯全文检索）。
ClientBundle = tuple[ChatClient, EmbeddingClient | None, RerankClient | None]


@lru_cache(maxsize=1)
def _cached_clients() -> ClientBundle:
    """进程内复用三个客户端。

    ⚠️ 用 `lru_cache` 而不是每次请求重建：客户端本身是**无状态**的（key 不变），
        重建只会带来重复的对象分配。真正的连接复用靠 `providers.get_http_client()`。
    """
    return build_clients()


def get_clients() -> ClientBundle:
    """FastAPI 依赖：模型客户端。

    ⚠️ 做成依赖是**为了测试能替换**：测试里 `dependency_overrides[get_clients]`
        塞假客户端 —— 否则跑一次测试就要真花钱（AGENTS §7.1 约定 9）。
    """
    return _cached_clients()


ClientsDep = Annotated[ClientBundle, Depends(get_clients)]


# ------------------------------------------------------------
# 会话归属：与 conversations.py 同一套规则（租户 + 租户内按用户）
# ------------------------------------------------------------
async def _load_conversation(
    session: SessionDep, user: User, conversation_id: uuid.UUID
) -> Conversation:
    """取出当前用户**有权访问**的会话；否则 404（不是 403，见 AGENTS §7.1 约定 14）。"""
    repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Conversation)
    stmt = repo.select().where(Conversation.id == conversation_id)
    if user.role is not UserRole.ADMIN:
        stmt = stmt.where(Conversation.user_id == user.id)

    conversation = await session.scalar(stmt)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return conversation


@router.get("/{conversation_id}/messages", response_model=list[MessageOut], summary="会话消息列表")
async def list_messages(
    conversation_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[Message]:
    """按时间正序列出会话内消息（前端刷新页面后用它恢复上下文）。"""
    conversation = await _load_conversation(session, current_user, conversation_id)
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Message)
    stmt = (
        repo.select()
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


@router.post(
    "/{conversation_id}/messages",
    summary="提问（SSE 流式回答）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "SSE 事件流：`sources`（引用来源，最先发）→ `delta`*（增量文本）→ `done`（收尾）。"
                "流中出错会发 `error` 事件。"
            ),
        }
    },
)
async def ask(
    conversation_id: uuid.UUID,
    payload: AskRequest,
    session: SessionDep,
    current_user: CurrentUser,
    clients: ClientsDep,
) -> StreamingResponse:
    """提问并流式返回回答。

    ⚠️ **落库时机**：用户消息**立刻**落库（否则刷新页面就丢了），
        助手消息在流结束后落库（那时才知道引用了什么、是否拒答、耗时多少）。
    """
    conversation = await _load_conversation(session, current_user, conversation_id)

    chat, embedder, reranker = clients
    pipeline = RagPipeline(
        session=session,
        tenant_id=current_user.tenant_id,
        chat=chat,
        embedder=embedder,
        reranker=reranker,
    )

    question = payload.question.strip()
    if not question:
        # ⚠️ 用 `HTTP_422_UNPROCESSABLE_CONTENT`（新名）。
        #    老名 `..._ENTITY` 在 starlette 里已标记弃用，会刷警告 —— 警告刷多了就没人看。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="问题不能为空"
        )

    # ① 用户消息先落库（并提交）—— 即使后面生成失败，提问记录也在
    user_message = Message(
        tenant_id=current_user.tenant_id,
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content=question,
    )
    session.add(user_message)
    await session.commit()

    history = await _load_history(session, conversation.id)

    return StreamingResponse(
        _stream(session, pipeline, conversation, question, history),
        media_type="text/event-stream",
        headers={
            # ⚠️ 告诉 nginx **不要缓冲**这个响应。少了它，SSE 会被攒成一坨再发，
            #    "流式"看起来完全没生效 —— 而且本地直连（不经过 nginx）时是好的，
            #    只有部署后才出问题，很容易误判成前端 bug。
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


async def _load_history(session: SessionDep, conversation_id: uuid.UUID) -> list[dict[str, str]]:
    """取最近若干轮对话，供多轮追问使用。

    ⚠️ 从**最早**的那条开始带（而不是最新）—— 顺序反了模型会以为对话是倒着发生的。
        所以这里是「先按时间倒序取 N 条，再翻回正序」。
    """
    limit = get_settings().history_messages
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    # 只带 user / assistant；system 不落库，这里防御性过滤
    return [
        {"role": row.role.value, "content": row.content}
        for row in rows
        if row.role in (MessageRole.USER, MessageRole.ASSISTANT)
    ]


def _sse(event: str, data: dict[str, object]) -> str:
    """拼一个 SSE 帧。

    ⚠️ `data` 必须是 **JSON 单行**。SSE 协议里换行是**字段分隔符** ——
        回答里只要有换行，不转义就会把一帧拆成两帧，前端收到半截 JSON 直接解析失败。
        `json.dumps` 默认把换行转成 `\\n`，正好解决这件事。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(
    session: SessionDep,
    pipeline: RagPipeline,
    conversation: Conversation,
    question: str,
    history: list[dict[str, str]],
) -> AsyncIterator[str]:
    """把 pipeline 的领域事件翻译成 SSE 帧，并负责助手消息落库。

    ⚠️ **落库放在 `finally`**：客户端中途断开（关页面 / 切网络）会让这个生成器被
        `GeneratorExit`，如果落库写在最后一行，那条回答就永远丢了 ——
        而用户明明已经看到了半截。
    """
    final: Answer | None = None
    try:
        async for event in pipeline.answer(question=question, history=history):
            if isinstance(event, Sources):
                yield _sse(
                    "sources",
                    {
                        "citations": [c.as_dict() for c in event.citations],
                        "confidence": event.confidence,
                        "refused": event.refused,
                    },
                )
            elif isinstance(event, Delta):
                yield _sse("delta", {"text": event.text})
            elif isinstance(event, Answer):
                final = event
                if event.error:
                    yield _sse("error", {"detail": event.error, "stage": "generation"})
                yield _sse(
                    "done",
                    {
                        "refused": event.refused,
                        "confidence": event.confidence,
                        "latency_ms": event.latency_ms,
                    },
                )
    except Exception as exc:
        # ⚠️ 这里**不能**再抛 HTTP 异常：响应头早就发出去了，改不了状态码。
        #    唯一能做的是在流里告诉前端"出错了"。
        yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}", "stage": "pipeline"})
    finally:
        await _persist_answer(session, conversation, final)


async def _persist_answer(
    session: SessionDep, conversation: Conversation, final: Answer | None
) -> None:
    """把助手回答落库（即使客户端提前断开也要落）。"""
    if final is None or not final.text:
        return
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=final.text,
        citations=[c.as_dict() for c in final.citations],
        refused=final.refused,
        latency_ms=final.latency_ms,
    )
    session.add(message)
    # ⚠️ 会话列表是按 `updated_at` 倒序排的。助手消息落库**不会**自动更新
    #    `conversations.updated_at`（那是另一张表），所以这里显式碰一下 ——
    #    否则"刚聊过的会话"不会浮到列表最上面，用起来非常别扭。
    #    用 UPDATE 语句而不是改 ORM 属性：属性赋同一个值 SQLAlchemy 会认为"没变"，
    #    于是不发 UPDATE，`onupdate` 也就不会触发。
    await session.execute(
        update(Conversation).where(Conversation.id == conversation.id).values(updated_at=func.now())
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
