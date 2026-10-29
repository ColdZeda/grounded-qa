"""embedding 元数据锁 —— 纯单元 ＋ 集成（真库）。

⚠️ 这条锁治的是**最贵的一类故障**：换过 embedding 模型之后，
    新旧向量不在同一个空间里，余弦相似度**不是相似度、是噪音** ——
    而系统**不报错、不崩溃、接口 200**，只是答案慢慢变差。

三段防线各测一段：

    读侧：`HybridRetriever._vector` 只查同模型的块  → 集成测试
    自检：`warn_if_embedding_lock_mismatch` 会喊    → 集成测试（打日志 + 指标）
    记账：`inspect` 数得对不对                      → 集成测试
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.chat import get_clients
from app.core.config import get_settings
from app.db import checks
from app.db.models.user import UserRole
from app.rag.embedding_lock import EmbeddingLock, inspect
from tests.test_chat_api import UNIT_VECTOR, FakeChat, FakeEmbedder, _seed_chunk

PREFIX = get_settings().api_prefix
EXPECTED = "fake-embedding"  # 与 `FakeEmbedder.model` 一致


# 纯单元：数得对、话能照着修
def test_lock_is_ok_when_everything_matches() -> None:
    lock = EmbeddingLock(
        expected_model="m1",
        expected_dim=1024,
        fingerprint=(("m1", 1024, 231),),
    )

    assert lock.ok is True
    assert lock.matching == 231
    assert lock.mismatched == 0
    assert lock.message() == ""


def test_lock_counts_mismatched_and_unknown_separately() -> None:
    """⚠️ 两种"用不到"要**分开报**：修法不一样。

    · `mismatched`：知道是哪个旧模型 → 换回去，或者全量重嵌入
    · `unknown`：连模型都没记 → 先查清楚它是谁嵌的，否则不知道该怎么修
    """
    lock = EmbeddingLock(
        expected_model="m2",
        expected_dim=1024,
        fingerprint=(("m2", 1024, 10), ("m1", 1024, 5), (None, 1024, 3)),
    )

    assert lock.total == 18
    assert lock.matching == 10
    assert lock.mismatched == 5
    assert lock.unknown == 3
    assert lock.ok is False


def test_lock_message_names_the_model_and_the_fix() -> None:
    lock = EmbeddingLock(
        expected_model="new-model",
        expected_dim=1024,
        fingerprint=(("new-model", 1024, 1), ("old-model", 768, 2)),
    )

    message = lock.message()

    # 要说清"谁跟谁对不上"
    assert "old-model" in message
    assert "768" in message
    assert "new-model" in message
    # 还要说清"怎么修"，否则用户只知道坏了
    assert "--replace" in message
    assert "不会被向量检索用到" in message


def test_lock_tolerates_empty_knowledge_base() -> None:
    lock = EmbeddingLock(expected_model="m", expected_dim=1024, fingerprint=())

    assert lock.ok is True
    assert lock.total == 0
    assert lock.models() == ()


# 集成：三段的接线
@pytest.mark.integration
async def test_inspect_reads_the_actual_metadata(session: AsyncSession, make_account) -> None:
    account = await make_account()
    await _seed_chunk(session, tenant_id=account.tenant_id, content="A", embedding=UNIT_VECTOR)
    await _seed_chunk(
        session, tenant_id=account.tenant_id, content="B", embedding=UNIT_VECTOR, model="旧模型"
    )
    # 只有文本、没有向量（`--no-embed` 的情况）—— 不该被算进"不一致"
    await _seed_chunk(session, tenant_id=account.tenant_id, content="C")

    lock = await inspect(
        session, tenant_id=account.tenant_id, expected_model=EXPECTED, expected_dim=1024
    )

    assert lock.total == 2  # 没有向量的那条不算
    assert lock.matching == 1
    assert lock.mismatched == 1
    assert lock.unknown == 0
    assert "旧模型" in lock.message()


@pytest.mark.integration
async def test_inspect_ignores_other_tenants(session: AsyncSession, make_account) -> None:
    """元数据锁是**按租户**看的：别人库里的旧模型不该让你这边报警。"""
    mine = await make_account()
    other = await make_account(label="other")
    await _seed_chunk(session, tenant_id=mine.tenant_id, content="A", embedding=UNIT_VECTOR)
    await _seed_chunk(
        session, tenant_id=other.tenant_id, content="B", embedding=UNIT_VECTOR, model="旧模型"
    )

    lock = await inspect(
        session, tenant_id=mine.tenant_id, expected_model=EXPECTED, expected_dim=1024
    )

    assert lock.ok is True
    assert lock.mismatched == 0


@pytest.mark.integration
async def test_vector_search_skips_chunks_from_another_model(
    client: AsyncClient, app, make_account, login, session: AsyncSession
) -> None:
    """**核心行为**：另一个模型嵌出来的向量**不参与向量检索**。

    ⚠️ 两条块的向量**一模一样**（都是 UNIT_VECTOR）——
        如果读侧不过滤，两条的余弦相似度都会是 1.0，回答里两条都会出现。
        过滤之后只剩当前模型那条。
    """
    app.dependency_overrides[get_clients] = lambda: (
        FakeChat(),
        FakeEmbedder(UNIT_VECTOR),
        None,
    )
    account = await make_account(role=UserRole.ADMIN)
    token = await login(account.email, account.password)
    headers = account.auth(token)

    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="无理由退货 7 天（当前模型）",
        embedding=UNIT_VECTOR,
    )
    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="这句来自旧模型，不该被向量路选中",
        embedding=UNIT_VECTOR,
        model="old-model",
    )

    conversation = await client.post(
        f"{PREFIX}/conversations", json={"title": "元数据锁"}, headers=headers
    )
    conversation_id = conversation.json()["id"]
    resp = await client.post(
        f"{PREFIX}/conversations/{conversation_id}/messages",
        json={"question": "退货几天？"},
        headers=headers,
    )

    assert resp.status_code == 200
    # 引用来源里**不该**出现旧模型那条
    assert "旧模型" not in resp.text
    assert "当前模型" in resp.text


@pytest.mark.integration
async def test_startup_check_warns_when_metadata_drifts(
    session: AsyncSession, make_account, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """自检发现不一致时**要打显眼的告警**，并且不动启动（只告警）。

    ⚠️ 两处替身都是必须的：
        · `checks.get_session_factory` 换成测试引擎 —— 否则它去连全局引擎；
        · `tenant_ids=` 限定到**自己造的租户** —— 不传的话它会扫全库，
          而开发库里本来就有真实数据（模型名与 `FakeEmbedder` 不同），
          测试就会依赖全局状态、并且变得不可控。
    """
    account = await make_account()
    await _seed_chunk(
        session, tenant_id=account.tenant_id, content="B", embedding=UNIT_VECTOR, model="old-model"
    )

    factory = async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    monkeypatch.setattr(checks, "get_session_factory", lambda: factory)

    with caplog.at_level(logging.WARNING, logger="app.db.checks"):
        await checks.warn_if_embedding_lock_mismatch(tenant_ids=[account.tenant_id])

    assert any("元数据锁" in record.message for record in caplog.records)
    # 指标也要跟着动（监控上能看见漂移了多少块）
    assert REGISTRY.get_sample_value("gq_embedding_metadata_drift_chunks") is not None


@pytest.mark.integration
async def test_startup_check_is_quiet_when_everything_matches(
    session: AsyncSession, make_account, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    account = await make_account()
    # ⚠️ 这里必须用**配置里的真实模型名**，不是 `FakeEmbedder` 的假名字 ——
    #    自检比的是"库里的元数据 vs `settings.embedding_model`"。
    await _seed_chunk(
        session,
        tenant_id=account.tenant_id,
        content="A",
        embedding=UNIT_VECTOR,
        model=get_settings().embedding_model,
    )

    factory = async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    monkeypatch.setattr(checks, "get_session_factory", lambda: factory)

    with caplog.at_level(logging.WARNING, logger="app.db.checks"):
        await checks.warn_if_embedding_lock_mismatch(tenant_ids=[account.tenant_id])

    assert not any("元数据锁" in record.message for record in caplog.records)
    assert REGISTRY.get_sample_value("gq_embedding_metadata_drift_chunks") == 0
