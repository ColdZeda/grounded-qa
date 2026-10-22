"""参照时刻（v1.3 阶段 B2）—— **同一句话，不同时间基准要引不同版本**。

分三层测，因为这是三处**互相独立**的失败点：

    ① 时间从哪来     `reftime.parse_explicit_date` / `resolve_ref_time`  ← 纯单元
    ② 怎么进 SQL     `HybridRetriever` 的资格过滤（`WHERE` 里、`LIMIT` 之前）← 集成
    ③ 提示词怎么解释 `prompt.build_messages` 的【时间基准】块          ← 纯单元

⚠️ 为什么要专门测 ①：问句里"2023版保修条款还适用吗"含**四个数字**，
    正则一不小心就把它读成一个日期，于是整个检索的时间基准被一句话拐跑。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk, ContentStatus, Document, DocumentStatus
from app.rag.pipeline import RagPipeline
from app.rag.prompt import build_messages
from app.rag.reftime import RefTime, RefTimeSource, parse_explicit_date, resolve_ref_time
from app.rag.retrieval import HybridRetriever
from tests.test_chat_api import UNIT_VECTOR, FakeChat

# ------------------------------------------------------------
# ① 时间从哪来
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我 2024 年 3 月 15 日买的沙发，保修多久", date(2024, 3, 15)),
        ("2024年3月15号买的", date(2024, 3, 15)),
        ("2024年3月买的", date(2024, 3, 1)),
        ("2024年买的", date(2024, 1, 1)),
        ("2024-03-15 下的单", date(2024, 3, 15)),
        ("2024/3/15 下的单", date(2024, 3, 15)),
        ("2024-03 买的", date(2024, 3, 1)),
    ],
)
def test_reads_explicit_dates(text: str, expected: date) -> None:
    assert parse_explicit_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # ⚠️ 关键用例：这是版本号，不是日期。读成日期会让时间基准整体跑偏。
        "2023版保修条款还适用吗",
        "2023版的退货政策是什么",
        # 没有分隔符的裸数字串：单号 / 流水号，不是日期
        "订单 20240315 到哪了",
        # 非法日期
        "2024-02-30 买的",
        # 什么都没有
        "保修多久",
        "",
    ],
)
def test_does_not_invent_dates(text: str) -> None:
    assert parse_explicit_date(text) is None


def test_invalid_date_falls_through_to_the_other_form() -> None:
    """`2024-02-30` 非法 → 不该整体放弃，后面那个合法日期仍然要读出来。"""
    assert parse_explicit_date("2024-02-30 的记录，实际是 2024年5月8日 买的") == date(2024, 5, 8)


def test_order_time_beats_the_question() -> None:
    """订单是库里的**事实**，优先级高于用户自述。"""
    resolved = resolve_ref_time(
        question="我 2020 年买的，保修多久",
        order_placed_at=[datetime(2024, 3, 15, tzinfo=UTC)],
    )

    assert resolved == RefTime(date(2024, 3, 15), RefTimeSource.ORDER)


def test_question_time_is_used_when_there_is_no_order() -> None:
    resolved = resolve_ref_time(question="2024年3月买的，保修多久")

    assert resolved.value == date(2024, 3, 1)
    assert resolved.source is RefTimeSource.QUESTION
    assert resolved.informed is True


def test_multiple_orders_take_the_earliest() -> None:
    """只能挑一个时点，**取最早那单**：宁可落在更老的政策边界上（见模块 docstring）。"""
    resolved = resolve_ref_time(
        order_placed_at=[
            datetime(2026, 1, 20, tzinfo=UTC),
            datetime(2023, 6, 1, tzinfo=UTC),
            datetime(2024, 9, 9, tzinfo=UTC),
        ]
    )

    assert resolved.value == date(2023, 6, 1)


def test_no_signal_falls_back_to_today_and_is_not_informed() -> None:
    """**没信号 = 今天**，等于阶段 B 的行为 —— 不猜、也不去反问用户要单号。"""
    resolved = resolve_ref_time(question="保修多久", today=date(2026, 10, 22))

    assert resolved == RefTime(date(2026, 10, 22), RefTimeSource.TODAY)
    assert resolved.informed is False


def test_pipeline_wiring_prefers_order_then_frozen() -> None:
    """接线：订单时间进 pipeline，而**冻结值优先于一切**（评测要能复现）。"""

    def make(**kwargs: object) -> RagPipeline:
        return RagPipeline(
            session=None,  # type: ignore[arg-type]  # 只调纯函数，不碰库
            tenant_id=uuid.uuid4(),
            chat=FakeChat(),  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )

    with_order = make(order_placed_at=[datetime(2024, 3, 15, tzinfo=UTC)])
    assert with_order._resolve_ref_time("我 2020 年买的").value == date(2024, 3, 15)

    frozen = make(
        order_placed_at=[datetime(2024, 3, 15, tzinfo=UTC)],
        ref_time=date(2025, 1, 1),
    )
    assert frozen._resolve_ref_time("我 2020 年买的") == RefTime(
        date(2025, 1, 1), RefTimeSource.FROZEN
    )


# ------------------------------------------------------------
# ③ 提示词怎么解释这个时间
# ------------------------------------------------------------


def test_prompt_explains_why_an_old_policy_still_applies() -> None:
    """⚠️ 这一段是 B2 的**关键一句**，不是装饰。

    按 2024 年作答时，检索到的正是 2023 版条款，而它正文里写着"已于 2025-12-31 废止"。
    不解释，模型会拿这句话去**劝用户忽略**唯一正确的那份资料。
    """
    system = build_messages(
        question="保修多久",
        hits=[],
        ref_time=RefTime(date(2024, 3, 15), RefTimeSource.ORDER),
    )[0]["content"]

    assert "【时间基准】" in system
    assert "2024-03-15" in system
    assert "下单时间" in system
    assert "当时有效" in system


def test_prompt_omits_the_block_when_there_is_no_signal() -> None:
    """没有信号时**不许**出现这一段：凭空说一句"按今天"会让模型以为用户在问时间。"""
    system = build_messages(question="保修多久", hits=[], ref_time=None)[0]["content"]

    assert "【时间基准】" not in system


# ------------------------------------------------------------
# ② 怎么进 SQL（真库）
# ------------------------------------------------------------


async def _seed(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    content: str,
    status: ContentStatus,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> uuid.UUID:
    """塞一份文档 + 一条带向量的块，返回 `document_id`。"""
    document = Document(
        tenant_id=tenant_id,
        filename=f"{status.value}-{uuid.uuid4().hex[:8]}.md",
        content_type="text/markdown",
        content_hash=uuid.uuid4().hex * 2,
        status=DocumentStatus.READY,
        content_status=status,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    session.add(document)
    await session.flush()
    session.add(
        Chunk(
            tenant_id=tenant_id,
            document_id=document.id,
            ordinal=0,
            content=content,
            embedding=list(UNIT_VECTOR),
            embedding_model="fake-embedding",
            embedding_dim=len(UNIT_VECTOR),
        )
    )
    await session.commit()
    return document.id


async def _hit_docs(
    session: AsyncSession, tenant_id: uuid.UUID, *, ref_time: date | None, scope: str = "current"
) -> set[uuid.UUID]:
    retriever = HybridRetriever(session, tenant_id=tenant_id, scope=scope, ref_time=ref_time)
    hits, _ = await retriever.search(
        query="家具主体保修多久",
        query_embedding=UNIT_VECTOR,
        top_k=5,
        rrf_k=60,
        fulltext_min_similarity=0.05,
    )
    return {hit.document_id for hit in hits}


@pytest.mark.integration
async def test_same_question_picks_different_versions_by_ref_time(
    session: AsyncSession, make_account
) -> None:
    """**B2 的核心行为**：同一个问题，时间基准不同 → 引用不同版本。

    ⚠️ 两份文档的**相似度完全一样**（同样的向量、几乎同样的话），
        所以这里测的必然是资格过滤，不是打分 —— 这正是"相关度 ≠ 有效性"。
    """
    account = await make_account()
    legacy = await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 1 年（2023 版，已于 2025-12-31 废止）",
        status=ContentStatus.SUPERSEDED,
        effective_from=date(2023, 1, 1),
        effective_to=date(2025, 12, 31),
    )
    current = await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 2 年（2026 版）",
        status=ContentStatus.ACTIVE,
        effective_from=date(2026, 1, 1),
    )

    # 2024 年买的沙发 → 当年生效的是 2023 版
    assert await _hit_docs(session, account.tenant_id, ref_time=date(2024, 3, 15)) == {legacy}
    # 今天问 → 现行版
    assert await _hit_docs(session, account.tenant_id, ref_time=date(2026, 10, 22)) == {current}


@pytest.mark.integration
async def test_interval_boundaries_are_inclusive(session: AsyncSession, make_account) -> None:
    """生效区间是**闭区间**：`effective_from` 当天与 `effective_to` 当天都算有效。"""
    account = await make_account()
    doc = await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 1 年",
        status=ContentStatus.SUPERSEDED,
        effective_from=date(2023, 1, 1),
        effective_to=date(2025, 12, 31),
    )

    for day in (date(2023, 1, 1), date(2025, 12, 31)):
        assert await _hit_docs(session, account.tenant_id, ref_time=day) == {doc}

    for day in (date(2022, 12, 31), date(2026, 1, 1)):
        assert await _hit_docs(session, account.tenant_id, ref_time=day) == set()


@pytest.mark.integration
@pytest.mark.parametrize(
    "status", [ContentStatus.DRAFT, ContentStatus.INTERNAL, ContentStatus.SIBLING_BRAND]
)
async def test_other_statuses_never_surface_whatever_the_date(
    session: AsyncSession, make_account, status: ContentStatus
) -> None:
    """草案 / 内部 / 兄弟品牌 **永远**排除 —— 它们不是"时间没到"，是根本不该给客户看。

    ⚠️ 区间留空（= 一直有效）也一样排除。这条一旦放松，
        内部话术库会立刻出现在客户答案里（"你是谁"那次 badcase 就是它）。
    """
    account = await make_account()
    await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 1 年",
        status=status,
    )

    for day in (date(2024, 3, 15), date(2026, 10, 22)):
        assert await _hit_docs(session, account.tenant_id, ref_time=day) == set()


@pytest.mark.integration
async def test_scope_all_still_sees_everything(session: AsyncSession, make_account) -> None:
    """`--scope all` 是**复现历史数字**的另一支尺子：它连时间也不看。"""
    account = await make_account()
    legacy = await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 1 年",
        status=ContentStatus.SUPERSEDED,
        effective_from=date(2023, 1, 1),
        effective_to=date(2025, 12, 31),
    )
    current = await _seed(
        session,
        tenant_id=account.tenant_id,
        content="家具主体保修 2 年",
        status=ContentStatus.ACTIVE,
        effective_from=date(2026, 1, 1),
    )

    hits = await _hit_docs(session, account.tenant_id, ref_time=date(2026, 10, 22), scope="all")

    assert hits == {legacy, current}
