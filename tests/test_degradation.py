"""**检索降级必须留痕**（2026-10-26 被真实事故逼出来的一条）。

## 事故本身

阿里云百炼的**预算耗尽**（`BudgetLimitExceeded`）→ embedding 与 rerank **同时**返回 4xx。
链路按设计"照样答"（可用性 > 功能齐全），但：

- 向量路没了 ⟹ 召回退化为纯全文（hit@5 从 0.98 掉到 0.57）；
- 精排没了 ⟹ 置信度从 rerank 分退回余弦；两样都没了则 `confidence is None`
  ⟹ `should_refuse` 直接 `return False`，**拒答闸门失效**。

最糟的不是变差，是**一点声音都没有**：`pipeline` 那条路当时连日志都不打。
于是"向量路死了"和"知识库里确实没有"在外部看起来一模一样 ——
这正是本项目反复踩到的那一类坑（全文检索 ctype / 向量漂移 / 限额放行…）。

## 所以这条测试钉两件事

1. 降级时**必须**有日志（`caplog`）；
2. 降级时**必须**有计数器（`gq_retrieval_degraded_total{kind}`），否则看板上看不见比例。

⚠️ 顺带守住"降级不抛异常"：模型侧的抖动不该把问答打成 500。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.core.config import get_settings
from app.core.metrics import retrieval_degraded
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.rag.providers import ModelAPIError
from tests.test_chat_api import UNIT_VECTOR, FakeChat, parse_sse

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix

#: 和问题**共享长中文串** —— embedding 挂了以后只剩全文路（`word_similarity`），
#: 靠这一句保证"确实能召回"，否则测的就变成"召回为空"了。
CONTENT = "七天无理由退货：签收之日起 7 个自然日内，未使用且不影响二次销售可申请。"
QUESTION = "七天无理由退货的期限是多久？"


class BrokenEmbedder:
    """**一调就炸**的 embedding 客户端（模拟配额耗尽 / 服务不可用）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-embedding"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise ModelAPIError("embedding API 返回 400：BudgetLimitExceeded")


class GoodEmbedder:
    """正常返回一个"和入库向量完全相同"的向量 —— 余弦 1.0。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-embedding"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [list(UNIT_VECTOR) for _ in texts]


class BrokenReranker:
    """**一调就炸**的精排客户端。"""

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int = 5) -> Any:
        raise ModelAPIError("rerank API 返回 429：BudgetLimitExceeded")


class GoodReranker:
    """给一个必然过阈值的高分。"""

    def __init__(self, score: float = 0.95) -> None:
        self._score = score

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int = 5) -> Any:
        return [(index, self._score) for index in range(min(top_n, len(documents)))]


def _count(kind: str) -> float:
    """读计数器当前的绝对值（Prometheus 的 Counter 只增不减，比前后差值即可）。"""
    return float(retrieval_degraded.labels(kind=kind)._value.get())


async def _seed(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    document = Document(
        tenant_id=tenant_id,
        filename="退货.md",
        content_type="text/markdown",
        content_hash=uuid.uuid4().hex * 2,
        status=DocumentStatus.READY,
    )
    session.add(document)
    await session.flush()
    session.add(
        Chunk(
            tenant_id=tenant_id,
            document_id=document.id,
            ordinal=0,
            content=CONTENT,
            embedding=list(UNIT_VECTOR),
            embedding_model="fake-embedding",
            embedding_dim=len(UNIT_VECTOR),
        )
    )
    await session.commit()


async def _ask(client: AsyncClient, token: str) -> dict[str, Any]:
    resp = await client.post(
        f"{PREFIX}/conversations",
        json={"title": "降级"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    conversation_id = resp.json()["id"]
    async with client.stream(
        "POST",
        f"{PREFIX}/conversations/{conversation_id}/messages",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        body = "".join([piece async for piece in response.aiter_text()])
    return next(data for event, data in parse_sse(body) if event == "done")


async def test_embedding_failure_logs_and_counts_and_still_answers(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession, caplog
) -> None:
    """向量路挂掉：**记日志 ＋ 记指标 ＋ 照样答**（退化为纯全文）。"""
    account = await make_account()
    await _seed(session, tenant_id=account.tenant_id)
    token = await login(account.email, account.password)

    embedder = BrokenEmbedder()
    app.dependency_overrides[get_clients] = lambda: (
        FakeChat(["七天无理由退货是 7 天[1]。"]),
        embedder,
        GoodReranker(),
    )

    before = _count("embedding")
    with caplog.at_level(logging.WARNING):
        done = await _ask(client, token)

    assert embedder.calls >= 1
    assert _count("embedding") == before + 1
    assert any("检索降级" in record.message for record in caplog.records)
    # 降级不等于不能用：有全文本命中就该照常作答（可用性 > 功能齐全）
    assert done["refused"] is False


async def test_rerank_failure_logs_and_counts(
    client: AsyncClient, app: Any, make_account, login, session: AsyncSession, caplog
) -> None:
    """精排挂掉：**记日志 ＋ 记指标**，并退回"余弦当置信度"。

    ⚠️ 这一步不是小事：置信度的**量纲换了**，拒答阈值也跟着换成另一把尺子
        （`should_refuse` 按 `score_kind` 选阈值），所以必须能被看见。
    """
    account = await make_account()
    await _seed(session, tenant_id=account.tenant_id)
    token = await login(account.email, account.password)

    app.dependency_overrides[get_clients] = lambda: (
        FakeChat(["七天无理由退货是 7 天[1]。"]),
        GoodEmbedder(),
        BrokenReranker(),
    )

    before = _count("rerank")
    with caplog.at_level(logging.WARNING):
        done = await _ask(client, token)

    assert _count("rerank") == before + 1
    assert any("检索降级" in record.message for record in caplog.records)
    # 余弦 1.0 远高于 `answer_min_cosine` ⟹ 仍然作答（不是"降级就拒答"）
    assert done["refused"] is False
