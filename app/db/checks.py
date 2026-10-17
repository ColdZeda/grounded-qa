"""启动自检：**把"静默降级"变成"看得见的告警"。**

⚠️ 这个模块存在的唯一理由，是一类特别讨厌的故障：**功能坏了，但什么都不报。**

背景（2026-10-14 实测）：`pg_trgm` 按数据库的 **ctype** 判断"什么字符算词"。
    · ctype = `C`      → 中文不算字母 → `show_trgm('退款政策')` 返回**空集**
    · ctype = `C.UTF-8` → 正常切出 5 个三元组

    后果是 `word_similarity()` **恒为 0**，全文检索那一路永远召回不出任何东西。
    **不抛异常、不写日志、接口 200** —— 只是"搜索结果变少了"，看起来像"知识库里确实没有"。

    这类故障靠人眼发现不了，所以必须在启动时**主动测一下**并喊出来。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from prometheus_client import Gauge
from sqlalchemy import select, text

from app.core.config import get_settings
from app.db.models.knowledge import Chunk
from app.db.session import get_session_factory
from app.rag.embedding_lock import inspect

logger = logging.getLogger(__name__)

#: 库里"向量元数据与本配置不一致"的块数。**上线后应当恒为 0** ——
#: 不为 0 时读侧会自动忽略那些块（见 `app/rag/embedding_lock.py`），
#: 所以问答不会坏，但**一部分知识事实上检索不到了**，必须有人去重嵌入。
_embedding_drift = Gauge(
    "gq_embedding_metadata_drift_chunks",
    "向量元数据与当前 embedding 配置不一致（或未记录）的块数",
)

#: 探针文本：两个中文字足够切出三元组（4 个字符以上才有中间三元组；
#: `show_trgm` 对 2 个字符会给边界三元组，够用了）。
_PROBE_TEXT = "测试"


async def warn_if_fulltext_degraded() -> None:
    """启动时检查全文检索是否可用；不可用就打**显眼的告警**。

    ⚠️ **只告警，不阻止启动。** 理由：
        全文路挂掉时，向量路仍然工作，问答**仍然可用**（只是召回差一截）。
        为了一个"降级"而拒绝启动，等于把可用性问题升级成可用性事故。
        但如果连数据库都连不上，那是另一回事 —— 那时静默返回即可，
        让真正的健康检查（compose healthcheck / `/healthz`）去处理。
    """
    try:
        factory = get_session_factory()
        async with factory() as session:
            trigrams = await session.scalar(
                text("select show_trgm(:probe)"), {"probe": _PROBE_TEXT}
            )
    except Exception:
        logger.debug("跳过全文检索自检（数据库不可达或 pg_trgm 未安装）")
        return

    if trigrams:
        return

    logger.warning(
        "\n"
        "=" * 72 + "\n"
        "⚠️  全文检索对中文**不可用**（pg_trgm 切不出三元组）\n"
        "=" * 72 + "\n"
        "现象：word_similarity() 恒为 0 → 混合检索的「全文路」一条都召回不出来，\n"
        "      而接口**不会报错**，看起来就像'知识库里确实没有'。\n"
        "原因：数据库的 ctype 是 `C`（中文不算字母）而非 `C.UTF-8`。\n"
        "修法（⚠️ 只影响新建的库，已有库要重建）：\n"
        "    SELECT datctype FROM pg_database WHERE datname = current_database();\n"
        "    -- 期望 C.UTF-8；若是 C，需按 C.UTF-8 重建该库（见 AGENTS §10）\n"
        '    容器初始化参数：POSTGRES_INITDB_ARGS="--encoding=UTF8 --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8"\n'
        "=" * 72
    )


async def warn_if_embedding_lock_mismatch(
    tenant_ids: Sequence[uuid.UUID] | None = None,
) -> None:
    """启动时比对"库里向量的元数据"与"当前 embedding 配置"；对不上就**喊出来**。

    Args:
        tenant_ids: 只查这些租户。**生产不传** —— 不传就扫全部（启动时就是这个行为）。
            存在的唯一理由是**测试要能只查自己造的那个租户**：
            开发库里本来就有真实数据，不限定范围的话测试要么依赖全局状态、要么没法跑。

    ⚠️ **只告警，不阻止启动**（和上面那条同一个理由）：
        读侧会过滤掉不一致的向量（`app/rag/retrieval.py`），问答**仍然可用**，
        只是那部分知识事实上检索不到了。为它拒绝启动，等于把"降级"升级成"事故"。

    ⚠️ **这条不能省。** 不一致时的症状是**答案慢慢变差** —— 不报错、不崩溃、接口 200。
        没有这条自检，唯一的发现途径是"感觉最近答得不对"。
    """
    settings = get_settings()
    lock = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            if tenant_ids is None:
                tenant_ids = list((await session.scalars(select(Chunk.tenant_id).distinct())).all())
            for tenant_id in tenant_ids:
                lock = await inspect(
                    session,
                    tenant_id=tenant_id,
                    expected_model=settings.embedding_model,
                    expected_dim=settings.embedding_dim,
                )
                if not lock.ok:
                    break
    except Exception:
        logger.debug("跳过 embedding 元数据自检（数据库不可达或表还没建）")
        return

    if lock is None or lock.ok:
        _embedding_drift.set(0)
        return

    drift = lock.mismatched + lock.unknown
    _embedding_drift.set(drift)
    logger.warning(
        "\n"
        "=" * 72 + "\n"
        f"⚠️  embedding 元数据锁**不一致**（{drift} 个块的向量用不到）\n"
        "=" * 72 + "\n"
        f"{lock.message()}\n"
        f"库里出现过的模型：{' / '.join(lock.models())}\n"
        "📌 这条**不影响启动**：读侧已按模型过滤，问答仍可用，但那些块检索不到。\n"
        "=" * 72
    )
