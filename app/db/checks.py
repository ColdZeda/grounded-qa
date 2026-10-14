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

from sqlalchemy import text

from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

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
