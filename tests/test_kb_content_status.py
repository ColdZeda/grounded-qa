"""目录名 → `content_status` 的映射（v1.3 阶段 A）。

⚠️ 这是 `PLAN §8.6` 点名的「**唯一隐性成本**」：
   语料目录一挪，映射不改就**静默变状态** —— 不报错，只是不该引用的文档悄悄进了候选集。
   所以这里逐条钉死：目录真挪了，这个文件必须红。

⚠️ 迁移里的回填 SQL 是同一份映射的**第二个实现**（`migrations/versions/…10cd5033d34e…`），
   那次机会已经用掉了；以后新增语料走 `classify_content_status`，别再改迁移。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.models.knowledge import ContentStatus
from app.kb.ingest import classify_content_status


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 90 = 历史版本 → 已被新版取代
        (
            "senyu/90-干扰-历史版本/02-2023版保修条款（已废止）.md",
            ContentStatus.SUPERSEDED,
        ),
        # 例外：变更公告本身就是"现行"的（它讲的就是"哪版作废了"）
        ("senyu/90-干扰-历史版本/06-版本变更公告.md", ContentStatus.ACTIVE),
        # 91 = 兄弟品牌
        ("senyu/91-干扰-森屿Lab/03-森屿Lab保修政策.md", ContentStatus.SIBLING_BRAND),
        # 92 = 未生效草案
        (
            "senyu/92-干扰-其他地区/01-英国站上线预览（未生效草案）.md",
            ContentStatus.DRAFT,
        ),
        # 例外：辟谣澄清是现行口径
        (
            "senyu/92-干扰-其他地区/04-站外流传的假政策澄清.md",
            ContentStatus.ACTIVE,
        ),
        # 93 = 内部材料，不该直接引给客户
        ("senyu/93-干扰-内部FAQ/02-客服话术库·高频问题.md", ContentStatus.INTERNAL),
        # 94 = 相邻概念 **故意算现行**：这些本来就是给客户看的
        ("senyu/94-干扰-相邻概念/01-保修与退换货的区别.md", ContentStatus.ACTIVE),
        # 正样本目录
        ("senyu/40-服务/40-保修与维修.md", ContentStatus.ACTIVE),
        # 知识库 A（公开文档）落在 active
        ("fastapi/tutorial/path-params.md", ContentStatus.ACTIVE),
        # Windows 风格分隔符也要认（有人会这么传）
        ("senyu\\90-干扰-历史版本\\01-2023版退货政策（已废止）.md", ContentStatus.SUPERSEDED),
    ],
)
def test_classify_by_directory(filename: str, expected: ContentStatus) -> None:
    status, _, _ = classify_content_status(filename)
    assert status is expected


def test_legacy_policy_has_effective_range() -> None:
    """2023 版原文的横幅写着"已于 2025-12-31 废止" —— 区间必须是这两天。

    ⚠️ 这个区间是**追溯**用的：客户问"我 2023 年那单保修几年"，要能按那会儿的版本答。
    """
    status, start, end = classify_content_status(
        "senyu/90-干扰-历史版本/02-2023版保修条款（已废止）.md"
    )
    assert status is ContentStatus.SUPERSEDED
    assert start == date(2023, 1, 1)
    assert end == date(2025, 12, 31)


def test_current_policy_starts_2026() -> None:
    status, start, end = classify_content_status("senyu/40-服务/40-保修与维修.md")
    assert status is ContentStatus.ACTIVE
    assert start == date(2026, 1, 1)
    assert end is None


def test_non_policy_docs_leave_range_empty() -> None:
    """品牌介绍 / 兄弟品牌 / 内部 FAQ 没有"版本"概念 —— 留空比瞎填一个日期诚实。"""
    _, start, end = classify_content_status("senyu/91-干扰-森屿Lab/01-森屿Lab品牌介绍.md")
    assert (start, end) == (None, None)


def test_unknown_path_defaults_to_active() -> None:
    """没见过的路径一律 `active`：**宁可漏标，不可误标** ——
    误标成 superseded 会让一份正常文档突然搜不到，那是更难发现的坏。
    """
    status, start, end = classify_content_status("user-upload/随便传的.md")
    assert status is ContentStatus.ACTIVE
    assert (start, end) == (None, None)
