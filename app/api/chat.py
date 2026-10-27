"""会话内容（消息）与问答（SSE 流式）。

⚠️ **SSE 是"流式"的载体，不是"更快的 HTTP"。** 它解决的是体验问题：
    等 8 秒憋出一整段回答 vs 立刻开始冒字 —— 后者即使总耗时一样，感知也完全不同。
    代价是**错误处理变复杂**：HTTP 状态码在流开始前就发出去了，
    流开始之后再出错，只能**在流里发一个 error 事件**（不可能再改成 500）。
    所以这里的错误分两类，见 `_sse_error` 的注释。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update

from app import images as image_store
from app.api.deps import CurrentUser, LimitsRedisDep, SessionDep
from app.core.config import Settings, get_settings
from app.core.limits import LimitGuard, ensure
from app.core.metrics import record_chat_outcome, record_image_analysis
from app.db.models.conversation import Channel, Conversation, Message, MessageRole
from app.db.models.image import Image
from app.db.models.user import User, UserRole
from app.db.repository import TenantScopedRepository
from app.orders import order_context
from app.rag import followup as followup_module
from app.rag.agent import AgentRunner, ToolEvent
from app.rag.pipeline import Answer, Delta, RagPipeline, Sources, build_clients
from app.rag.providers import ChatClient, EmbeddingClient, ModelAPIError, RerankClient
from app.rag.vision import ImageAnalysisClient, build_image_client
from app.schemas.chat import AskRequest, MessageOut

logger = logging.getLogger("grounded.chat")

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


def get_image_client() -> ImageAnalysisClient | None:
    """FastAPI 依赖：读图客户端（**没配好就是 `None`**）。

    ⚠️ 做成依赖的理由同 `get_clients`：测试要能塞假客户端，否则跑一次测试就真花钱
        （`AGENTS §7.1` 约定 9）。`None` 不是错误 —— 线上关掉读图时就是它。
    """
    return build_image_client()


ImageClientDep = Annotated[ImageAnalysisClient | None, Depends(get_image_client)]


@dataclass
class ImageContext:
    """这条提问带的那张图，**读完之后**的结果（读失败也在里面）。

    ⚠️ **失败不抛**：图读不出来不该让整条问答 500 —— 用户还能靠文字问。
        但失败必须**留下痕迹**（`error` 进 SSE、`analysis_error` 进库），
        否则"我们其实没看图"就变成静默的了（`PLAN §8.3` D10）。
    """

    image_id: uuid.UUID | None = None
    #: 进历史的那一句文字记忆（追问靠它，模型看不到图）。
    note: str = ""
    #: 进**检索**的那一句（`scene`）。
    scene: str = ""
    #: 只进生成上下文，且标了"可能有误"。
    text: str = ""
    error: str = ""
    #: 真的读成功了（用于 SSE 里区分"没带图"和"带了但没读成"）。
    analyzed: bool = False


async def _prepare_image(
    session: SessionDep,
    user: User,
    image_id: uuid.UUID | None,
    client: ImageAnalysisClient | None,
    *,
    guard: LimitGuard,
) -> ImageContext:
    """取图 → 读图 → 落库读数。**查不到/不是本人的 → 404**。

    ⚠️ **读图花的 token 也要记账**（`PLAN §8.3` D9）：它是一次实打实的模型调用，
        不算进配额就等于"图片这条路免费"，用图片绕开配额是真实存在的。
    """
    if image_id is None:
        return ImageContext()

    repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Image)
    row = await repo.get(image_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片不存在")

    if client is None:
        return ImageContext(image_id=image_id, error="图片分析没有启用，我只按你的文字回答")

    try:
        raw = await image_store.read(row.path)
        analysis = await client.analyze(raw, content_type=row.content_type)
    except ModelAPIError as exc:
        logger.warning("读图失败 image=%s: %s", image_id, exc)
        record_image_analysis("error")
        row.analysis_error = str(exc)[:500]
        await session.commit()
        return ImageContext(image_id=image_id, error="图片没读成功，我只按你的文字回答")
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("读图的文件不可用 image=%s: %s", image_id, exc)
        record_image_analysis("error")
        row.analysis_error = f"文件不可用：{exc}"[:500]
        await session.commit()
        return ImageContext(image_id=image_id, error="图片没读成功，我只按你的文字回答")

    # ⚠️ 读数落库：既是缓存（同一张图只读一次），也是 badcase 复盘的唯一证据
    #    （"当时模型到底读到了什么"）。失败时留 `analysis_error`，不假装成功。
    row.analysis = analysis.as_dict()
    row.analysis_error = None
    row.analyzed_at = datetime.now(UTC)
    await session.commit()

    # ⚠️ 读图的 token 也是花出去的钱：记账 + 指标。
    #    拿不到 usage 时记 0（**如实**：不是"没花"，是"没量到"）。
    await guard.record_tokens(
        user.id, int(analysis.usage.get("total_tokens") or 0), channel=Channel.WEB.value
    )
    record_image_analysis("fallback" if analysis.fallback else "ok")

    logger.info(
        "读图完成 image=%s type=%s fallback=%s tokens=%s",
        image_id,
        analysis.image_type,
        analysis.fallback,
        analysis.usage.get("total_tokens"),
    )
    return ImageContext(
        image_id=image_id,
        note=analysis.memory_line,
        scene=analysis.scene,
        text=analysis.transcription,
        analyzed=True,
    )


async def _order_context(
    session: SessionDep, user: User, *, text: str, extra_order_no: str | None = None
) -> tuple[list[dict[str, object]], str, list[datetime]]:
    """从这段文字里捞单号 → 查库 → 事实卡片 ＋ 提示词块 ＋ 下单时间。

    ⚠️ 2026-10-26 起**实现搬到了 `app/orders.py::order_context`** —— 因为飞书那边
        也要查订单了（之前只有 Web 查）。这里只保留一层壳，调用点不动。
    """
    return await order_context(
        session, tenant_id=user.tenant_id, text=text, extra_order_no=extra_order_no
    )


# ------------------------------------------------------------
# 会话归属：与 conversations.py 同一套规则（租户 + 租户内按用户）
# ------------------------------------------------------------
def _preferred_mode(user: User) -> str:
    """这个用户上次选的模式（没选过就是**快速**）。"""
    raw = (user.preferences or {}).get("chat_mode")
    return raw if raw in ("fast", "agent") else "fast"


def _resolve_mode(payload: AskRequest, user: User, settings: Settings) -> str:
    """这次提问**实际走哪条路**。

    三层开关的合成规则（必须写在一个地方，否则迟早出现“到底听谁的”）：

        实际模式 = open_agent_mode AND （请求显式指定 → 用户偏好 → 快速）

    ⚠️ **总闸关掉时，请求里传 `agent` 也不生效** —— 而且要告诉用户（前端把开关置灰）。
        最坏的情况是“用户以为在用 agent，实际走的 pipeline”—— 那样评测数据和用户体验全都是假的。
    """
    if not settings.open_agent_mode:
        return "fast"
    return payload.mode or _preferred_mode(user)


def _scratchpad_of(conversation: Conversation) -> dict[str, object]:
    """取这个会话的**跨轮事实**（agent 写的）。没有就是空字典。

    ⚠️ 读不出来就当空，**不报错**：这是"记忆"，不是"数据"。
        一个格式不对的 meta 不该让整次问答失败。
    """
    raw = (conversation.meta or {}).get("scratchpad")
    return dict(raw) if isinstance(raw, dict) else {}


class _AgentSource:
    """把 `AgentRunner` 套成 `source.answer(question=..., history=...)` 的形状。

    ⚠️ 为什么要这个适配器：`AgentRunner` 的问题与历史是**构造时**就定下的
        （它要把它们组成系统提示），而 `_stream` 只认 pipeline 那个形状。
        两边不动，只加一层适配 —— 比去改任一边的接口都安全。
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    def answer(self, *, question: str | None = None, history: object = None) -> AsyncIterator:
        # 已在构造时定下，这里的参数只为了对齐形状（念念不忘，念念不忘）。
        return self._runner.answer()


def _agent_meta(final: Answer | None) -> dict[str, object] | None:
    """agent 跑过的痕迹 → `messages.meta`。**pipeline 模式下返回 `None`**（不写这个字段）。

    ⚠️ 没有它，agent 的行为**无法归因** —— 路径不固定，事后只能靠这串轨迹回放
        "它当时为什么调了这个工具、为什么没调那个"。
    """
    if final is None:
        return None
    trace = final.extra.get("agent_trace")
    if not isinstance(trace, list):
        return None
    meta: dict[str, object] = {"agent_trace": trace}
    for key in ("agent_steps", "agent_retries", "degraded", "awaiting"):
        if key in final.extra:
            meta[key] = final.extra[key]
    return meta


#: 这些 `extra` 字段**两种模式都要落库**（不只是 agent 的）。
#: ⚠️ `citation_audit` 只随 SSE `done` 帧发一次，不落库的话**刷新即消失、事后统计为 0**
#:    —— 2026-10-27 走查就是这样：`gq_citation_support_total` 有数，库里一条都查不到。
#:    详见 `architecture §9.53 ④`「只在一帧里的证据等于没有」。
_PERSISTED_EXTRA = ("citation_audit", "citation_repairs")


def _persisted_extra(final: Answer) -> dict[str, object]:
    """两种模式共用的"要落库的 extra"。**没有就返回空字典**（不写空值）。"""
    return {key: final.extra[key] for key in _PERSISTED_EXTRA if key in final.extra}


def _merged_meta(final: Answer) -> dict[str, object] | None:
    """`messages.meta` 最终写什么：**agent 痕迹 ＋ 两种模式都该落的核对结果**。

    ⚠️ 两边都空时返回 `None`（而不是 `{}`）—— 老数据里没有这个字段，
        新数据也别塞空对象，否则做统计的人要同时处理两种空。
    """
    meta: dict[str, object] = dict(_agent_meta(final) or {})
    meta.update(_persisted_extra(final))
    return meta or None


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
                "SSE 事件流：`sources`（引用来源，最先发）→ `delta`*（增量文本）→ "
                "`done`（收尾，`citation_audit` 在校到东西时才带）。"
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
    redis: LimitsRedisDep,
    image_client: ImageClientDep,
) -> StreamingResponse:
    """提问并流式返回回答。

    ⚠️ **落库时机**：用户消息**立刻**落库（否则刷新页面就丢了），
        助手消息在流结束后落库（那时才知道引用了什么、是否拒答、耗时多少）。

    ⚠️ **两道闸都在落库之前**（被拒的请求不该在库里留下痕迹）：
        ① 速率（次数/分钟）—— 放最前面，**连 404 / 422 的请求也计数**，
           那正是刷子最先打的地方；
        ② 配额（token/天）—— 放到校验之后：格式错的请求不花一分钱，不该扣额度。
    """
    guard = LimitGuard(redis, get_settings())
    ensure(await guard.chat_rate(current_user.id))

    conversation = await _load_conversation(session, current_user, conversation_id)

    question = payload.question.strip()
    if not question:
        # ⚠️ 用 `HTTP_422_UNPROCESSABLE_CONTENT`（新名）。
        #    老名 `..._ENTITY` 在 starlette 里已标记弃用，会刷警告 —— 警告刷多了就没人看。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="问题不能为空"
        )

    # ② 配额：超了就**连模型都不调**（这时候拒是最省的）
    # ⚠️ 必须在**读图之前**：读图也是一次模型调用，也要花钱。
    ensure(await guard.quota(current_user.id))

    # ③ 读图（如果这条消息带了图）。失败**不抛**，只降级 —— 见 `ImageContext`。
    image_context = await _prepare_image(
        session, current_user, payload.image_id, image_client, guard=guard
    )

    # ④ 订单：从"图里读到的文字 + 用户原话"里捞单号，精确查库。
    #    ⚠️ 图里的转写排前面：用户从图里抄单号时，那句话往往就是"SF10001 这单…"。
    scratchpad = _scratchpad_of(conversation)
    order_snapshots, order_facts, order_placed_at = await _order_context(
        session,
        current_user,
        text=f"{image_context.text} {question}".strip(),
        # 上一轮已经确认过的单号（仅 agent 跑过之后才会有）
        extra_order_no=scratchpad.get("order_no")
        if isinstance(scratchpad.get("order_no"), str)
        else None,
    )

    chat, embedder, reranker = clients
    pipeline = RagPipeline(
        session=session,
        tenant_id=current_user.tenant_id,
        chat=chat,
        embedder=embedder,
        reranker=reranker,
        # 检索只用 scene（精确串不进 query），转写只进生成上下文
        image_scene=image_context.scene,
        image_text=image_context.text,
        order_facts=order_facts,
        # 查到订单就以**下单时间**为时间基准（老订单按当年条款答），见 `app/rag/reftime.py`
        order_placed_at=order_placed_at,
    )

    # ① 用户消息先落库（并提交）—— 即使后面生成失败，提问记录也在
    # ⚠️ 图与订单的读数存在 `meta`，**不写进 `content`**：
    #    `content` 永远是用户原话，混进机器生成的文字会让排障和评测都失真。
    meta: dict[str, object] | None = None
    if image_context.image_id is not None or order_snapshots:
        meta = {
            "image_id": str(image_context.image_id) if image_context.image_id else None,
            "image_note": image_context.note or None,
            "image_error": image_context.error or None,
            "orders": order_snapshots,
        }
    user_message = Message(
        tenant_id=current_user.tenant_id,
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content=question,
        meta=meta,
    )
    session.add(user_message)
    await session.commit()

    history = await _load_history(session, conversation.id)

    # ⑤ 追问改写（v1.4 C1'）：这次检索会不会并进"上一轮有实质内容的问句"。
    #    ⚠️ 落两处，各有各的用途：
    #       · **用户消息的 `meta`**（这里）—— 界面靠它显示 chip「已合并上一轮检索」，
    #         而且**刷新页面后还在**（用户"想看出有没有生效"往往正是事后看的）；
    #       · **`Answer.extra["followup_query"]`**（pipeline 里）—— 给 badcase 归因。
    #    ⚠️ 算两遍不是浪费：同一个**纯函数**、同一份历史 ⟹ 结论必然一致
    #       （这一点由 `followup.previous_question` 的单元测试钉住）。
    followup_prev = followup_module.previous_question(question, history)
    if followup_prev:
        user_message.meta = {**(user_message.meta or {}), "followup_prev": followup_prev}
        await session.commit()

    # ⑤ 选路：`ANSWER_MODE=pipeline`（默认）时 **一个字都不变**，
    #    `agent` 才把同一个 pipeline 当作**回落目标**交给 `AgentRunner`。
    #    ⚠️ 分支只能出现在这里：往前靠一步就会把 agent 的东西渗进老路径，
    #       `PLAN §13.4` 要求 pipeline 模式与加之前逐字节一致。
    settings_now = get_settings()
    chosen = _resolve_mode(payload, current_user, settings_now)
    if payload.mode is not None and payload.mode != _preferred_mode(current_user):
        # 记住"上次选了啥"，下次进来自动回显（**以请求为准，表里只记偏好**）
        current_user.preferences = {**(current_user.preferences or {}), "chat_mode": payload.mode}
        await session.commit()

    source: RagPipeline | _AgentSource = pipeline
    if chosen == "agent":
        source = _AgentSource(
            AgentRunner(
                session=session,
                tenant_id=current_user.tenant_id,
                chat=chat,
                question=question,
                fallback=lambda: pipeline,
                embedder=embedder,
                reranker=reranker,
                settings=settings_now,
                history=history,
                user_id=current_user.id,
                scratchpad=scratchpad,
            )
        )

    return StreamingResponse(
        _stream(
            session,
            source,
            conversation,
            question,
            history,
            guard,
            current_user.id,
            image_context=image_context,
            followup_prev=followup_prev,
        ),
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


def _outcome_of(answer: Answer) -> str:
    """一次问答的结局。三个桶**互斥**。

    ⚠️ **拒答单独一个桶**，不和"正常回答"混：拒答率突然升高通常意味**检索坏了**
        （阈值没变、库里却搜不到东西），而不是用户问得怪。混在一起就看不见这件事。
    ⚠️ `error` 与"拒答"是两回事：拒答是**产品行为**，出错是**故障**。
    """
    if answer.error:
        return "error"
    return "refused" if answer.refused else "answered"


def _sse(event: str, data: dict[str, object]) -> str:
    """拼一个 SSE 帧。

    ⚠️ `data` 必须是 **JSON 单行**。SSE 协议里换行是**字段分隔符** ——
        回答里只要有换行，不转义就会把一帧拆成两帧，前端收到半截 JSON 直接解析失败。
        `json.dumps` 默认把换行转成 `\\n`，正好解决这件事。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(
    session: SessionDep,
    source: RagPipeline | _AgentSource,
    conversation: Conversation,
    question: str,
    history: list[dict[str, str]],
    guard: LimitGuard,
    user_id: uuid.UUID,
    *,
    image_context: ImageContext | None = None,
    followup_prev: str = "",
) -> AsyncIterator[str]:
    """把 pipeline 的领域事件翻译成 SSE 帧，并负责助手消息落库。

    ⚠️ **落库在 `finally`，而且自带 session**（2026-10-28 改）：
        客户端中途断开会让这个生成器被取消/关掉，若落库还挂在请求级 session 上，
        那一轮就永远丢了 —— 而用户明明已经看到（或正要看到）答案。
        现在的做法见下面 `finally` 那段注释。
    """
    final: Answer | None = None
    try:
        async for event in source.answer(question=question, history=history):
            if isinstance(event, Sources):
                payload: dict[str, object] = {
                    "citations": [c.as_dict() for c in event.citations],
                    "confidence": event.confidence,
                    "refused": event.refused,
                }
                # ⚠️ 图的状态**必须发出来**：读图失败时前端要能明说"图片没读成功"，
                #    而不是让用户以为我们看过图了（`PLAN §8.3` D10）。
                if image_context is not None and (
                    image_context.image_id is not None or image_context.error
                ):
                    payload["image"] = {
                        "analyzed": image_context.analyzed,
                        "note": image_context.note or None,
                        "error": image_context.error or None,
                    }
                # ⚠️ 追问改写（v1.4 C1'）：**说清楚"检索并进了哪一句"**。
                #    不说的话，用户看到「德国呢」被回答成德国政策，只会觉得"它怎么知道"；
                #    反过来，答偏了也无从判断是不是改写惹的祸（详细档才显示）。
                if followup_prev:
                    payload["followup_prev"] = followup_prev
                yield _sse("sources", payload)
            elif isinstance(event, Delta):
                yield _sse("delta", {"text": event.text})
            elif isinstance(event, ToolEvent):
                # ⚠️ **这一帧不是装饰**：agent 比 pipeline 慢 2~4 倍，
                #    中间几秒什么都不给，用户会以为卡死了（`PLAN §13.3`）。
                yield _sse(
                    "tool",
                    {
                        "name": event.name,
                        "args": event.args_preview,
                        "ok": event.ok,
                        "ms": event.ms,
                        "summary": event.summary,
                    },
                )
            elif isinstance(event, Answer):
                final = event
                record_chat_outcome(_outcome_of(event))
                if event.error:
                    yield _sse("error", {"detail": event.error, "stage": "generation"})
                done_payload: dict[str, object] = {
                    "refused": event.refused,
                    "confidence": event.confidence,
                    "latency_ms": event.latency_ms,
                }
                # 引用可核对性（v1.4 A1）：**让"这次有几处对不上"跟着收尾事件出去**，
                # web 端才有机会在不改正文的前提下提示一句（飞书那边同理，
                # `channels/feishu.py::render_reply`）。
                # ⚠️ 只在**真的核过**（`checked > 0`）时才带：没有可核数字的回答
                #    （拒答 / 闭卷 / 全名词句）带上一个全零对象，只会让前端多判断一次。
                audit = event.extra.get("citation_audit")
                if isinstance(audit, dict) and audit.get("checked"):
                    done_payload["citation_audit"] = audit
                # ⚠️ agent 的降级原因要**跟着收尾事件出去**：界面靠它画那个角标。
                #    不发的话，只有刷新页面（从 `messages.meta` 重建）才能看到 —— 而正在等答案的人恰恰看不到。
                degraded = event.extra.get("degraded")
                if isinstance(degraded, str) and degraded:
                    done_payload["degraded"] = degraded
                    done_payload["agent_steps"] = int(event.extra.get("agent_steps") or 0)
                # ⚠️ **2026-10-27 走查 B2**：轨迹以前只在"从 `messages.meta` 重建消息"时读，
                #    流式当轮 `done` 帧没带 ⟹ **正在等答案的人永远看不到**，刷新一下才有。
                #    轨迹的价值恰恰是"解释刚才发生了什么"，事后补看等于没用。
                trace = event.extra.get("agent_trace")
                if isinstance(trace, list) and trace:
                    done_payload["agent_trace"] = trace
                # 修法 D 的明细也跟着出去：界面要能说"我们改过编号"（不静默动证据）。
                repairs = event.extra.get("citation_repairs")
                if isinstance(repairs, list) and repairs:
                    done_payload["citation_repairs"] = repairs
                yield _sse("done", done_payload)
    except Exception as exc:
        # ⚠️ 这里**不能**再抛 HTTP 异常：响应头早就发出去了，改不了状态码。
        #    唯一能做的是在流里告诉前端"出错了"。
        record_chat_outcome("error")
        yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}", "stage": "pipeline"})
    finally:
        # ⚠️⚠️ **2026-10-28：收尾动作放进 `CancelScope(shield=True)`。**
        #
        #    走查第三次实测（生产库 + nginx 日志）：生成中途切走页面 ⟹ 这一轮被
        #    **取消** —— 答案不生成、不落库、`record_tokens` 也不执行（钱花了、账没记），
        #    会话里只剩一条没人回的提问。原来那句"断开也会落库"只在"答案已生成完、
        #    就差最后一句写库"那几十毫秒里成立，**等于没有**。
        #
        #    `shield=True` 的意思是：**已经被取消的任务**在退出这个 scope 之前不会被
        #    再打断一次 ⟹ 这两个"必须做完"的动作能真的做完。
        #
        #    ⚠️ **它救不了"生成途中就被取消"**（那种连 `Answer` 都没产生，没有东西可落）——
        #       那种要靠"生成与转发解耦"（候选 `W25`）。真正挡住它的是前端那条
        #       「离开页面不再 abort」（`ChatView.onBeforeUnmount`）。
        #    ⚠️ 刻意**不换独立 session / 不另起 task**：`get_session_factory()` 是进程级
        #       引擎，而测试是"每个用例一个事件循环"（约定 16）⟹ 在 `finally` 里开新
        #       session 会把连接池跨循环复用，整批测试跟着红（这次真踩到了）。
        #       用请求的 session 是安全的：yield 依赖在**响应结束之后**才拆。
        with anyio.CancelScope(shield=True):
            await _persist_answer(session, conversation, final)
            # ⚠️ **记账也放这里**：用户中途关掉页面时 token 其实**已经花了**，
            #    不记的话，最费钱的那类请求（问大问题、看两眼就走）反而完全不进账。
            # ⚠️ 拒答时 `usage` 是空的（根本没调模型）⟹ 记 0，不占额度。
            if final is not None:
                await guard.record_tokens(
                    user_id, int(final.usage.get("total_tokens") or 0), channel=Channel.WEB.value
                )


async def _persist_answer(
    session: SessionDep, conversation: Conversation, final: Answer | None
) -> None:
    """把助手回答落库（即使客户端提前断开也要落 —— 由调用方的 shield 保证）。"""
    if final is None or not final.text:
        return
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=final.text,
        citations=[c.as_dict() for c in final.citations],
        refused=final.refused,
        # ⚠️ 置信度也落库（2026-10-15，见 `architecture.md` §9.19）：
        #    不落的话，事后只能知道"它拒答了"，**答不出"它在哪个分数上拒的"** ——
        #    那样阈值调优就只能靠猜，badcase 也归因不了。
        confidence=final.confidence,
        latency_ms=final.latency_ms,
        # ⚠️ agent 跑过才有；无痕迹时保持 `None`，不给老数据增加空字段。
        # ⚠️ 2026-10-27：`citation_audit` / `citation_repairs` **两种模式都要落** ——
        #    它们以前只随 SSE 帧出去，库里查不到，等于"看不见的证据"（§9.53 ④）。
        meta=_merged_meta(final),
    )
    session.add(message)

    # 把这一轮**确认过的事实**并进会话的 scratchpad（下一轮"那保修呢"就靠它）。
    # ⚠️ 新事实**覆盖**同名旧事实；没提到的字段原样保留（否则"上一轮说过的商品"会被一句寒暄冲掉）。
    delta = final.extra.get("scratchpad")
    if isinstance(delta, dict) and delta:
        current = _scratchpad_of(conversation)
        conversation.meta = {**(conversation.meta or {}), "scratchpad": {**current, **delta}}
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
        raise
