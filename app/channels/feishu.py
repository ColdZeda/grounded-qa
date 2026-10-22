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
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.limits import LimitGuard, RateLimitedError
from app.db.models.conversation import Conversation, Message, MessageRole
from app.db.models.user import User
from app.db.repository import TenantScopedRepository
from app.db.session import get_session_factory
from app.rag.pipeline import Answer, RagPipeline, Sources
from app.rag.providers import ChatClient, EmbeddingClient, RerankClient

logger = logging.getLogger("grounded.feishu")

#: 单条飞书**卡片**的字数上限（飞书自己允许更大，但长文在群里没人读）。
#: 超了就按段落切开分多条发 —— **截断**是不可接受的（会把结论吃掉）。
MAX_REPLY_CHARS = 3500

#: 每个飞书会话对应库里的一个 `Conversation`，标题带 chat_id 以便**跨重启**找回。
_CONVERSATION_TITLE = "飞书 · {chat_id}"

#: 进提示词的历史条数（和 Web 端同一口径：只取最近若干条，纯文本）。
_HISTORY_LIMIT = 20

#: 内存去重的容量：近期消息 id 足够，旧的自然淘汰。
_SEEN_CAPACITY = 512


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


#: 表格的**分隔行**（`|---|---|` / `|:--:|--|`）：只由 `|` `-` `:` 和空格组成。
_TABLE_SEP = re.compile(r"^[\s|:\-]+$")

#: `---` / `***` / `___` 独立成行 = 分割线。`lark_md` 不认，会原样露出。
_HRULE = re.compile(r"^ {0,3}([-*_])\s*(?:\1\s*){2,}$")

#: `## 标题`：卡片里没有标题语法，降级成整行加粗。
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*$")

#: 整行只有一个引用标记 `[3]`。
_LONE_MARKER = re.compile(r"^\s*\[(\d{1,2})\]\s*$")


def _table_rows_to_lines(rows: list[list[str]], *, has_sep: bool) -> list[str]:
    """`| a | b |` 的表格 → 列表行。飞书卡片**不支持表格**，只能降级。

    两列是最常见的形状（`部件类别 | 保修期限`），直接写成 `- key：value`；
    多列就把表头带上，写成 `- key1：v1｜key2：v2`。
    ⚠️ **一条信息都不许丢**：宁可难看，也不能把某列静默删掉。
    """
    if has_sep and len(rows) >= 2:
        header, body = rows[0], rows[1:]
    else:  # 没有分隔行 ⇒ 认不出哪行是表头，全部当数据（别拿第一行冒险）
        header, body = [], rows

    lines: list[str] = []
    for row in body:
        if len(header) == 2 and len(row) >= 2:
            lines.append(f"- {row[0]}：{row[1]}")
        elif header:
            cells = [f"{header[i]}：{row[i]}" for i in range(min(len(header), len(row))) if row[i]]
            lines.append("- " + "｜".join(cells))
        else:
            lines.append("- " + "｜".join(cell for cell in row if cell))
    return lines


def to_feishu_markdown(text: str) -> str:
    """把模型输出的 markdown **降级**成飞书卡片认得的 `lark_md`。

    `lark_md` 认：粗体 / 斜体 / 删除线 / 链接 / 有序无序列表 / 代码块 / 图片。
    **不认**这三样 —— **表格**、`#` **标题**、`---` **分割线** —— 不处理就会原样
    露出 `|---|---|`，用户在群里看到的是源码（2026-10-22 真机联调实测踩中）。
    """
    out: list[str] = []
    table: list[list[str]] = []
    has_sep = False

    def flush() -> None:
        nonlocal has_sep
        if table:
            out.extend(_table_rows_to_lines(table, has_sep=has_sep))
            table.clear()
        has_sep = False

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # 表格：连续的 `|` 行收集起来**整块**转换（表头只在第一行）
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if _TABLE_SEP.match(stripped):
                has_sep = True
            else:
                table.append([cell.strip() for cell in stripped.strip("|").split("|")])
            continue
        flush()

        if not stripped:
            out.append("")
            continue
        if _HRULE.match(line):
            out.append("")  # 换成空行：卡片里一句 "---" 没有信息量
            continue
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1)
            out.append(f"**{title}**" if title else "")
            continue
        marker = _LONE_MARKER.match(line)
        if marker:
            # 模型常把 `[3]` 单独甩一行（前面还空了一行）；并回上一行才是"这段的依据在 3"
            while out and not out[-1].strip():
                out.pop()
            if out:
                out[-1] = f"{out[-1].rstrip()} [{marker.group(1)}]"
                continue
        out.append(line)

    flush()
    return "\n".join(out).strip()


def render_reply(answer: Answer, *, show_sources: bool) -> str:
    """把 `Answer` 变成飞书里看到的文字（**已经是 `lark_md`**）。

    ⚠️ 飞书里点不了 `[1]`，所以来源给的是**文字版**（编号 + 摘要 + 分数口径），
       而且**分数必须带口径**（`精排分` / `余弦`）—— 同一个数字不同含义，
       和 Web 端是同一纪律（`architecture.md` §9.21）。
    """
    body = to_feishu_markdown(answer.text.strip())
    if not body:
        body = "（没有生成出内容）"
    if not show_sources or not answer.citations:
        return body

    lines = [body, "", "**—— 参考来源 ——**"]
    for cite in answer.citations:
        snippet = " ".join(cite.snippet.split())[:60]
        lines.append(f"**[{cite.index}]** {snippet}（{cite.score_kind} {cite.score:.3f}）")
    return "\n".join(lines)


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
    ) -> None:
        self._sender = sender
        self._chat = chat
        self._embedder = embedder
        self._reranker = reranker
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()
        #: 限流 / 配额。`None` = 不限（测试用）。生产必须传。
        self._guard = guard
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
            conversation = await self._conversation(session, user, event.chat_id)
            history = await self._history(session, user, conversation)

            # 用户消息**先落库**：就算后面生成失败，这条提问也在（和 Web 端同一口径）
            self._dump(session, user, conversation, MessageRole.USER, event.text)
            await session.commit()

            if self._guard is not None:
                # ⚠️ 两道闸的语义和 Web 端一致：先次数（便宜），再配额（要读 Redis 计数）
                from app.core.limits import ensure

                ensure(await self._guard.chat_rate(user.id))
                ensure(await self._guard.quota(user.id))

            pipeline = RagPipeline(
                session=session,
                tenant_id=user.tenant_id,
                chat=self._chat,
                embedder=self._embedder,
                reranker=self._reranker,
                settings=self._settings,
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
                await self._guard.record_tokens(user.id, int(final.usage["total_tokens"]))

            return render_reply(final, show_sources=self._settings.feishu_show_sources)

    async def _channel_user(self, session: AsyncSession) -> User:
        """渠道账号：所有飞书消息都记在它名下（`FEISHU_CHANNEL_EMAIL`，留空回落管理员）。"""
        email = (self._settings.feishu_channel_email or self._settings.admin_email).strip().lower()
        user = await session.scalar(select_user_by_email(email))
        if user is None:
            # ⚠️ 配置问题要**说出来**，否则表现是"机器人不理我"，而日志里什么都没有
            raise RuntimeError(f"飞书渠道账号不存在：{email}（先跑 python -m app.db.seed？）")
        return user

    async def _conversation(self, session: AsyncSession, user: User, chat_id: str) -> Conversation:
        """一个飞书会话 ↔ 库里的一个 `Conversation`。**跨重启**靠标题里的 chat_id 找回。"""
        repo = TenantScopedRepository(session, tenant_id=user.tenant_id, model=Conversation)
        title = _CONVERSATION_TITLE.format(chat_id=chat_id)
        existing = await session.scalar(
            repo.select().where(Conversation.user_id == user.id, Conversation.title == title)
        )
        if existing is not None:
            return existing

        conversation = repo.build(user_id=user.id, title=title)
        await repo.add(conversation)
        await session.commit()
        return conversation

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
