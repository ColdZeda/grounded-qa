"""提问带图：读图 → 检索用 scene → 订单注入 → 降级。

四个断言守着四种"错了也不报错"的情况：

    ① **scene 真的进了检索** —— 否则图等于白传（用户只写"这能退吗"，光靠这句话检索不到"破损"）
    ② **单号进了上下文、但没进检索 query** —— 精确串进 query 只会搅乱召回
    ③ **图读失败要显式告知** —— 静默丢图会让用户以为我们看过图了
    ④ **别人的图一律 404** —— 和订单同一条纪律

⚠️ 依旧**绝不真调模型**：`dependency_overrides` 换掉 `get_clients` 与 `get_image_client`。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import Account
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients, get_image_client
from app.core.config import get_settings
from app.core.limits import peek_quota
from app.db.models.conversation import Conversation, MessageRole
from app.db.models.conversation import Message as MessageRow
from app.db.models.image import Image
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.order import Order, OrderStatus
from app.db.models.user import UserRole
from app.rag.providers import ModelAPIError
from app.rag.vision import ImageAnalysis

PREFIX = get_settings().api_prefix


class FakeChat:
    """记录收到的 messages，吐一句固定回答。"""

    def __init__(self, pieces: Sequence[str] = ("好的", "，我来看看。")) -> None:
        self.pieces = pieces
        self.calls: list[list[dict[str, str]]] = []

    async def stream(
        self, messages: Sequence[dict[str, str]], usage: dict[str, int] | None = None
    ) -> AsyncIterator[str]:
        self.calls.append([dict(item) for item in messages])
        for piece in self.pieces:
            yield piece
        if usage is not None:
            usage.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class FakeImageClient:
    """假的读图客户端：要么返回固定读数，要么按指令抛错。"""

    def __init__(
        self,
        *,
        scene: str = "包裹内泡沫填充物碎裂",
        transcription: str = "订单号：SF10001",
        image_type: str = "photo",
        error: str | None = None,
        total_tokens: int = 15,
    ) -> None:
        self.scene = scene
        self.transcription = transcription
        self.image_type = image_type
        self.error = error
        self.total_tokens = total_tokens
        self.calls = 0

    async def analyze(self, image: bytes, *, content_type: str) -> ImageAnalysis:
        self.calls += 1
        if self.error:
            raise ModelAPIError(self.error)
        return ImageAnalysis(
            image_type=self.image_type,
            scene=self.scene,
            transcription=self.transcription,
            has_exact_fields=True,
            model="fake-vl",
            usage={"total_tokens": self.total_tokens},
        )


def _events(raw: str) -> dict[str, list[dict[str, Any]]]:
    """把 SSE 文本解析成 `{事件名: [data, ...]}`。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for frame in raw.split("\n\n"):
        name = data = ""
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name and data:
            out.setdefault(name, []).append(json.loads(data))
    return out


async def _seed_chunk(session: AsyncSession, account: Account, content: str) -> None:
    """落一篇文档 ＋ 一个块。**不给向量** —— 这样只走全文路，confidence=None，
    天然不会触发拒答（我们要测的是"带图之后的生成上下文"，不是拒答）。"""
    document = Document(
        tenant_id=account.tenant_id,
        filename=f"senyu/{uuid.uuid4().hex[:8]}.md",
        content_type="text/markdown",
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
        size_bytes=len(content),
    )
    session.add(document)
    await session.flush()
    session.add(
        Chunk(
            tenant_id=account.tenant_id,
            document_id=document.id,
            ordinal=0,
            content=content,
        )
    )
    await session.commit()


async def _new_conversation(session: AsyncSession, account: Account) -> uuid.UUID:
    conversation = Conversation(
        tenant_id=account.tenant_id, user_id=account.user_id, title="带图测试"
    )
    session.add(conversation)
    await session.commit()
    return conversation.id


async def _add_order(session: AsyncSession, account: Account, order_no: str) -> None:
    session.add(
        Order(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            order_no=order_no,
            status=OrderStatus.DELIVERED,
            placed_at=datetime.now(UTC) - timedelta(days=10),
            delivered_at=datetime.now(UTC) - timedelta(days=5),
            items=[{"name": "实木床架", "qty": 1}],
            amount_cents=128_000,
            currency="CNY",
        )
    )
    await session.commit()


async def _upload_image(client: AsyncClient, account: Account, token: str) -> str:
    """走真接口上传一张最小 PNG（1×1 白点）。返回 `image_id`。"""
    from io import BytesIO

    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    resp = await client.post(
        f"{PREFIX}/images",
        headers=account.auth(token),
        files={"file": ("a.png", buffer.getvalue(), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_scene_enters_retrieval_and_order_enters_context(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    account: Account = await make_account(role=UserRole.ADMIN, label="img-chat")
    token = await login(account.email, account.password)
    await _add_order(session, account, "SF10001")
    # 知识库里只放运输破损条款 —— "泡沫填充物碎裂"这个词只在图里，不在用户原话里
    await _seed_chunk(
        session,
        account,
        "泡沫填充物碎裂属于运输破损：签收后 48 小时内报障，需提供外箱六面照片。",
    )
    conversation_id = await _new_conversation(session, account)

    chat = FakeChat()
    image_client = FakeImageClient()
    app.dependency_overrides[get_clients] = lambda: (chat, None, None)
    app.dependency_overrides[get_image_client] = lambda: image_client

    image_id = await _upload_image(client, account, token)
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=account.auth(token),
        json={"question": "这个怎么处理", "image_id": image_id},
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)

    # ① 图被读了、状态发出来了
    assert image_client.calls == 1
    image_payload = events["sources"][0]["image"]
    assert image_payload["analyzed"] is True
    assert "泡沫" in image_payload["note"]

    # ② scene 进了检索："泡沫填充物碎裂"只在图里，用户原话是"这个怎么处理"
    #    —— 命中就证明：检索用的是 scene 拼上原话，而不只是原话
    assert events["sources"][0]["citations"], "scene 没有参与检索：一个候选都没召回"

    # ③ 订单与图都进了提示词
    system = chat.calls[0][0]["content"]
    assert "【用户上传的图片】" in system
    assert "【订单信息】" in system
    assert "SF10001" in system
    assert "实木床架" in system

    # ④ 读数落进用户消息的 meta（不污染 content）
    # ⚠️ **必须筛 `role=USER` 而且排序**：一个会话里还有助手那条消息，
    #    不带条件的 `select` 返回顺序**由 PG 决定**（数据量变了就可能换执行计划）——
    #    2026-10-23 全量跑时它就真的把助手那句 "好的，我来看看。" 当成了用户消息。
    #    这类"偶尔红"的断言比没有断言更糟：它会被当成噪音跳过。
    user_message = await session.scalar(
        select(MessageRow)
        .where(
            MessageRow.conversation_id == conversation_id,
            MessageRow.role == MessageRole.USER,
        )
        .order_by(MessageRow.created_at)
    )
    assert user_message is not None
    assert user_message.content == "这个怎么处理"
    assert user_message.meta is not None
    assert user_message.meta["image_id"] == image_id
    assert user_message.meta["orders"][0]["order_no"] == "SF10001"

    # ⑤ 读图结果也落库了（badcase 复盘要看"当时读到了什么"）
    row = await session.get(Image, uuid.UUID(image_id))
    assert row is not None and row.analysis is not None
    assert row.analysis["scene"].startswith("包裹")


async def test_unknown_order_is_not_injected(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """图里有个查不到的单号 → **不许塞任何订单文本**（没有材料就编不出来）。"""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    account: Account = await make_account(label="no-order")
    token = await login(account.email, account.password)
    await _seed_chunk(session, account, "退款时效：信用卡 3~5 个工作日到账。")
    # 这两条不考察召回质量：把全文门槛降到 0，保证有块就能进生成阶段
    monkeypatch.setattr(get_settings(), "fulltext_min_similarity", 0.0)
    conversation_id = await _new_conversation(session, account)

    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, None, None)
    app.dependency_overrides[get_image_client] = lambda: FakeImageClient(
        transcription="订单号：SF99999"
    )

    image_id = await _upload_image(client, account, token)
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=account.auth(token),
        json={"question": "我这个单什么时候退款", "image_id": image_id},
    )
    assert resp.status_code == 200, resp.text

    system = chat.calls[0][0]["content"]
    assert "【订单信息】" not in system
    assert "SF99999" in system  # 转写进了上下文（它确实印在图上）……
    # ……但没有"这是我们的订单"这种说法
    assert "订单号 SF99999：" not in system


async def test_image_failure_degrades_explicitly(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """读图挂了 → 明确告知 + 仍然按文字回答（不静默丢图、也不整条失败）。"""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    account: Account = await make_account(label="vl-down")
    token = await login(account.email, account.password)
    await _seed_chunk(session, account, "无理由退货的运费由顾客承担。")
    # 这两条不考察召回质量：把全文门槛降到 0，保证有块就能进生成阶段
    monkeypatch.setattr(get_settings(), "fulltext_min_similarity", 0.0)
    conversation_id = await _new_conversation(session, account)

    chat = FakeChat()
    app.dependency_overrides[get_clients] = lambda: (chat, None, None)
    app.dependency_overrides[get_image_client] = lambda: FakeImageClient(error="boom")

    image_id = await _upload_image(client, account, token)
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=account.auth(token),
        json={"question": "退货谁出运费", "image_id": image_id},
    )
    assert resp.status_code == 200, resp.text
    events = _events(resp.text)

    image_payload = events["sources"][0]["image"]
    assert image_payload["analyzed"] is False
    assert "没读成功" in image_payload["error"]
    # 降级后照样答了（不因为图挂了就 500 或者拒答）
    assert "delta" in events
    system = chat.calls[0][0]["content"]
    assert "【用户上传的图片】" not in system

    # 失败也要留痕
    row = await session.get(Image, uuid.UUID(image_id))
    assert row is not None and row.analysis_error


async def test_other_users_image_is_404(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    owner: Account = await make_account(label="img-owner")
    other: Account = await make_account(label="img-other")
    owner_token = await login(owner.email, owner.password)
    other_token = await login(other.email, other.password)
    conversation_id = await _new_conversation(session, other)

    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    app.dependency_overrides[get_image_client] = lambda: FakeImageClient()

    image_id = await _upload_image(client, owner, owner_token)
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=other.auth(other_token),
        json={"question": "这个怎么处理", "image_id": image_id},
    )
    assert resp.status_code == 404, resp.text


async def test_without_image_nothing_changes(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
) -> None:
    """不带图的老路径：不发 image 字段、不调读图客户端（回归守卫）。"""
    account: Account = await make_account(label="plain")
    token = await login(account.email, account.password)
    await _seed_chunk(session, account, "七天无理由退货，自签收之日起算。")
    conversation_id = await _new_conversation(session, account)

    image_client = FakeImageClient()
    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    app.dependency_overrides[get_image_client] = lambda: image_client

    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=account.auth(token),
        json={"question": "退货多少天"},
    )
    assert resp.status_code == 200, resp.text
    assert "image" not in _events(resp.text)["sources"][0]
    assert image_client.calls == 0


async def test_vision_tokens_are_added_to_quota(
    client: AsyncClient,
    app: Any,
    session: AsyncSession,
    make_account: Any,
    login: Any,
    tmp_path: Any,
    monkeypatch: Any,
    limits_redis: Any,
) -> None:
    """⚠️ **读图也要记账**（`PLAN §8.3` D9）。

    不记的话，"用图片提问"就是一条免费通道 —— 而它明明是一次模型调用。
    这条用例同时守两件事：token 进了账、且**账本用的是同一个 subject**（否则配额页看不见）。
    """
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    account: Account = await make_account(label="vl-quota")
    token = await login(account.email, account.password)
    await _seed_chunk(session, account, "退款时效：信用卡 3~5 个工作日到账。")
    monkeypatch.setattr(get_settings(), "fulltext_min_similarity", 0.0)
    conversation_id = await _new_conversation(session, account)

    app.dependency_overrides[get_clients] = lambda: (FakeChat(), None, None)
    app.dependency_overrides[get_image_client] = lambda: FakeImageClient(total_tokens=77)

    image_id = await _upload_image(client, account, token)
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        headers=account.auth(token),
        json={"question": "什么时候到账", "image_id": image_id},
    )
    assert resp.status_code == 200, resp.text

    used = await peek_quota(limits_redis, subject=str(account.user_id))
    # 读图 77 ＋ 生成 15
    assert used is not None and used >= 77
