"""飞书**真名**解析：群名 / 用户名（走查 P1-⑥）。

为什么要它：会话列表里如果只有 `oc_9f3a…` / `ou_7b21…`，坐席根本认不出是谁在问 ——
"+1 个会话"没有意义。

⚠️ **拿不到就降级，绝不挡回答。** 不在通讯录可见范围、没加权限、接口抖一下 ——
   全都退成 `open_id` 后 6 位。宁可显示「飞书用户 · 3a9c1f」，也不能因为查不到名字就不回话。

⚠️ 需要的两个权限（用户去开放平台加，改完**必须重新发版**才生效，`AGENTS §10` 踩过）：
   · `im:chat:readonly` → `GET /open-apis/im/v1/chats/{chat_id}` 读群名
   · **`contact:contact.base:readonly`**（获取**通讯录**基本信息）→
     `GET /open-apis/contact/v3/users/{open_id}` 读用户名

   ⚠️⚠️ **别加 `contact:user.base:readonly`（获取用户基本信息）—— 那个不行。**
   2026-10-25 真机踩过：加上它、发版、重启容器，接口仍然报
   `99991672 Access denied. One of the following scopes is required:
   [contact:contact.base:readonly, contact:contact:access_as_app,
   contact:contact:readonly, contact:contact:readonly_as_app]`。
   飞书把通讯录那套 scope 改过名，`contact:user.base:readonly` 是**旧名字**（界面上还留着，
   点得动、也会显示"已开通"），但**不满足这个接口的校验**。
   判据：日志里必须没有 `读用户名失败 code=99991672`。

⚠️ **只取名字**：头像 / union_id / 手机号 / 部门 / 邮箱**一律不读、不存**（用户 2026-10-24 明确）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger("grounded.feishu.names")


def short_id(value: str | None) -> str:
    """`ou_7f3a9c1b2d` → `9c1b2d`（后 6 位）。拿不到真名时用它，至少两个会话不会长得一样。"""
    text = (value or "").strip()
    return text[-6:] if len(text) >= 6 else (text or "……")


class NameResolver(Protocol):
    """解析飞书里的名字。**实现方拿不到就返回 `None`**，不要抛。"""

    async def chat_name(self, chat_id: str) -> str | None: ...

    async def user_name(self, open_id: str) -> str | None: ...


class LarkNameResolver:
    """走飞书开放平台读名字。

    ⚠️ `lark.Client` 的方法是**同步阻塞**的 → 一律 `asyncio.to_thread`（和 `LarkSender` 同一条）。
    ⚠️ **必须看 `success()`**：飞书返回 HTTP 200 也可能是权限不足（`code=99991672` 之类），
       只看 HTTP 状态码会把"没权限"当成"这个人就叫空字符串"。
    """

    def __init__(self, settings: Settings) -> None:
        import lark_oapi as lark

        self._lark = lark
        self._client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def _chat_name(self, chat_id: str) -> str | None:
        request = self._lark.im.v1.GetChatRequest.builder().chat_id(chat_id).build()
        response = self._client.im.v1.chat.get(request)
        if not response.success():
            logger.info("读群名失败 code=%s msg=%s chat=%s", response.code, response.msg, chat_id)
            return None
        name = getattr(response.data, "name", None) if response.data else None
        return (name or "").strip() or None

    def _user_name(self, open_id: str) -> str | None:
        request = (
            self._lark.contact.v3.GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._client.contact.v3.user.get(request)
        if not response.success():
            logger.info(
                "读用户名失败 code=%s msg=%s open_id=%s", response.code, response.msg, open_id
            )
            return None
        user = getattr(response.data, "user", None) if response.data else None
        name = getattr(user, "name", None)
        return (name or "").strip() or None

    async def chat_name(self, chat_id: str) -> str | None:
        return await self._call(self._chat_name, chat_id)

    async def user_name(self, open_id: str) -> str | None:
        return await self._call(self._user_name, open_id)

    async def _call(self, fn, arg: str) -> str | None:
        """统一兜底：**任何异常都退成 `None`**，绝不让"查不到名字"变成"这条消息不回"。

        ⚠️ 但要把异常**记下来**：权限没配好时日志里得看得见，否则就成了"永远显示 open_id 后 6 位、
        却没人知道为什么"的静默降级（`AGENTS §10` 那个病）。
        """
        if not arg:
            return None
        try:
            return await asyncio.to_thread(fn, arg)
        except Exception as exc:  # 名字解析失败永远不该影响回答
            logger.info("解析飞书名字时出错（已降级）：%s: %s", type(exc).__name__, exc)
            return None


__all__ = ["LarkNameResolver", "NameResolver", "short_id"]
