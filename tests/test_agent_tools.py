"""agent 工具层（`app/rag/tools.py`）的测试。

分两半，**故意分开**：

* **① 纯单元**（不需要数据库）：参数校验、`ask_user` 只能一次、`finish` 的引用规则。
  这几条是**护栏**，跑得飞快，任何一次回归都该立刻红。
* **② 集成**（`@pytest.mark.integration`，需要真库）：`search_kb` 的 ref 编号与版本信息、
  `get_order` 的事实卡、以及 **`get_order` → `ref_time` → 旧版条款** 那条多跳链路。

⚠️ 这一层的测试**不调任何模型 API**（`AGENTS §7.1` 约定 9）：
   `search_kb` 在 `embedder=None` / `reranker=None` 时退化成纯全文检索，正好够验"证据链"，
   而"模型选得对不对"是评测层（`PLAN §13.4`）的事，不在单元测试里假装。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk, ContentStatus, Document, DocumentStatus
from app.db.models.order import Order, OrderStatus
from app.rag import tools
from app.rag.tools import ToolArgumentError, ToolRuntime, validate_arguments
from tests.test_chat_api import UNIT_VECTOR


def _runtime(*, question: str = "保修多久", ref_time: date | None = None) -> ToolRuntime:
    """不需要碰数据库的运行时（`session` 留空：这些用例只走校验与状态机）。"""
    return ToolRuntime(
        session=None,  # type: ignore[arg-type] —— 纯状态机用例不碰库
        tenant_id=uuid.uuid4(),
        question=question,
        ref_time=ref_time,
    )


# ------------------------------------------------------------
# ① 工具签名与参数校验
# ------------------------------------------------------------


def test_schema_names_match_the_whitelist() -> None:
    """白名单与给模型的签名**必须逐字一致**。

    ⚠️ 这两处一旦漂移，症状是"模型调了个不存在的东西"或者"工具写了但模型从不知道" ——
        都不会报错，只会表现成"agent 有点笨"。
    """
    names = [item["function"]["name"] for item in tools.TOOL_SCHEMAS]
    assert names == list(tools.TOOL_NAMES)
    assert set(tools.TERMINAL_TOOLS) <= set(names)


def test_every_schema_forbids_extra_properties() -> None:
    """每个工具都必须 `additionalProperties: false`（与 `validate_arguments` 的严格模式对齐）。"""
    for item in tools.TOOL_SCHEMAS:
        params = item["function"]["parameters"]
        assert params["additionalProperties"] is False
        assert set(params["required"]) <= set(params["properties"])


def test_unknown_tool_name_is_rejected() -> None:
    with pytest.raises(ToolArgumentError, match="没有这个工具"):
        validate_arguments("check_conflicts", {})


def test_unknown_parameter_is_rejected() -> None:
    """模型爱顺手塞 `explanation` 这种"贴心"字段 —— 一律拒。"""
    with pytest.raises(ToolArgumentError, match="不认识的参数"):
        validate_arguments(tools.SEARCH_KB, {"query": "保修", "explanation": "在查保修"})


def test_missing_required_parameter_is_rejected() -> None:
    with pytest.raises(ToolArgumentError, match="缺少必填参数"):
        validate_arguments(tools.SEARCH_KB, {})


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, 123])
def test_query_must_be_a_reasonable_string(bad: object) -> None:
    with pytest.raises(ToolArgumentError):
        validate_arguments(tools.SEARCH_KB, {"query": bad})


def test_order_no_is_uppercased_and_trimmed() -> None:
    args = validate_arguments(tools.GET_ORDER, {"order_no": "  sf10001 "})
    assert args["order_no"] == "SF10001"


@pytest.mark.parametrize("bad", ["SF10001'; drop table orders; --", "../etc/passwd", "SF 10001"])
def test_order_no_charset_is_whitelisted(bad: str) -> None:
    """单号这一列最终进 SQL 的 `==`，字符集收紧是零成本的。"""
    with pytest.raises(ToolArgumentError, match="order_no"):
        validate_arguments(tools.GET_ORDER, {"order_no": bad})


@pytest.mark.parametrize("bad", [0, 21, True, "5"])
def test_top_k_is_range_checked(bad: object) -> None:
    with pytest.raises(ToolArgumentError, match="top_k"):
        validate_arguments(tools.SEARCH_KB, {"query": "保修", "top_k": bad})


def test_top_k_accepts_a_small_number() -> None:
    assert validate_arguments(tools.SEARCH_KB, {"query": "保修", "top_k": 8})["top_k"] == 8


def test_citations_must_be_integers_and_are_deduped_in_order() -> None:
    args = validate_arguments(tools.FINISH, {"answer": "可以退", "citations": [3, 1, 3]})
    assert args["citations"] == [3, 1]


@pytest.mark.parametrize("bad", [["1"], [True], "1,2"])
def test_citations_reject_non_integers(bad: object) -> None:
    with pytest.raises(ToolArgumentError, match="citations"):
        validate_arguments(tools.FINISH, {"answer": "可以退", "citations": bad})


def test_recent_must_be_boolean() -> None:
    with pytest.raises(ToolArgumentError, match="recent"):
        validate_arguments(tools.GET_ORDER, {"recent": "yes"})


# ------------------------------------------------------------
# ② 状态机：反问只一次 / 交卷要有引用
# ------------------------------------------------------------


async def test_ask_user_is_allowed_only_once() -> None:
    """`PLAN §13.3`：**全程只允许问一次**。第二次要变成"可被模型读懂的错误"，不是异常。"""
    runtime = _runtime()

    first = await runtime.dispatch(tools.ASK_USER, {"question": "请问订单号是？"})
    assert first.ok is True
    assert first.terminal is True
    assert first.awaiting == "请问订单号是？"

    second = await runtime.dispatch(tools.ASK_USER, {"question": "那手机号呢？"})
    assert second.ok is False
    assert "不能问第二次" in second.payload["error"]
    assert second.terminal is False


async def test_finish_without_citations_is_rejected() -> None:
    """**不带引用不许收尾** —— 而且拒得有指导性（告诉它下一步该调什么）。"""
    result = await _runtime().dispatch(tools.FINISH, {"answer": "7 天无理由", "citations": []})
    assert result.ok is False
    assert result.terminal is False
    assert tools.SEARCH_KB in result.payload["hint"]


async def test_finish_with_unknown_ref_lists_available_refs() -> None:
    """引用了不存在的 ref → 退回重来，并把"现在有哪些 ref"告诉它。"""
    runtime = _runtime()
    runtime._evidence.append(_evidence(ref=1))

    result = await runtime.dispatch(tools.FINISH, {"answer": "7 天", "citations": [1, 9]})
    assert result.ok is False
    assert result.payload["available_refs"] == [1]


async def test_finish_with_valid_ref_succeeds_and_is_terminal() -> None:
    runtime = _runtime()
    runtime._evidence.append(_evidence(ref=2))

    result = await runtime.dispatch(tools.FINISH, {"answer": "7 天无理由", "citations": [2]})
    assert result.ok is True
    assert result.terminal is True
    assert result.finish_answer == "7 天无理由"
    assert result.finish_refs == (2,)


async def test_dispatch_never_raises_on_garbage() -> None:
    """**兜底是硬要求**：工具怎么坏都不能让整次问答变成 500。

    ⚠️ 但错误必须可读 —— `ok=false` ＋ 中文提示，模型才能自己纠正。
    """
    runtime = _runtime()
    for name, args in [
        (tools.SEARCH_KB, None),
        (tools.SEARCH_KB, ["query"]),
        (tools.SEARCH_KB, {"query": "保修", "top_k": {"n": 1}}),
        ("nope", {}),
    ]:
        result = await runtime.dispatch(name, args)
        assert result.ok is False
        assert result.payload["error"]


async def test_search_kb_is_not_terminal() -> None:
    """非终止工具不许把循环提前结束（否则模型一查完就被踢出来）。"""
    runtime = _runtime()
    result = await runtime.dispatch(tools.SEARCH_KB, {"query": "保修多久"})
    # 没有数据库 → 工具内部会失败，但**仍然不许是 terminal**
    assert result.terminal is False


def _evidence(*, ref: int) -> tools.Evidence:
    return tools.Evidence(
        ref=ref,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ordinal=0,
        filename="09-退换货政策.md",
        content_status=ContentStatus.ACTIVE.value,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        score=0.91,
        score_kind="rerank",
        snippet="家具主体保修 2 年",
        content="家具主体保修 2 年",
    )


async def test_finish_rejects_numbers_that_are_not_in_the_citations() -> None:
    """交卷自查：答案里的数字在引用里找不到 → **拦一次**。

    这就是 `PLAN §13.3` 的"交卷前自查"：不靠模型自觉，靠一次确定性的核对。
    """
    runtime = _runtime()
    runtime._evidence.append(_evidence(ref=1))

    first = await runtime.dispatch(tools.FINISH, {"answer": "保修 5 年[1]。", "citations": [1]})
    assert first.ok is False
    assert "数字" in first.payload["error"]
    assert "5" in str(first.payload["unsupported"])


async def test_self_check_blocks_only_once() -> None:
    """拦第二次就可能死循环 —— **只拦一次**，然后放行并记下审计结果。"""
    runtime = _runtime()
    runtime._evidence.append(_evidence(ref=1))

    await runtime.dispatch(tools.FINISH, {"answer": "保修 5 年[1]。", "citations": [1]})
    second = await runtime.dispatch(tools.FINISH, {"answer": "保修 5 年[1]。", "citations": [1]})

    assert second.ok is True
    assert runtime.last_audit is not None
    assert runtime.last_audit["unsupported"] >= 1


@pytest.mark.integration
async def test_ask_user_is_refused_right_after_an_order_was_found(
    session: AsyncSession, make_account
) -> None:
    """**已经查到订单了，就别再问顾客确认是哪一单。**

    ⚠️ 评测里真实发生过：agent 跑完 `get_order + search_kb` 之后去 `ask_user`
        “请确认是哪一单”，而 `ask_user` 是**终止工具** ——
        一道能答的题变成了一句反问。对内部坐席而言，他手里就有这张单。
    ⚠️ **只拦一次**：确实存在“查到了单，但顾客问的是另一件事”，彻底禁止会让它只能硬答。
    """
    account = await make_account(label="tools")
    await _seed_order(
        session,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        order_no="SF50001",
        placed_at=datetime.now(UTC) - timedelta(days=5),
        delivered_at=datetime.now(UTC) - timedelta(days=3),
    )
    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="能退吗")

    order = await runtime.dispatch(tools.GET_ORDER, {"order_no": "SF50001"})
    assert order.payload["orders"]

    blocked = await runtime.dispatch(tools.ASK_USER, {"question": "请问是哪一单？"})
    assert blocked.ok is False
    assert "SF50001" in str(blocked.payload.get("orders_at_hand"))
    assert blocked.terminal is False, "被拦不能当成反问成功"

    again = await runtime.dispatch(tools.ASK_USER, {"question": "请问是哪一单？"})
    assert again.ok is True, "只拦一次：真不行就让它问"


# ------------------------------------------------------------
# ③ 集成：真库
# ------------------------------------------------------------


async def _seed_doc(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    content: str,
    status: ContentStatus = ContentStatus.ACTIVE,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> uuid.UUID:
    document = Document(
        tenant_id=tenant_id,
        filename=filename,
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


async def _seed_order(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    order_no: str,
    placed_at: datetime,
    delivered_at: datetime | None = None,
    status: OrderStatus = OrderStatus.DELIVERED,
) -> None:
    session.add(
        Order(
            tenant_id=tenant_id,
            user_id=user_id,
            order_no=order_no,
            status=status,
            placed_at=placed_at,
            delivered_at=delivered_at,
            items=[{"name": "云朵单人沙发", "sku": "SF-DS-01", "qty": 1}],
            amount_cents=328000,
            currency="CNY",
        )
    )
    await session.commit()


def _db_runtime(
    session: AsyncSession, *, tenant_id: uuid.UUID, question: str, user_id: uuid.UUID | None = None
) -> ToolRuntime:
    """没有 embedder / reranker 的运行时 —— 检索退化成纯全文，**不花钱**。"""
    return ToolRuntime(
        session=session,
        tenant_id=tenant_id,
        question=question,
        user_id=user_id,
    )


@pytest.mark.integration
async def test_search_kb_returns_refs_with_version_info(
    session: AsyncSession, make_account
) -> None:
    """每条结果都要带回**出处 ＋ 内容状态 ＋ 生效区间** —— 政策版本解释全靠这三个字段。"""
    account = await make_account(label="tools")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="09-退换货政策-2026.md",
        content="家具主体保修 2 年，自签收之日起计算。",
        effective_from=date(2026, 1, 1),
    )

    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="保修多久")
    result = await runtime.dispatch(tools.SEARCH_KB, {"query": "家具保修多久"})

    assert result.ok is True
    assert result.terminal is False
    results = result.payload["results"]
    assert results, "全文检索至少该命中刚塞进去的那条"
    first = results[0]
    assert first["ref"] == 1
    assert first["source"] == "09-退换货政策-2026.md"
    assert first["content_status"] == ContentStatus.ACTIVE.value
    assert first["effective_from"] == "2026-01-01"
    assert first["effective_to"] is None
    assert first["score_kind"] == "fulltext_trgm"
    assert "保修" in first["content"]
    assert result.new_evidence and result.new_evidence[0].ref == 1


@pytest.mark.integration
async def test_ref_numbers_continue_across_searches(session: AsyncSession, make_account) -> None:
    """两次 `search_kb` 的 ref **不许都从 1 开始**。

    ⚠️ 否则 `finish` 里的 `[1]` 会指向"最后一次查到的第一条"，
        而模型脑子里记的是"第一次查到的第一条" —— 引用错位，且**不报错**。
    """
    account = await make_account(label="tools")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="a.md",
        content="家具主体保修 2 年，自签收之日起计算。",
    )

    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="保修多久")
    first = await runtime.dispatch(tools.SEARCH_KB, {"query": "家具保修多久"})
    second = await runtime.dispatch(tools.SEARCH_KB, {"query": "家具保修多久"})

    first_refs = [item["ref"] for item in first.payload["results"]]
    second_refs = [item["ref"] for item in second.payload["results"]]
    assert first_refs and second_refs
    assert min(second_refs) > max(first_refs)


@pytest.mark.integration
async def test_get_order_returns_fact_card_without_pii(session: AsyncSession, make_account) -> None:
    """订单事实卡：状态 / 时间 / 距今几天 / 商品 / 金额 —— **没有任何手机号字段**。"""
    account = await make_account(label="tools")
    now = datetime.now(UTC)
    # ToolRuntime.now 用的是 datetime.now().astimezone()（本地时区），
    # 所以两个时刻必须落在同一个日历上，否则跨日那几小时会差一天。
    # 做法：本地时区的「当天正午」往前推 10 天 —— 与运行时刻无关，也不会跨日。
    local_noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    delivered = local_noon - timedelta(days=10)
    await _seed_order(
        session,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        order_no="SF10001",
        placed_at=now - timedelta(days=40),
        delivered_at=delivered,
    )

    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="能退吗")
    result = await runtime.dispatch(tools.GET_ORDER, {"order_no": "sf10001"})

    assert result.ok is True
    orders = result.payload["orders"]
    assert len(orders) == 1
    assert orders[0]["order_no"] == "SF10001"
    assert orders[0]["days_since_delivery"] == 10
    assert orders[0]["amount"] == "¥3,280.00"
    assert "phone" not in str(orders[0]).lower()


@pytest.mark.integration
async def test_get_order_miss_returns_empty_list_not_a_sentence(
    session: AsyncSession, make_account
) -> None:
    """查不到就**给空数组**，不给"没查到订单"这种句子 —— 给了模型会顺着编。"""
    account = await make_account(label="tools")
    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="能退吗")
    result = await runtime.dispatch(tools.GET_ORDER, {"order_no": "SF99999"})

    assert result.ok is True
    assert result.payload["orders"] == []


@pytest.mark.integration
async def test_get_order_without_arguments_fails_with_guidance(
    session: AsyncSession, make_account
) -> None:
    account = await make_account(label="tools")
    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="我的单呢")
    result = await runtime.dispatch(tools.GET_ORDER, {})

    assert result.ok is False
    assert tools.ASK_USER in result.payload["hint"]


@pytest.mark.integration
async def test_order_only_answer_may_finish_without_policy_citations(
    session: AsyncSession, make_account
) -> None:
    """纯订单事实题（"退款到账没"）可以不引用条款 —— 但**必须真的查过订单**。"""
    account = await make_account(label="tools")
    await _seed_order(
        session,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        order_no="SF10002",
        placed_at=datetime.now(UTC) - timedelta(days=2),
        delivered_at=None,
        status=OrderStatus.REFUNDING,
    )

    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="退款到账没")
    order = await runtime.dispatch(tools.GET_ORDER, {"order_no": "SF10002"})
    assert order.payload["orders"]

    result = await runtime.dispatch(tools.FINISH, {"answer": "退款处理中", "citations": []})
    assert result.ok is True


@pytest.mark.integration
async def test_get_order_moves_ref_time_to_the_order_date(
    session: AsyncSession, make_account
) -> None:
    """**这条是 agent 相对 pipeline 的那点真优势**：先查订单 → 再按当年的条款检索。

    同一句"家具保修多久"：
      · 不知道订单时 → 参照时刻是今天 → 只拿得到 2026 版；
      · 查到一张 2024 年下的单 → 参照时刻变成 2024 → 拿的是 2023 版（当时有效的那版）。

    ⚠️ 这正是 pipeline 结构性做不到的：它的正则抽不到单号就直接退化，
        而 agent 可以**先查订单、再决定用哪一年的眼光看知识库**。
    """
    account = await make_account(label="tools")
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="09-退换货政策-2023.md",
        content="家具主体保修 1 年，自签收之日起计算（2023 版）。",
        status=ContentStatus.SUPERSEDED,
        effective_from=date(2023, 1, 1),
        effective_to=date(2025, 12, 31),
    )
    await _seed_doc(
        session,
        tenant_id=account.tenant_id,
        filename="09-退换货政策-2026.md",
        content="家具主体保修 2 年，自签收之日起计算（2026 版）。",
        effective_from=date(2026, 1, 1),
    )
    await _seed_order(
        session,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        order_no="SF20001",
        placed_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        delivered_at=datetime(2024, 5, 6, 12, 0, tzinfo=UTC),
    )

    runtime = _db_runtime(session, tenant_id=account.tenant_id, question="这把椅子保修多久")

    before = await runtime.dispatch(tools.SEARCH_KB, {"query": "家具保修多久"})
    before_sources = {item["source"] for item in before.payload["results"]}
    assert "09-退换货政策-2026.md" in before_sources
    assert "09-退换货政策-2023.md" not in before_sources

    order = await runtime.dispatch(tools.GET_ORDER, {"order_no": "SF20001"})
    assert order.payload["orders"][0]["order_no"] == "SF20001"

    after = await runtime.dispatch(tools.SEARCH_KB, {"query": "家具保修多久"})
    assert after.payload["effective_on"] == "2024-05-01"
    after_sources = {item["source"] for item in after.payload["results"]}
    assert "09-退换货政策-2023.md" in after_sources
    assert "09-退换货政策-2026.md" not in after_sources
