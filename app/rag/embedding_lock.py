"""embedding 元数据锁：**两套向量空间不能被悄悄混用**。

## 要防的是什么

换一个 embedding 模型之后，旧向量和新向量**不在同一个空间里** ——
它们之间的余弦相似度**不是"相似度"，是噪音**。而这个故障的特点是：

    · 不报错（SQL 照样执行，接口照样 200）
    · 不崩溃（检索照样返回 k 条）
    · 只是**答案慢慢变差**，而且 badcase 归因不出来

这正是项目最怕的那类病（和 `pg_trgm` 在 `ctype=C` 下对中文恒为 0 同一种病）。
所以 `chunks` 上早就有两半元数据（`embedding_model` / `embedding_dim`），
本模块负责**读它们、比对它们、把不一致喊出来**。

## 三个防线，各管一段

| 防线 | 在哪 | 不一致时 |
|---|---|---|
| **写**：向量与元数据一起落库 | `app/kb/ingest.py` | —— （早就有了） |
| **读**：向量检索**只查当前模型**的块 | `app/rag/retrieval.py` | 旧向量自动被忽略 ⟹ **不会污染排序** |
| **启动**：扫一遍库，对不上就喊 | `app/db/checks.py` | 打显眼的告警 ＋ 记 Prometheus 指标 |

## ⚠️ 为什么**不在入库时直接拒绝**

`knowledge.py` 的模块注释原本写的是"入库时不一致就拒绝"。真做下去会发现
**它把自己的恢复路径堵死了**：

    换模型的正确做法是"逐文件 `ingest.py --replace` 重嵌入"。
    第一个文件重嵌入时，库里**仍然躺着**其余文件的旧向量 ——
    按"入库时拒绝"的逻辑，这一步会被拒，于是**永远换不了模型**。

所以改成"**读侧硬过滤 ＋ 启动响亮告警 ＋ 写入如实记账**"：
旧向量不会被用到（安全），但也不会拦住你重嵌入（可恢复）。

## 有向量却没记模型的老数据

理论上不该出现（两条总是同时写）。真出现了说明是历史遗留 —— 它属于**来源不明**，
既不能证明和当前模型一致，也不该被当成一致。于是：**读侧一样排除它**，
启动自检单独统计成 `unknown`（与 `mismatched` 分开报，因为修法不一样）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk


@dataclass(frozen=True)
class EmbeddingLock:
    """库里向量的"指纹" 与 当前配置的比对结果。

    `fingerprint` 是 `((model, dim, count), ...)` —— **可能有多组**（就是混用了）。
    """

    expected_model: str
    expected_dim: int
    fingerprint: tuple[tuple[str | None, int | None, int], ...]

    @property
    def total(self) -> int:
        """有向量的块总数。"""
        return sum(count for _, _, count in self.fingerprint)

    @property
    def matching(self) -> int:
        """与当前配置一致的块数（**只有这些会被向量检索用到**）。"""
        return sum(
            count
            for model, dim, count in self.fingerprint
            if model == self.expected_model and dim == self.expected_dim
        )

    @property
    def mismatched(self) -> int:
        """记了模型，但**不是当前模型**的块数。"""
        return sum(
            count
            for model, dim, count in self.fingerprint
            if model is not None and (model != self.expected_model or dim != self.expected_dim)
        )

    @property
    def unknown(self) -> int:
        """**有向量但没记模型** —— 来源不明，读侧同样排除。"""
        return sum(count for model, _, count in self.fingerprint if model is None)

    @property
    def ok(self) -> bool:
        """全部对得上（包含"库是空的"这种平凡情况）。"""
        return self.mismatched == 0 and self.unknown == 0

    def models(self) -> tuple[str, ...]:
        """库里出现过的模型名（去重、保序）。"""
        seen: list[str] = []
        for model, _, _ in self.fingerprint:
            name = model or "（未记录）"
            if name not in seen:
                seen.append(name)
        return tuple(seen)

    def message(self) -> str:
        """一句**能照着修**的话。不是"出错了敬请排查"。"""
        if self.ok:
            return ""
        parts: list[str] = []
        if self.mismatched:
            parts.append(
                f"{self.mismatched} 个块是用 {self._describe_others()} 生成的，"
                f"而当前配置是 `{self.expected_model}`（{self.expected_dim} 维）"
            )
        if self.unknown:
            parts.append(f"{self.unknown} 个块有向量但**没记模型**（来源不明）")
        body = "；".join(parts)
        return (
            f"知识库里 {body}。这些向量**不会被向量检索用到**"
            f"（读侧按模型过滤，避免拿两套不可比的向量算相似度）。"
            f"修法：① 换回原来的模型；或 ② 用当前模型全量重嵌入"
            f"（`scripts/kb/ingest.py --replace` 把每个文件重灌一遍）。"
        )

    def _describe_others(self) -> str:
        names = [
            f"`{model}`（{dim} 维）"
            for model, dim, _ in self.fingerprint
            if model is not None and (model != self.expected_model or dim != self.expected_dim)
        ]
        # 去重保序
        unique: list[str] = []
        for name in names:
            if name not in unique:
                unique.append(name)
        return " / ".join(unique) if unique else "其他模型"


async def inspect(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    expected_model: str,
    expected_dim: int,
) -> EmbeddingLock:
    """扫一遍本租户**有向量**的块，按 `(model, dim)` 聚合。

    ⚠️ 只统计 `embedding IS NOT NULL` 的块：`--no-embed` 落进来的文本块
        本来就没有向量，它们既不参与向量检索，也不算"不一致"。
    """
    stmt = (
        select(Chunk.embedding_model, Chunk.embedding_dim, func.count())
        .where(Chunk.tenant_id == tenant_id, Chunk.embedding.is_not(None))
        .group_by(Chunk.embedding_model, Chunk.embedding_dim)
    )
    rows = (await session.execute(stmt)).all()
    return EmbeddingLock(
        expected_model=expected_model,
        expected_dim=expected_dim,
        fingerprint=tuple((model, dim, int(count)) for model, dim, count in rows),
    )
