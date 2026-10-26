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


def test_calendar_dates_and_derived_expiry_are_not_flagged() -> None:
    """⚠️ **真机首跑就误报的那一条**（2026-10-23 02:06，飞书）。

    回答写"2024 年 3 月签收的订单，其 1 年保修期已于 2025 年 3 月前后到期 [3]"：
    `2024 年 3 月` 来自**用户提问**、`2025 年 3 月` 是模型**算出来**的，
    只有 `1 年` 需要在 `[3]` 里。不排除日历分量的话，
    **任何做了日期推算的回答都会被判串号**，这个比例就废了。
    """
    answer = "- 按签收日起算，2024 年 3 月签收的订单，其 1 年保修期已于 2025 年 3 月前后到期 [3]"

    result = audit(answer, _HITS)

    assert result.unsupported == 0, result.issues


def test_term_values_are_still_checked() -> None:
    """日历分量豁免了，**条款数值不能跟着一起豁免** —— 那是串号的正题。"""
    assert audit("家具主体保修 2 年 [3]", _HITS).unsupported == 1
    assert audit("布艺与填充件 6 个月 [2]", _HITS).unsupported == 1


# ---------------------------------------------------------------- 2026-10-27 校尺
#: 下面几条全部来自串号抽样的真实答案（240 句里 14/16 的误报是这几种）。
#: ⚠️ 它们断言的是**不能报** —— 校尺的方向是"宁可放过，不可误杀"。


def test_list_ordinals_are_layout_not_data() -> None:
    """`2. 补拍照片 [1][4]` —— "2" 是列表序号（2026-10-22 真机误报）。"""
    answer = "2. 补拍破损处的清晰照片/视频（含连接处整体与细节），供客服认定等级 [1][2]"

    result = audit(answer, _HITS)

    assert (result.checked, result.unsupported) == (0, 0)


def test_heading_ordinals_are_layout_not_data() -> None:
    """`**3. 有效期** [2]` —— "3" 是标题序号（2026-10-27 抽样，一条答案连报三处）。"""
    for heading in ("**3. 有效期** [2]", "### 4. 积分怎么来 [2]", "| 5. 退款顺序 | [2]"):
        result = audit(heading, _HITS)
        assert (result.checked, result.unsupported) == (0, 0), heading


def test_decimal_point_is_not_mistaken_for_an_ordinal() -> None:
    """`3.5 米` 里的 `3.` **不是**序号 —— 剥序号不能把小数也剥掉。"""
    hits = [_hit("[示例] 层高 3.5 米，超出部分另计。")]

    result = audit("层高 3.5 米以内不另收费 [1]", hits)

    assert result.checked == 1
    assert result.unsupported == 0, result.issues


def test_unit_written_only_in_the_table_header_is_not_flagged() -> None:
    """原文表格写 `日本 8`（"天"在列头），模型渲染成 `8 天` —— 原文确实支持。

    2026-10-26 真机误报两处就是这个（`_HITS` 之外单独造一条，贴近语料原样）。
    """
    hits = [
        _hit(
            "[无理由与质量问题的区别 > 一句话决策法] 没有 → 无理由，7 天（美国 30 / 德国 14 / 日本 8）。"
        )
    ]

    for answer in (
        "此外，日本站为 **8 天**[1]。",
        "一句话判断：美国 30 天、德国 14 天、日本 8 天 [1]。",
    ):
        result = audit(answer, hits)
        assert result.unsupported == 0, (answer, result.issues)


def test_order_days_written_with_unit_are_not_flagged() -> None:
    """订单快照里是裸值 `8`，模型写"已签收 8 天" —— **不占编号的证据也要认得出来**。

    ⚠️ 这条不只是"少一条噪音"：agent 的交卷自查拿这个审计当闸门，
        误报会让它**把正确答案退回去重写一次**（多花一轮 token）。
    """
    hits = [
        _hit(
            "[退换货总则 > 七天无理由退货] 自签收之日起 7 个自然日内可申请无理由退货。"
            "⚠️ 7 天是总则下限。质量问题为 30 个自然日内。"
        )
    ]

    result = audit(
        "该单已签收 8 天，若属无理由退货已超出窗口，若属质量问题仍在 30 天内 [1]。",
        hits,
        extra_evidence="SF10001 delivered 2026-10-17 8 实木床架",
    )

    assert result.unsupported == 0, result.issues


def test_bare_value_does_not_glue_to_a_neighbouring_number() -> None:
    """⚠️ **2026-10-27 校尺时实测踩到的真 bug**：归一化把空白也删掉之后，
    `2026-10-17 8 件` 会粘成 `2026-10-178件`，于是订单快照里的裸值 `8`
    被判成"原文里没有" —— 订单事实类回答**照样**误报。
    数字必须按"独立出现"判：`8` 也不许去匹配 `18` / `80`。
    """
    hits = [_hit("[订单] 签收后 7 天内可申请无理由退货。")]

    glued = audit("已签收 8 天 [1]。", hits, extra_evidence="placed 2026-10-17 8")
    decoy = audit("已签收 8 天 [1]。", hits, extra_evidence="placed 2026-10-17 18")

    assert glued.unsupported == 0, glued.issues
    assert decoy.unsupported == 1, "18 不许当 8 用"


def test_iso_dates_are_not_checked_as_term_values() -> None:
    """`2026-10-17` 会被拆成 2026 / 10 / 17 三个原子 —— 它是订单日期，不是条款数值。"""
    hits = [_hit("[退换货总则] 自签收之日起 7 个自然日内可申请无理由退货。")]

    result = audit("2026-10-17 签收，退款需质检通过后再发起 [1]。", hits)

    assert (result.checked, result.unsupported) == (0, 0)


def test_a_wrong_number_in_the_same_shaped_sentence_is_still_caught() -> None:
    """放宽单位之后，**数字本身照样要核** —— 这是校尺不能退的那条底线。"""
    hits = [_hit("[保修期限] 家具主体 1 年；布艺与填充件 6 个月。")]

    assert audit("家具主体保修 3 年 [1]", hits).unsupported == 1
    assert audit("布艺与填充件 9 个月 [1]", hits).unsupported == 1


def test_swapped_unit_is_deliberately_tolerated() -> None:
    """⚠️ **已知代价，故意留着**：数字对、单位错（把"48 小时"说成"48 个月"）会放过。

    换掉的是"单位没连写"那一大类误报（占抽样误报的四分之一），
    而"单位写错"型串号在 240 句里**一例都没有**。要收紧回来，
    得先让语料里的表格把单位写进每个格子 —— 那是批次 B 精加语料的活。
    """
    hits = [_hit("[客服 SLA] 超时返工 3 个月内免费；售后邮箱 48 小时内回复。")]

    assert audit("售后邮箱 48 个月内回复 [1]", hits).unsupported == 0
