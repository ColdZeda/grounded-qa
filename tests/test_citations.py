"""引用可核对性 —— 回答里的 `[n]` 指对了没有。

核心用例来自 2026-10-23 生产真机：模型把「布艺与填充件 **6 个月**」标成 `[2]`，
而那两行写在 `[3]` 里，`[2]` 是另一张没有任何期限数字的表。

⚠️ 这套检查**只抓不改**（理由见 `app/rag/citations.py`）—— 所以这里的断言全部是
"能不能识别出来"，不是"改写成什么样"。
"""

from __future__ import annotations

import uuid

from app.rag.citations import StreamingRepair, audit
from app.rag.citations import repair as repair_citations
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


# --------------------------------------------------- 修法 D：把编号指回去（repair）
#: 这三块**只有 [3] 支持"6 个月"** —— 与真机那一幕同构。
_REPAIR_HITS = [
    _hit("[版本变更公告 > 旧值与新值对照] 美国站包邮门槛由 $49 调整为 $59。"),
    _hit("[保修与退换货的区别 > 对照表] 时间窗口：签收后 7 天（无理由）/ 30 天（质量问题）。"),
    _hit("[2023 年版保修条款 > 保修期限] 家具主体 1 年；布艺与填充件 6 个月；五金配件 6 个月。"),
]


def test_a_year_in_the_heading_does_not_validate_a_term_value() -> None:
    """⚠️ **2026-10-27 实测踩到的坑**：`家具主体保修 3 年` 去比《**2023 年版**保修条款》，
    纯子串匹配会因为标题里的 "2023 年版" 而通过（"3年" 正好是 "2023年版" 的子串）。

    数字必须**两侧不接别的数字**才算原文里有它 —— 这条对"完整出现"同样适用。
    """
    hits = [_hit("[2023 年版保修条款 > 保修期限] 家具主体 1 年；布艺与填充件 6 个月。")]

    result = audit("家具主体保修 3 年 [1]", hits)

    assert result.unsupported == 1, "2023 年版里的 3 不能当 3 年用"
    # 而真正写着的那个数当然该过
    assert audit("家具主体保修 1 年 [1]", hits).unsupported == 0


def test_repair_points_the_marker_at_the_chunk_that_really_has_it() -> None:
    """**修法 D 的样板**：`[2]` 改成 `[3]`，**一个字都没重写**。"""
    answer = "- 布艺与填充件：**6 个月**，自签收起算 [2]"

    result = repair_citations(answer, _REPAIR_HITS)

    assert result.text == "- 布艺与填充件：**6 个月**，自签收起算 [3]"
    assert result.changed is True
    (fix,) = result.fixes
    assert (fix.marker, fix.corrected) == (2, 3)
    assert fix.chunk_id == str(_REPAIR_HITS[2].chunk_id)
    # 改完必须真的核得过 —— 否则"修了"是假的。
    assert audit(result.text, _REPAIR_HITS).unsupported == 0


def test_repair_leaves_a_correct_citation_untouched() -> None:
    """对的那句**一个字节都不动** —— 这是它敢上线的前提。"""
    answer = "- 布艺与填充件：6 个月 [3]"

    result = repair_citations(answer, _REPAIR_HITS)

    assert result.text == answer
    assert result.fixes == []


def test_repair_refuses_when_no_chunk_supports_the_sentence() -> None:
    """0 块支持（数字哪儿都没有）→ **不动**。宁可留着报，也不乱指。"""
    answer = "家具主体保修 3 年 [3]"

    result = repair_citations(answer, _REPAIR_HITS)

    assert result.text == answer
    assert result.fixes == []
    assert audit(result.text, _REPAIR_HITS).unsupported == 1


def test_repair_refuses_when_more_than_one_chunk_could_be_the_source() -> None:
    """2 块都支持（`7 天` 在第 2 块、`30 天` 也在第 2 块；再造一块同样含 `30 天` 的）→ **不动**。

    真机上就有这种：`gen-09` 那句「德国 €69、日本 ¥8000…本次未调整」同时命中 3 块。
    """
    hits = [
        _hit("[总则] 签收后 7 天内可无理由退货。"),
        _hit("[区别] 质量问题 30 天内；无理由 7 天。"),
        _hit("[地区] 德国站适用 14 天法定撤回权，质量问题 30 天。"),
    ]

    result = repair_citations("质量问题 30 天内可退 [0]", hits)

    assert result.fixes == []


def test_repair_refuses_a_sentence_with_several_markers() -> None:
    """一句里有**两个编号** → "该改哪一个"说不清，不动。"""
    answer = "家具主体 1 年、布艺件 6 个月 [1][2]"

    result = repair_citations(answer, _REPAIR_HITS)

    assert result.fixes == []


def test_repair_requires_the_new_chunk_to_support_every_atom() -> None:
    """新编号必须支持这句的**全部**原子，不是"把缺的那个补上就行"。

    `1 年` 只在第 3 块有、`$59` 只在第 1 块有 ⟹ 没有哪一块能同时支持两个 ⟹ 不动。
    """
    answer = "包邮门槛 $59，家具主体保修 1 年 [2]"

    result = repair_citations(answer, _REPAIR_HITS)

    assert result.fixes == []


def test_repair_does_not_touch_sentences_supported_by_order_facts() -> None:
    """订单事实（`extra_evidence`）支持的数字不该触发改编号。

    "已签收 8 天" 既不在第 1 块也不在第 3 块 —— 但它由订单快照支持，
    所以这句**本来就不该被判错**，更不该被改到某一块上去。
    """
    hits = [_hit("[总则] 签收后 7 天内可无理由退货。"), _hit("[保修] 家具主体 1 年。")]

    result = repair_citations(
        "该单已签收 8 天，超过 7 天窗口 [1]",
        hits,
        extra_evidence="SF10001 delivered 2026-10-17 8 实木床架",
    )

    assert result.text == "该单已签收 8 天，超过 7 天窗口 [1]"
    assert result.fixes == []


def test_streaming_repair_matches_the_whole_text_repair() -> None:
    """流式逐字喂 vs 整段修 —— **结果必须一模一样**（否则发出去的和核对的对不上）。"""
    answer = "- 布艺与填充件：6 个月 [2]\n- 家具主体：1 年 [3]\n"
    buffer = StreamingRepair(_REPAIR_HITS)

    out = "".join(buffer.feed(char) for char in answer)
    out += buffer.flush()

    assert out == repair_citations(answer, _REPAIR_HITS).text
    assert out.count("[3]") >= 2
    assert buffer.raw_text == answer, "原文必须能原样取回（量模型串号率要用它）"
    assert len(buffer.fixes) == 1


def test_streaming_repair_holds_until_the_sentence_ends() -> None:
    """句末到之前**一个字都不发** —— 这正是"改在发出去之前"的机制。"""
    buffer = StreamingRepair(_REPAIR_HITS)

    assert buffer.feed("布艺与填充件：6 个") == ""
    assert buffer.feed("月 [2]") == ""
    assert buffer.feed("。") == "布艺与填充件：6 个月 [3]。"


def test_streaming_repair_gives_up_rather_than_stalling_the_stream() -> None:
    """攒太久（没有句末的巨长句）→ **原样发**，不许把流式卡住。"""
    buffer = StreamingRepair(_REPAIR_HITS, max_hold=20)

    out = buffer.feed("很长很长的一段没有任何句号的话一直到超出上限")

    assert out == "很长很长的一段没有任何句号的话一直到超出上限"
    assert buffer.fixes == []
