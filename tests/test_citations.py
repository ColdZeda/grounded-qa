"""引用可核对性 —— 回答里的 `[n]` 指对了没有。

核心用例来自 2026-10-23 生产真机：模型把「布艺与填充件 **6 个月**」标成 `[2]`，
而那两行写在 `[3]` 里，`[2]` 是另一张没有任何期限数字的表。

⚠️ 这套检查**只抓不改**（理由见 `app/rag/citations.py`）—— 所以这里的断言全部是
"能不能识别出来"，不是"改写成什么样"。
"""

from __future__ import annotations

import uuid

from app.rag.citations import audit
from app.rag.retrieval import RetrievedChunk


def _hit(content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ordinal=0,
        content=content,
        score=0.9,
    )


#: 还原真机那一幕：三条来源，只有 [3] 有"6 个月"这个数
_HITS = [
    _hit("[版本变更公告 > 旧值与新值对照] 家具主体保修期由 1 年调整为 2 年。"),
    _hit("[保修与退换货的区别 > 对照表] 时间窗口：签收后 7 天（无理由）/ 30 天（质量问题）。"),
    _hit("[2023 年版保修条款 > 保修期限] 家具主体 1 年；布艺与填充件 6 个月；五金配件 6 个月。"),
]


def test_the_real_badcase_is_caught() -> None:
    """**这就是生产上抓到的那一条**：数字出现在 `[3]` 里，却标了 `[2]`。"""
    answer = "- 布艺与填充件（如布面、抱枕芯）：**6 个月**，自签收起算 [2]"

    result = audit(answer, _HITS)

    assert result.checked == 1
    assert result.unsupported == 1
    assert result.ok is False
    assert result.issues[0].markers == (2,)
    assert "6个月" in result.issues[0].atoms


def test_correct_citation_passes() -> None:
    """同一句话标的要是 `[3]`，就该通过 —— 检查器不能把对的也判错。"""
    answer = "- 布艺与填充件：**6 个月**，自签收起算 [3]"

    result = audit(answer, _HITS)

    assert (result.checked, result.unsupported) == (1, 0)
    assert result.ok is True


def test_multiple_markers_are_satisfied_by_any_cited_source() -> None:
    """一句话用两段资料各支撑一半，是**合法写法** —— 只要原子出现在其中任一段。"""
    answer = "家具主体 1 年、电子件 2 年的时间窗口见 [1][3]"

    result = audit(answer, _HITS)

    assert result.unsupported == 0


def test_marker_without_any_number_is_not_penalised() -> None:
    """⚠️ "这条请咨询人工 [2]" 这类句子全是名词 —— **不计入分母**，也不算错。"""
    answer = "这条建议你联系人工客服确认 [2]"

    result = audit(answer, _HITS)

    assert (result.checked, result.unsupported) == (0, 0)
    assert result.ok is True
    assert result.rate == 0.0


def test_citation_markers_are_not_mistaken_for_numbers() -> None:
    """`[3]` 里的 3 不能被当成"数据 3"拿去核对 —— 否则每条引用都会假阳性。"""
    answer = "签收后 30 天内可退 [2]"

    result = audit(answer, _HITS)

    assert result.unsupported == 0


def test_empty_inputs_are_safe() -> None:
    assert audit("", _HITS).checked == 0
    assert audit("随便一句话 [1]", []).checked == 0


def test_out_of_range_marker_is_flagged() -> None:
    """引用了不存在的编号 —— 同样要报（用户点进去会 404）。"""
    answer = "家具主体保修 2 年 [9]"

    result = audit(answer, _HITS)

    assert result.unsupported == 1
    assert result.issues[0].markers == (9,)
