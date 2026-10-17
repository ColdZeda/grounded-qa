"""文档上传 / 列表 / 异步入库（**集成测试**，需要真实数据库）。

⚠️ 这个功能最坏的结果不是报错，是**静默**：用户传了文件、界面显示成功、
    而知识库里什么都没有 —— 之后问什么都拒答，还找不到原因。
    所以这里的断言集中在三件事：

      · 判重命中时**不重复入库**，但要如实告诉用户（不是错误）
      · 失败时 `status=FAILED` ＋ `error_message` **必须落库**
      · 处理完之后卷上的原始文件**要删掉**（卷是传送带，不是存储）

⚠️ 入队这一步用**假实现**替掉（`get_enqueue` 依赖覆盖）：真 Redis 不参与，
    因为这里要验的是"上传接口做了什么决定"，不是 Redis 通不通。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.documents import get_enqueue
from app.core.config import get_settings
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.user import UserRole
from app.kb.uploads import upload_path
from app.workers.main import handle_job
from app.workers.queue import IngestJob

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix

DOC = "# 退换货\n\n无理由退货：签收后 7 天内可申请。\n\n## 例外\n\n定制商品不适用。\n"


class FakeEmbedder:
    """假 embedding 客户端（AGENTS §7.1 约定 9：测试不许真调模型 API）。

    ⚠️ 维度必须和 `chunks.embedding` 的列一致（`EMBEDDING_DIM`），
        否则写库时会直接报维度不符 —— 那是**测试之外**的失败，不该混进来。
    """

    model = "fake-embedding"
    dim = 1024

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.01] * self.dim for _ in texts]


class RecordingEnqueue:
    """记录被排队的任务；也能按需要装成"队列挂了"。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.jobs: list[IngestJob] = []
        self.boom = boom

    async def __call__(self, job: IngestJob) -> None:
        if self.boom:
            raise ConnectionError("redis 不可用")
        self.jobs.append(job)


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把上传目录挪到临时目录。

    ⚠️ **autouse**：忘了加的话，测试会把文件写到 `repo/uploads/`，
        跑完留下一堆垃圾 —— 而且这种"测试污染工作区"的问题没人会去查。
    """
    target = tmp_path / "uploads"
    monkeypatch.setattr(get_settings(), "upload_dir", str(target))
    return target


@pytest.fixture
def enqueue() -> RecordingEnqueue:
    return RecordingEnqueue()


@pytest.fixture
def wire(app, enqueue: RecordingEnqueue) -> RecordingEnqueue:
    """把入队换成假实现。**请求时才读覆盖表**，所以在测试体里设置也生效。"""
    app.dependency_overrides[get_enqueue] = lambda: enqueue
    return enqueue


@pytest.fixture
def factory(db_engine) -> async_sessionmaker[AsyncSession]:
    """给 worker 用的会话工厂（和 `client` 一样绑测试引擎，不碰全局引擎）。"""
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


def _file(name: str = "退货政策.md", body: str = DOC) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, body.encode("utf-8"), "text/markdown")}


async def _admin_headers(make_account, login) -> tuple[dict[str, str], object]:
    account = await make_account(role=UserRole.ADMIN)
    token = await login(account.email, account.password)
    return account.auth(token), account


# ------------------------------------------------------------
# 上传：接受与拒绝
# ------------------------------------------------------------
async def test_upload_accepts_text_and_enqueues_once(
    client: AsyncClient, wire, make_account, login
):
    headers, account = await _admin_headers(make_account, login)

    resp = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())

    # 202（已接受、还没做完），不是 201
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["status"] == DocumentStatus.PENDING.value
    assert body["filename"] == "upload/退货政策.md"
    # 入库是异步的：这一刻还没有块
    assert len(wire.jobs) == 1
    assert wire.jobs[0].tenant_id == account.tenant_id
    assert wire.jobs[0].document_id == uuid.UUID(body["id"])


async def test_upload_rejects_pdf_with_415(client: AsyncClient, wire, make_account, login):
    headers, _ = await _admin_headers(make_account, login)

    resp = await client.post(f"{PREFIX}/documents", headers=headers, files=_file(name="说明书.pdf"))

    assert resp.status_code == 415
    assert "只接受文本类文件" in resp.json()["detail"]
    assert wire.jobs == []


async def test_upload_rejects_empty_file(client: AsyncClient, wire, make_account, login):
    headers, _ = await _admin_headers(make_account, login)

    resp = await client.post(f"{PREFIX}/documents", headers=headers, files=_file(body=""))

    assert resp.status_code == 400
    assert wire.jobs == []


async def test_upload_rejects_oversized_file(client: AsyncClient, wire, make_account, login):
    headers, _ = await _admin_headers(make_account, login)
    too_big = "字" * (get_settings().upload_max_bytes + 10)

    resp = await client.post(f"{PREFIX}/documents", headers=headers, files=_file(body=too_big))

    assert resp.status_code == 413
    assert wire.jobs == []


async def test_member_cannot_upload(client: AsyncClient, wire, make_account, login):
    """知识库是租户共有的 —— 上传是管理员动作（否则谁都能往别人上下文里塞东西）。"""
    account = await make_account(role=UserRole.MEMBER)
    token = await login(account.email, account.password)

    resp = await client.post(f"{PREFIX}/documents", headers=account.auth(token), files=_file())

    assert resp.status_code == 403
    assert wire.jobs == []


async def test_upload_requires_auth(client: AsyncClient, wire, make_account, login):
    await _admin_headers(make_account, login)

    resp = await client.post(f"{PREFIX}/documents", files=_file())

    assert resp.status_code == 401
    assert wire.jobs == []


# ------------------------------------------------------------
# 判重：不是错误，但要如实说
# ------------------------------------------------------------
async def test_same_content_twice_is_not_an_error_and_does_not_requeue(
    client: AsyncClient, wire, make_account, login
):
    headers, _ = await _admin_headers(make_account, login)

    first = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())
    second = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())

    assert first.status_code == 202 and first.json()["created"] is True
    assert second.status_code == 202 and second.json()["created"] is False
    # ⚠️ 关键：**没有第二次入队** —— 重复的消息会白烧一遍 embedding
    assert len(wire.jobs) == 1
    # 而且返回的是**同一行**（同一个 id），不是又建了一份
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["note"]


async def test_upload_after_failure_is_allowed(
    client: AsyncClient, wire, make_account, login, session
):
    """失败过的文档**允许被重新上传顶掉** —— 否则同样的字节会一直撞上那条失败记录。"""
    headers, _ = await _admin_headers(make_account, login)

    first = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())
    document = await session.scalar(
        select(Document).where(Document.id == uuid.UUID(first.json()["id"]))
    )
    assert document is not None
    document.status = DocumentStatus.FAILED
    document.error_message = "人为造的失败"
    await session.commit()

    again = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())

    assert again.json()["created"] is True
    assert again.json()["id"] != first.json()["id"]
    assert len(wire.jobs) == 2


async def test_enqueue_failure_is_recorded_not_swallowed(
    client: AsyncClient, app, make_account, login, session
):
    """队列挂了 → 503，**并且**那条文档要留成 FAILED 带原因。"""
    app.dependency_overrides[get_enqueue] = lambda: RecordingEnqueue(boom=True)
    headers, account = await _admin_headers(make_account, login)

    resp = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())

    assert resp.status_code == 503
    # ⚠️ 必须按租户过滤：开发库里本来就有 A 库的文档，`select(Document)` 会捞到别人的
    document = await session.scalar(select(Document).where(Document.tenant_id == account.tenant_id))
    assert document is not None
    assert document.status is DocumentStatus.FAILED
    assert "入队失败" in (document.error_message or "")
    # 没排上队的文件不该留在盘上
    assert not upload_path(document.id).exists()


# ------------------------------------------------------------
# 列表
# ------------------------------------------------------------
async def test_list_shows_status_and_isolates_tenants(
    client: AsyncClient, wire, make_account, login
):
    mine, _ = await _admin_headers(make_account, login)
    other_account = await make_account(role=UserRole.ADMIN, label="other")
    other_token = await login(other_account.email, other_account.password)

    await client.post(f"{PREFIX}/documents", headers=mine, files=_file())
    await client.post(
        f"{PREFIX}/documents",
        headers=other_account.auth(other_token),
        files=_file(name="别人的.md", body="# 别的租户的内容\n"),
    )

    resp = await client.get(f"{PREFIX}/documents", headers=mine)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [item["filename"] for item in body["items"]] == ["upload/退货政策.md"]


# ------------------------------------------------------------
# worker：真的把文件变成块
# ------------------------------------------------------------
async def test_worker_turns_upload_into_chunks(
    client: AsyncClient, wire, make_account, login, session, factory
):
    headers, _ = await _admin_headers(make_account, login)
    uploaded = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())
    document_id = uuid.UUID(uploaded.json()["id"])

    embedder = FakeEmbedder()
    await handle_job(
        wire.jobs[0].dump(),
        embedder=embedder,
        settings=get_settings(),
        factory=factory,
    )

    document = await session.scalar(select(Document).where(Document.id == document_id))
    assert document is not None
    assert document.status is DocumentStatus.READY
    assert document.error_message is None

    chunks = list(await session.scalars(select(Chunk).where(Chunk.document_id == document_id)))
    assert chunks, "切完必须有块，否则这个文档在检索里等于不存在"
    assert all(chunk.embedding is not None for chunk in chunks)
    # 元数据锁的两半要跟着向量一起写（约定 3）
    assert {chunk.embedding_model for chunk in chunks} == {embedder.model}
    assert {chunk.embedding_dim for chunk in chunks} == {embedder.dim}
    # 块要带标题路径前缀，引用摘要才说得清出处
    assert any(chunk.content.startswith("[") for chunk in chunks)
    # 传送带上的文件用完就删
    assert not upload_path(document_id).exists()


async def test_worker_marks_failed_when_embedding_is_unconfigured(
    client: AsyncClient, wire, make_account, login, session, factory
):
    """没有 embedding key → **失败并写原因**，绝不静默降级成"只有全文检索"。"""
    headers, _ = await _admin_headers(make_account, login)
    uploaded = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())
    document_id = uuid.UUID(uploaded.json()["id"])

    await handle_job(
        wire.jobs[0].dump(),
        embedder=None,
        settings=get_settings(),
        factory=factory,
    )

    document = await session.scalar(select(Document).where(Document.id == document_id))
    assert document is not None
    assert document.status is DocumentStatus.FAILED
    assert "EMBEDDING_API_KEY" in (document.error_message or "")


async def test_worker_fails_clearly_when_file_is_missing(
    client: AsyncClient, wire, make_account, login, session, factory
):
    """卷被清过 / 文件丢了 → 如实落库，不能让界面一直转圈。"""
    headers, _ = await _admin_headers(make_account, login)
    uploaded = await client.post(f"{PREFIX}/documents", headers=headers, files=_file())
    document_id = uuid.UUID(uploaded.json()["id"])
    upload_path(document_id).unlink()

    await handle_job(
        wire.jobs[0].dump(),
        embedder=FakeEmbedder(),
        settings=get_settings(),
        factory=factory,
    )

    document = await session.scalar(select(Document).where(Document.id == document_id))
    assert document is not None
    assert document.status is DocumentStatus.FAILED
    assert "重新上传" in (document.error_message or "")


async def test_worker_ignores_job_for_unknown_document(factory) -> None:
    """伪造 / 过期的消息：**当它不存在**，而不是顺手去动点什么。"""
    job = IngestJob(document_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    await handle_job(job.dump(), embedder=FakeEmbedder(), settings=get_settings(), factory=factory)
