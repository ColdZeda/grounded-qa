"""飞书渠道：把「飞书里的一句话」变成一次问答。

## 和告警那个 webhook 是两回事

· `ALERT_WEBHOOK_URL`（v1.1 告警）= **群机器人 webhook**：只能**发**，收不到用户消息
· 这里 = **自建应用 ＋ 长连接**：能收发。凭据是 `App ID` / `App Secret`，**不是 webhook**

## 为什么走「长连接」而不是「事件回调」

回调要公网可达 + 验签 + HTTPS，而本项目**明确不做 HTTPS**（`AGENTS §7.1` 约定 4）。
长连接是**出站**的：服务器主动连飞书，不需要任何入站端口 —— 这也正是当初选
`lark-oapi` 的理由。代价是必须有一个**常驻进程**（内存预算见 `PLAN §2.3`）。

## 这个文件里**没有** `lark` 的 import

事件解析、去重、拼历史、切分长回复，全是纯逻辑 —— 这样测试**不需要装 `lark-oapi`**、
更不需要真的连飞书。`lark` 只出现在 `feishu_runner.py` 里，而且是在函数内部延迟导入。

## 已知简化（写出来，别当成"做好了"）

· **`open_id → 用户` 的映射没做**：所有飞书消息都记在 `FEISHU_CHANNEL_EMAIL` 那个账号名下。
  真做映射要有成对的表 + 管理界面 + 越权设计，那是另一件事（`CANDIDATES.md`）。
  隔离本身仍然成立（消息只落它名下那个租户/用户）。
· **去重是"单进程内存"**：飞书会重投事件，这里用有界集合挡一下。多副本部署要换 Redis。
· **不分块流式**：等答案生成完一次发出。飞书机器人没有 SSE 那套，流式要改成"更新卡片"。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.feishu_names import NameResolver, short_id
from app.core.config import Settings, get_settings
from app.core.limits import LimitGuard, RateLimitedError
from app.db.models.conversation import Channel, Conversation, Message, MessageRole
from app.db.models.user import User
from app.db.repository import TenantScopedRepository
from app.db.session import get_session_factory
from app.orders import order_context
from app.rag import mdtext
from app.rag.pipeline import Answer, RagPipeline, Sources
from app.rag.providers import ChatClient, EmbeddingClient, RerankClient

logger = logging.getLogger("grounded.feishu")

#: 单条飞书**卡片**的字数上限（飞书自己允许更大，但长文在群里没人读）。
#: 超了就按段落切开分多条发 —— **截断**是不可接受的（会把结论吃掉）。
MAX_REPLY_CHARS = 3500

#: 会话标题 / 群名的**刷新周期**：群改名、人改名都要能跟上，但没必要每条消息都问一次飞书。
_TITLE_TTL = timedelta(days=7)

#: 拿不到真名时的显示。
_USER_FALLBACK = "飞书用户 · {tail}"
_GROUP_FALLBACK = "飞书群 · {tail}"

#: 进提示词的历史条数（和 Web 端同一口径：只取最近若干条，纯文本）。
_HISTORY_LIMIT = 20

#: 内存去重的容量：近期消息 id 足够，旧的自然淘汰。
_SEEN_CAPACITY = 512

#: 引用对不上时补的那一行（v1.4 A1）。措辞只陈述"**我们没核对上**"，
#: 不说"答案是错的" —— 检查器会误报，替用户下结论比不给提示更糟。
_CITATION_HINT = "⚠️ 本次有 {count} 处引用未能核对上，请以原文为准。"


@dataclass(frozen=True)
class FeishuEvent:
    """一条**能回答**的飞书消息（已经摘掉 @ 和无关字段）。"""

    chat_id: str
    open_id: str
    message_id: str
    text: str
    #: 群聊里要多说一句"我是按渠道账号记的"吗？—— 不改行为，只用于日志区分。
    is_group: bool


class FeishuSender(Protocol):
    """发消息的能力。**协议而不是类**：测试塞假实现，生产塞 `lark` 的客户端。

    ⚠️ 收的是**飞书卡片的 markdown（`lark_md`）**，不是任意 markdown ——
       表格 / `#` 标题 / `---` 分割线这三种 `lark_md` **不认**，必须先过
       `to_feishu_markdown()`（否则用户看到的是 `|---|---|` 这种原文）。
    """

    async def send_markdown(self, chat_id: str, markdown: str) -> None: ...


def strip_mentions(text: str) -> str:
    """去掉 @ 占位符。

    ⚠️ 飞书在群里把 `@机器人` 转成 `@_user_1` 塞进正文开头。不去掉的话，
       它会成为 **检索 query 的一部分** —— trgm 和向量都会把 `@_user_1` 当成噪声。
    """
    out = text
    while "@_user_" in out:
        head, _, tail = out.partition("@_user_")
        token, _, rest = tail.partition(" ")
        out = f"{head}{rest}".strip() if token else f"{head}{tail}"
    return out.strip()


def parse_event(payload: dict[str, Any]) -> FeishuEvent | None:
    """从事件回调里抠出「谁在哪个会话里说了什么」。

    只处理 `im.message.receive_v1` 的**文本**消息。以下一律返回 `None`（并留日志）：
      · 非文本（图片/文件/表情）—— v1.2 不做飞书侧多模态，如实忽略而不是假装看过
      · 机器人自己发的（`sender_type != "user"`）—— 否则会自己跟自己对话
      · 字段缺失 / 结构不对 —— 宁可丢一条，也不要抛异常把长连接带崩
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    message = event.get("message")
    sender = event.get("sender")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None

    if message.get("message_type") != "text":
        logger.info("忽略非文本消息 type=%s", message.get("message_type"))
        return None

    # ⚠️ 只答**人**发的消息。别的机器人（包括我们自己的回复）也会触发事件 ——
    #    不挡的话会自己跟自己对话，而且真的会无限循环。
    sender_type = sender.get("sender_type")
    if sender_type != "user":
        logger.info("忽略非用户消息 sender_type=%s", sender_type)
        return None

    sender_id = sender.get("sender_id")
    open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None
    chat_id = message.get("chat_id")
    message_id = message.get("message_id")
    raw = message.get("content")

    if not (isinstance(chat_id, str) and isinstance(message_id, str) and isinstance(raw, str)):
        return None

    try:
        text = json.loads(raw).get("text", "")
    except json.JSONDecodeError:
        # ⚠️ `content` 是**JSON 字符串**（飞书把所有消息类型统一成这样）。
        #    解析失败就当成纯文本用 —— 丢消息比多问一次更糟。
        text = raw

    text = strip_mentions(text if isinstance(text, str) else "")
    if not text:
        return None

    return FeishuEvent(
        chat_id=chat_id,
        open_id=open_id if isinstance(open_id, str) else "",
        message_id=message_id,
        text=text,
        is_group=message.get("chat_type") == "group",
    )


def split_reply(text: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    """把长回复按**段落**切开，尽量每段不超过 `limit`。

    ⚠️ **不许截断**：飞书上没人会翻第二条去拼结论，截断等于把答案砍断。
    ⚠️ 优先在空行处切（模型排的列表/表格不会被切散），切不动才按字符硬切。
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:  # 空行太靠前（等于没切），退而求其次找单个换行
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def to_feishu_markdown(text: str) -> str:
    """把模型输出的 markdown **降级**成飞书卡片认得的 `lark_md`。

    `lark_md` 认：粗体 / 斜体 / 删除线 / 链接 / 有序无序列表 / 代码块 / 图片。
    **不认**这三样 —— **表格**、`#` **标题**、`---` **分割线** —— 不处理就会原样
    露出 `|---|---|`，用户在群里看到的是源码（2026-10-22 真机联调实测踩中）。

    ⚠️ 规则本体在 `app/rag/mdtext.py`，**Web 引用摘要用的是同一份**（那边只要最朴素的
       三件事、不要 `- ` 和加粗）。要改洗法改那里，别在这里另写一遍。
    """
    return mdtext.downgrade(
        text,
        bold_headings=True,  # 卡片没有标题语法 → 整行加粗
        list_tables=True,  # 表格降级成列表行更好读
        merge_lone_citations=True,  # 飞书里点不了 `[3]`，得说明"依据在这句"
    )


def render_reply(answer: Answer, *, show_sources: bool) -> str:
    """把 `Answer` 变成飞书里看到的文字（**已经是 `lark_md`**）。

    ⚠️ 飞书里点不了 `[1]`，所以来源给的是**文字版**（编号 + 摘要 + 分数口径），
       而且**分数必须带口径**（`精排分` / `余弦`）—— 同一个数字不同含义，
       和 Web 端是同一纪律（`architecture.md` §9.21）。
    """
    body = to_feishu_markdown(answer.text.strip())
    if not body:
        body = "（没有生成出内容）"
    hint = citation_hint(answer)
    if not show_sources or not answer.citations:
        return f"{body}\n\n{hint}" if hint else body

    lines = [body, "", "**—— 参考来源 ——**"]
    for cite in answer.citations:
        lines.append(
            f"**[{cite.index}]** {_snippet(cite.snippet)}（{cite.score_kind} {cite.score:.3f}）"
        )
    if hint:
        # ⚠️ 提示放**最后一行**：正文一个字都不动（理由见 `citation_hint`）。
        lines.extend(["", hint])
    return "\n".join(lines)


def citation_hint(answer: Answer) -> str | None:
    """引用对不上时**追加在末尾**的那一行（v1.4 A1 / `PLAN §8.7` D1）。

    ⚠️ **只追加、不改正文、不把 `[n]` 换成 `[?]`**：
        · 回答是流式**已经发出去**的，事后改写会让用户看到前后两个版本；
        · `[?]` 看起来像坏了，而用户真正需要的是"这一条我没把握，你去原文核一下"；
        · 检查器自己也会误报（`app/rag/citations.py` 的判据刻意宽松），
          加一行提示是**把决定权交回用户**，删引用则是替他做决定。

    ⚠️ 措辞只陈述"**我们没核对上**"，不说"答案是错的" —— 这两件事不一样。
    """
    audit = answer.extra.get("citation_audit")
    if not isinstance(audit, dict):
        return None
    unsupported = audit.get("unsupported")
    if not isinstance(unsupported, int) or unsupported <= 0:
        return None
    return _CITATION_HINT.format(count=unsupported)


def _snippet(text: str, limit: int = 60) -> str:
    """来源摘要的**单行化 ＋ 洗表格**（实现在 `app/rag/mdtext.py::snippet`）。

    ⚠️ 摘要直接取自 chunk 原文，标题和表格行常常一起被带出来
       （`部件类别 | 期限 | 起算点 | |------|------| | 家具主体…`）。
       卡片不渲染表格 ⟹ **只降级正文是不够的**，来源区照样会露源码。
    ⚠️ 2026-10-25 起这段洗法**和 Web 引用卡共用一份**：同一个 chunk 在两个渠道
       必须长得一样，否则"群里是对的、网页上是乱的"，排查时先怀疑自己眼花。
    """
    return mdtext.snippet(text, limit=limit)


class FeishuChannel:
    """把飞书事件变成"落库的问答"，然后把答案发回去。

    ⚠️ 依赖全部**构造时注入**（`AGENTS §7.1` 约定 19）：测试要在没有 `lark`、
        没有 Redis、没有模型 API 的情况下把它跑起来。
    """

    def __init__(
        self,
        *,
        sender: FeishuSender,
        chat: ChatClient,
        embedder: EmbeddingClient | None = None,
        reranker: RerankClient | None = None,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        guard: LimitGuard | None = None,
        names: NameResolver | None = None,
    ) -> None:
        self._sender = sender
        self._chat = chat
        self._embedder = embedder
        self._reranker = reranker
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()
        #: 限流 / 配额。`None` = 不限（测试用）。生产必须传。
        self._guard = guard
        #: 真名解析（群名 / 用户名）。`None` = 不解析，直接显示 `open_id` 后 6 位。
        #: ⚠️ 拿不到名字**永远不该**影响回答 —— 解析器的实现自己吞掉所有异常。
        self._names = names
        self._seen: OrderedDict[str, None] = OrderedDict()

    # ---------- 入口 ----------

    async def handle_event(self, payload: dict[str, Any]) -> None:
        """处理一条事件。**任何异常都不许冒泡出去** —— 冒出去长连接就断了。"""
        event = parse_event(payload)
        if event is None:
            return

        if not self._remember(event.message_id):
            # ⚠️ 飞书会重投（网络抖动 / 我们回得慢）。不挡的话同一个问题答两遍，
            #    而且在库里留下两条一样的消息。
            logger.info("重复事件，已忽略 message_id=%s", event.message_id)
            return

        logger.info(
            "收到飞书消息 message_id=%s chat=%s group=%s 长度=%d",
            event.message_id,
            event.chat_id,
            event.is_group,
            len(event.text),
        )

        try:
            reply = await self._answer(event)
        except RateLimitedError:
            # ⚠️ 限流/配额是**预期内**的拒绝，要说人话，不要回"出错了"
            reply = "现在问得有点频繁（或今天的额度用完了），过一会儿再问我吧。"
        except Exception:
            logger.exception("处理飞书消息失败 message_id=%s", event.message_id)
            reply = "抱歉，这条我处理失败了。稍后再试一次，或者换个问法。"

        for chunk in split_reply(reply):
            await self._sender.send_markdown(event.chat_id, chunk)

    # ---------- 内部 ----------

    def _remember(self, message_id: str) -> bool:
        """返回 True = **第一次见**。超出容量时淘汰最旧的。"""
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > _SEEN_CAPACITY:
            self._seen.popitem(last=False)
        return True

    async def _answer(self, event: FeishuEvent) -> str:
        async with self._session_factory() as session:
            user = await self._channel_user(session)
            conversation = await self._conversation(session, user, event)
            history = await self._history(session, user, conversation)

            # 用户消息**先落库**：就算后面生成失败，这条提问也在（和 Web 端同一口径）
            self._dump(session, user, conversation, MessageRole.USER, event.text)
            await session.commit()

            if self._guard is not None:
                # ⚠️ 两道闸的语义和 Web 端一致：先次数（便宜），再配额（要读 Redis 计数）
                from app.core.limits import ensure

                ensure(await self._guard.chat_rate(user.id))
                ensure(await self._guard.quota(user.id))

            # ⚠️ **2026-10-26：顾客侧终于能查订单了**。
            #    之前这条路**完全不查订单**（`resolve_orders` 只在 Web 的 `chat.py` 里调），
            #    于是顾客问“我的单到哪了”只能得到一段政策—— 答非所问。
            #    口径与 Web **完全一致**（同一个 `order_context`，只按租户）。
            _snapshots, order_facts, order_placed_at = await order_context(
                session, tenant_id=user.tenant_id, text=event.text
            )

            pipeline = RagPipeline(
                session=session,
                tenant_id=user.tenant_id,
                chat=self._chat,
                embedder=self._embedder,
                reranker=self._reranker,
                settings=self._settings,
                # 没查到就是空串 —— **不给“没查到订单”这种句子**（给了模型会顺着编）
                order_facts=order_facts,
                # 查到就以**下单时间**为时间基准（老订单按当年条款）
                order_placed_at=order_placed_at,
            )

            final: Answer | None = None
            async for item in pipeline.answer(question=event.text, history=history):
                if isinstance(item, Answer):
                    final = item
                elif isinstance(item, Sources):
                    logger.info(
                        "飞书检索完成 citations=%d refused=%s", len(item.citations), item.refused
                    )

            if final is None:  # 理论上不会发生：pipeline 一定以 Answer 收尾
                raise RuntimeError("pipeline 没有产出 Answer")

            self._dump(
                session,
                user,
                conversation,
                MessageRole.ASSISTANT,
                final.text,
                citations=[c.as_dict() for c in final.citations],
                refused=final.refused,
                confidence=final.confidence,
                latency_ms=final.latency_ms,
                meta={"channel": "feishu", "message_id": event.message_id},
            )
            await session.commit()

            if self._guard is not None and final.usage.get("total_tokens"):
                await self._guard.record_tokens(
                    user.id,
                    int(final.usage["total_tokens"]),
                    # 这条消息记在**哪个渠道**的账上：群聊 / 私聊分开算（2026-10-25）
                    channel=conversation.channel.value,
                )

            return render_reply(final, show_sources=self._settings.feishu_show_sources)

    async def _channel_user(self, session: AsyncSession) -> User:
        """渠道账号：所有飞书消息都记在它名下（`FEISHU_CHANNEL_EMAIL`，留空回落管理员）。"""
        email = (self._settings.feishu_channel_email or self._settings.admin_email).strip().lower()
        user = await session.scalar(select_user_by_email(email))
        if user is None:
            # ⚠️ 配置问题要**说出来**，否则表现是"机器人不理我"，而日志里什么都没有
            raise RuntimeError(f"飞书渠道账号不存在：{email}（先跑 python -m app.db.seed？）")
        return user

    async def _conversation(
        self, session: AsyncSession, user: User, event: FeishuEvent
    ) -> Conversation:
        """一条飞书消息 → 库里的一个 `Conversation`。

        ⚠️ **分桶键是 `(channel, chat_id, open_id)`**，不是 `chat_id`：
            · 私聊：`chat_id` 本来就是一人一个，按谁分都一样；
            · **群聊：同一个 `chat_id` 里好几个人** —— 不按 `open_id` 分会话，
              A 的追问会读到 B 的上下文，把别人的单号 / 地址串进回答里。
              这是这个功能存在的全部理由（决策 D5 的另一半）。
        ⚠️ 靠**列**找回，不靠标题（老实现把 chat_id 编进标题，群聊就没法按人分）。
        """
        channel = Channel.FEISHU_GROUP if event.is_group else Channel.FEISHU_P2P
        repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Conversation)
        existing = await session.scalar(
            repo.select().where(
                Conversation.channel == channel,
                Conversation.external_chat_id == event.chat_id,
                Conversation.external_user_id == event.open_id,
            )
        )
        if existing is None:
            conversation = repo.build(
                user_id=user.id,
                #: 先用 id 后 6 位兜底（万一解析很慢 / 失败，也有个能认的标题）
                title=_USER_FALLBACK.format(tail=short_id(event.open_id)),
                channel=channel,
                external_chat_id=event.chat_id,
                external_user_id=event.open_id,
            )
            await repo.add(conversation)
            await session.commit()
            await self._apply_names(session, conversation)
            return conversation

        await self._maybe_refresh_names(session, existing)
        return existing

    async def _maybe_refresh_names(self, session: AsyncSession, conversation: Conversation) -> None:
        """群名 / 用户名：**首次解析，之后每 7 天刷一次**（群改名、人改名都能跟上）。

        ⚠️ 为什么不是每条消息都问：这两次都是**同步 HTTP**（飞书开放平台），
           每条消息多两趟网络往返，为了一个基本不变的名字不划算。
        """
        seen = conversation.external_title_at
        if seen is not None and datetime.now(UTC) - seen < _TITLE_TTL:
            return
        await self._apply_names(session, conversation)

    async def _apply_names(self, session: AsyncSession, conversation: Conversation) -> None:
        """查一次真名，落到 `title`（**这个人**）/ `external_title`（**群名**或这个人）。

        ⚠️ 查不到就退成 `open_id` / `chat_id` 的**后 6 位** —— 显示"飞书用户 · 3a9c1f"
           也比显示一串 `ou_...` 强，更比"因为查不到名字就不回话"强得多。
        ⚠️ **无论成功失败都写 `external_title_at`**：不然解析一直失败时，每条消息都会
           重试一次（等于给每轮回答都加两趟注定失败的 HTTP）。
        """
        person: str | None = None
        group: str | None = None
        if self._names is not None:
            person = await self._names.user_name(conversation.external_user_id or "")
            if conversation.channel is Channel.FEISHU_GROUP:
                group = await self._names.chat_name(conversation.external_chat_id or "")

        tail = short_id(conversation.external_user_id)
        conversation.title = (person or _USER_FALLBACK.format(tail=tail))[:200]
        if conversation.channel is Channel.FEISHU_GROUP:
            group_tail = short_id(conversation.external_chat_id)
            conversation.external_title = (group or _GROUP_FALLBACK.format(tail=group_tail))[:200]
        else:
            conversation.external_title = (person or _USER_FALLBACK.format(tail=tail))[:200]
        conversation.external_title_at = datetime.now(UTC)
        await session.commit()

    async def _history(
        self, session: AsyncSession, user: User, conversation: Conversation
    ) -> list[dict[str, str]]:
        """最近若干条消息，转成 pipeline 要的 `[{"role","content"}]`。

        ⚠️ **只给文本**：飞书侧没有图，`meta` 里的读数（比如 Web 端传过的图）不重放。
        """
        repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Message)
        rows = (
            await session.scalars(
                repo.select()
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
                .limit(_HISTORY_LIMIT)
            )
        ).all()
        return [
            {
                "role": "user" if row.role is MessageRole.USER else "assistant",
                "content": row.content,
            }
            for row in reversed(rows)
        ]

    def _dump(
        self,
        session: AsyncSession,
        user: User,
        conversation: Conversation,
        role: MessageRole,
        content: str,
        **extra: Any,
    ) -> Message:
        repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Message)
        message = repo.build(conversation_id=conversation.id, role=role, content=content, **extra)
        session.add(message)
        return message


def select_user_by_email(email: str):
    """按邮箱查用户。

    ⚠️ 用户表**没有 tenant 维度**（登录前就要能查），所以这里不走 `TenantScopedRepository`
        —— 那是**唯一**可以绕开它的地方，理由和 `/auth/login` 一样。
    """
    from sqlalchemy import select

    return select(User).where(User.email == email, User.is_active.is_(True))
