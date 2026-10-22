"""飞书长连接进程：`python -m app.channels.feishu_runner`。

## 它是个**独立进程**，不是一个路由

长连接要一直挂着，放进 API 进程里会跟着重启、也把"请求-响应"和"常驻连接"两种生命周期
混在一起。所以它单独跑（compose 里一个 `feishu` 服务），API 那边**完全不知道**它的存在。

## `lark-oapi` 是**延迟导入**的

它包在 `pyproject.toml` 的可选 extra 里（`pip install .[feishu]`）。这样：
· 没装它时 `pytest`、API、worker 一切照旧（测试也不需要它）
· `--check` 模式**连 import 都不做** —— 用来确认"配置对不对"，不该要求装依赖

## 两种退出原因要分清（这是运维最常问的）

```
FEISHU_ENABLED=false        → 打一行日志、退出码 0（预期行为，不是故障）
FEISHU_ENABLED=true 但缺凭据 → 退出码 2，并说清缺哪个（配置错误，重启也没用）
```
⚠️ **别把这两种混成一个非零退出码** —— 容器一直重启、日志刷屏，而真正原因被埋掉。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
from typing import Any

from app.channels.feishu import FeishuChannel
from app.core.config import Settings, get_settings
from app.core.limits import LimitGuard
from app.rag.pipeline import build_clients

logger = logging.getLogger("grounded.feishu")


def _missing_credentials(settings: Settings) -> list[str]:
    """缺哪些非空项。**只回名字，不回值** —— 凭据不进日志。"""
    required = {
        "FEISHU_APP_ID": settings.feishu_app_id,
        "FEISHU_APP_SECRET": settings.feishu_app_secret,
    }
    return [name for name, value in required.items() if not value.strip()]


class LarkSender:
    """把消息发回飞书。

    ⚠️ `lark.Client` 的方法是**同步阻塞**的，所以调用点用 `asyncio.to_thread` 包一层 ——
       否则一次网络抖动会卡住整个事件循环（长连接的心跳也会跟着抖）。
    """

    def __init__(self, settings: Settings) -> None:
        import lark_oapi as lark

        self._lark = lark
        self._client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            # 只开消息相关能力，别的都关掉 —— 用不到的权限面就是多余的风险面
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def _send(self, chat_id: str, markdown: str) -> None:
        # ⚠️ 发**卡片**（`msg_type=interactive`），不是纯文本：飞书**文本消息不渲染
        #    markdown**，真机联调时用户看到的是 `**保修期按部件类别…**` 和 `|---|---|`
        #    （2026-10-22 实测）。卡片里的 `lark_md` 由飞书渲染，
        #    我们只负责先把 `lark_md` 不认的语法降级掉（`feishu.to_feishu_markdown`）。
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}],
        }
        body = (
            self._lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            self._lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        # ⚠️ 飞书的 HTTP 200 **不代表业务成功**（告警那条坑同理，`AGENTS §10`）：
        #    必须看 `success()` / `code`，否则"没发出去"会被记成"发过了"。
        if not response.success():
            logger.error(
                "飞书发送失败 code=%s msg=%s chat=%s",
                response.code,
                response.msg,
                chat_id,
            )

    async def send_markdown(self, chat_id: str, markdown: str) -> None:
        await asyncio.to_thread(self._send, chat_id, markdown)


def _event_payload(data: Any) -> dict[str, Any]:
    """把 `lark` 的事件对象**手工**拼成回调那种 JSON 结构。

    ⚠️ 为什么不用 `lark.JSON.marshal(data)`：那样会把整个 SDK 的字段布局都算进依赖面，
       升级 SDK 时形状一变就悄悄解析不出东西。这里只取我们真正要的 6 个字段，
       **和 `feishu.parse_event` 的约定一一对应**，多一个都不要。
    """
    message = data.event.message
    sender = data.event.sender
    sender_id = getattr(sender, "sender_id", None)
    return {
        "event": {
            "message": {
                "message_id": message.message_id,
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "message_type": message.message_type,
                "content": message.content,
            },
            "sender": {
                "sender_type": sender.sender_type,
                "sender_id": {"open_id": getattr(sender_id, "open_id", None)},
            },
        }
    }


def _run_forever(loop: asyncio.AbstractEventLoop) -> None:
    """在子线程里跑一个自己的事件循环（我们所有异步活都放这里）。"""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _serve(settings: Settings) -> None:
    """**主线程跑 lark 长连接**，把我们的异步活放到另一个线程的 loop 上。

    ⚠️⚠️ 为什么必须这样分工（2026-10-22 实测踩到的坑）：
        `lark_oapi.ws.Client` 在**构造时**就用 `get_event_loop()` 抓住了当时的 loop，
        之后 `start()` 里调 `self._loop.run_until_complete(...)`。
        如果构造发生在**已经有 loop 在跑**的主线程（`asyncio.run(...)` 里面），
        再把它丢到别的线程去 `start()`，就会当场炸：
            `RuntimeError: This event loop is already running`
        而且它**不会**让进程退出（异常只打在子线程里），表现是"容器 healthy、
        日志说已启动、但飞书那边一直连接失败" —— 极具误导性。
        ⟹ 反过来：让 lark **独占主线程**（它自己建 loop，阻塞在此），
          我们的 asyncio 活另开一条命名线程。
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    chat, embedder, reranker = build_clients(settings)
    if chat is None:
        logger.error("飞书渠道起不来：没有对话模型 key（`LLM_API_KEY` 为空）")
        raise SystemExit(2)

    work_loop = asyncio.new_event_loop()
    threading.Thread(target=_run_forever, args=(work_loop,), name="gq-async", daemon=True).start()

    channel = FeishuChannel(
        sender=LarkSender(settings),
        chat=chat,
        embedder=embedder,
        reranker=reranker,
        settings=settings,
        # ⚠️ 飞书来的问题也要计入限流/配额 —— **渠道不是免费的旁路**（Web 端那两把尺子
        #    按 user_id 记，飞书消息都记在渠道账号上，所以天然共享同一份额度）
        guard=LimitGuard(_redis_client(), settings),
    )

    def on_message(data: P2ImMessageReceiveV1) -> None:
        """**同步**回调（lark 在它自己的线程里调）。只做一件事：把活丢给我们的 loop。"""
        try:
            payload = _event_payload(data)
        except Exception:  # 结构不对就丢这一条，别把长连接搞崩
            logger.exception("飞书事件结构异常，已忽略")
            return
        future = asyncio.run_coroutine_threadsafe(channel.handle_event(payload), work_loop)
        future.add_done_callback(
            lambda fut: fut.exception() and logger.error("处理飞书消息异常：%s", fut.exception())
        )

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    ws_client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("飞书长连接启动中（出站，不需要任何入站端口）")
    # ⚠️ 阻塞调用（自带重连循环）。**在哪个线程调用，它就用哪个线程的 loop**，
    #    所以必须在主线程、且此刻主线程没有正在跑的 loop —— 见函数头。
    ws_client.start()
    logger.info("飞书长连接已退出")


def _redis_client():
    """拿一个 Redis 客户端（限流/配额要用）。

    ⚠️ 复用 `app.workers.queue.get_redis`（**同一个连接池工厂**，API / worker / 飞书渠道
       共用一套配置）—— 不在这里另造一个，否则 `REDIS_URL` 有两处解析逻辑。
    ⚠️ 不复用 FastAPI 的 `Depends(get_limits_redis)`：那是请求级注入，独立进程里没有请求上下文。
    """
    from app.workers.queue import get_redis

    return get_redis()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞书长连接渠道")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查配置（不连飞书、不加载 lark-oapi），用来排查'容器起来了但没连上'",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()

    if args.check:
        missing = _missing_credentials(settings)
        print(f"FEISHU_ENABLED = {settings.feishu_enabled}")
        print(f"缺凭据: {missing or '无'}")
        print(f"渠道账号: {settings.feishu_channel_email or settings.admin_email}")
        return 0 if (not settings.feishu_enabled or not missing) else 2

    if not settings.feishu_enabled:
        logger.info("FEISHU_ENABLED=false，飞书渠道不启动（这是正常退出）")
        return 0

    missing = _missing_credentials(settings)
    if missing:
        logger.error("飞书渠道起不来：缺 %s", "、".join(missing))
        return 2

    try:
        # ⚠️ **不 `asyncio.run(...)`**：那会在主线程建一个正在跑的 loop，
        #    而 lark 的 `ws.Client` 要在主线程 `run_until_complete` 它自己抓到的 loop
        #    —— 两者撞车就是 "This event loop is already running"。见 `_serve` 的函数头。
        _serve(settings)
    except KeyboardInterrupt:  # 本机 Ctrl-C
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
