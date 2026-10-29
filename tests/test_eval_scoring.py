"""评测判分器的单元测试（`evals/run.py` 里的纯函数）。

⚠️ 为什么单独测它：判分器**改错了不会报错**，只会让分数悄悄变高或变低 ——
   那是评测里最危险的一类 bug（"数字变好看了，其实是尺子松了"）。
   v1.4 B1（#9）把"找子串"换成"数字集合比对"，就是为了堵住这一类。
"""

from __future__ import annotations

import sys
from pathlib import Path

# `evals/` 不是包（评测脚本只在开发机跑、不进镜像），但它是仓库根下的目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run import (
    _norm,
    _numeric_atoms,
    _point_hit,
    _strip_citation_marks,
)


def _hit(point: str, answer: str) -> bool:
    return _point_hit(point, _norm(_strip_citation_marks(answer)))[0]


# 原子的归一化


def test_years_are_canonicalized_to_months() -> None:
    """`两年` / `2 年` / `24 个月` 抽出来必须是**同一个**原子。"""
    assert _numeric_atoms("两年") == [("month", 24.0)]
    assert _numeric_atoms("2 年") == [("month", 24.0)]
    assert _numeric_atoms("24 个月") == [("month", 24.0)]


def test_days_are_canonicalized_to_hours() -> None:
    assert _numeric_atoms("48 小时") == [("hour", 48.0)]
    assert _numeric_atoms("2 天") == [("hour", 48.0)]
    assert _numeric_atoms("两个自然日") == [("hour", 48.0)]


def test_workdays_stay_their_own_family() -> None:
    """⚠️ `个工作日` **不能**和"天"混：客服话术里这两个数经常同时出现，混了就判不清。"""
    assert _numeric_atoms("3 个工作日") == [("workday", 3.0)]
    assert _numeric_atoms("3 天") == [("hour", 72.0)]


def test_chinese_numerals_are_understood() -> None:
    assert _numeric_atoms("十五天") == [("hour", 360.0)]
    assert _numeric_atoms("三十天") == [("hour", 720.0)]
    assert _numeric_atoms("二十四个月") == [("month", 24.0)]


def test_bare_chinese_numeral_is_not_a_quantity() -> None:
    """⚠️ `一层` / `一次` / `同一` / `统一` 里的"一"是量词/副词，不是数据。

    不挡的话，要点"多一层 `item` 键"会要求回答里出现数字 1 ——
    一个完全正确的回答被判错（实测：知识库 A 的 20 题里误杀 4 题）。
    """
    assert _numeric_atoms("多一层 item 键") == []
    assert _numeric_atoms("同一请求只解析一次") == []
    assert _numeric_atoms("全线统一保修") == []


def test_clock_times_are_not_numbers() -> None:
    """`09:00–21:00` 是**格式**不是政策数值 —— 拆成 9/00 只会把对的答案判错。"""
    assert _numeric_atoms("工作日 09:00–21:00") == []
    assert _hit("在线客服工作日 09:00–21:00", "在线客服服务时间为工作日 0900–2100")


def test_range_gives_both_ends() -> None:
    """`3~5 个工作日` 是**两个**原子 —— 只答一端就是没答全。"""
    assert _numeric_atoms("3~5 个工作日") == [("workday", 3.0), ("workday", 5.0)]
    assert _numeric_atoms("3 到 5 个工作日") == [("workday", 3.0), ("workday", 5.0)]


# 要点判定


def test_synonym_rewrite_still_hits() -> None:
    """**同义改写不许被判错**：`2 年` 写成 `24 个月` / `两年` 都是对的。"""
    assert _hit("家具主体 2 年", "家具主体保修 24 个月。")
    assert _hit("家具主体 2 年", "家具主体保两年。")


def test_wrong_number_no_longer_slips_through() -> None:
    """⚠️ 旧口径的真实误判：要点 `2 年` 只要回答里有个 `2` 就算命中。

    `20 个月`、`2024 年` 都能把它蒙过去 —— 于是"答错了"被判成"答对了"，
    而且**从结果里看不出来**（分数只会偏高）。
    """
    assert not _hit("家具主体 2 年", "家具主体保修 20 个月，2024 年起算。")


def test_hours_and_days_are_interchangeable() -> None:
    assert _hit("48 小时", "签收后 2 天内可以报障")
    assert not _hit("7 天", "签收后 48 小时内报障")


def test_day_and_month_are_not_interchangeable() -> None:
    """⚠️ `30 天` ≠ `1 个月`：月长不固定，拿约等号判分会把判分器自己变成噪音源。"""
    assert not _hit("30 天", "1 个月内可退货")


def test_range_needs_both_ends() -> None:
    assert _hit("3~5 个工作日", "信用卡 3-5 个工作日到账")
    assert not _hit("3~5 个工作日", "信用卡 3 个工作日到账")


def test_latin_word_is_still_required() -> None:
    """要点里点名了平台（`PayPal`），回答没提到就算没答到。"""
    assert not _hit("PayPal 3~5 个工作日到账", "信用卡 3~5 个工作日到账")
    assert _hit("PayPal 3~5 个工作日到账", "PayPal 3~5 个工作日到账")


def test_citation_marks_do_not_feed_numbers() -> None:
    """`[1]` 里的 1 不能当成"回答了 1 年"。"""
    assert not _hit("电子件 1 年", "电子件的保修期限见下表[2]。")


def test_point_without_numbers_falls_back_to_bigram_coverage() -> None:
    """没有数字的要点走二元组覆盖，并且**标记成弱判定**（单独计数，不和数字型混）。"""
    hit, weak = _point_hit("顾客承担退回运费", _norm("退回运费由顾客承担。"))
    assert hit is True
    assert weak is True


def test_numeric_point_is_not_weak() -> None:
    _hit_result, weak = _point_hit("德国 14 天法定撤回权", _norm("德国有 14 天法定撤回权"))
    assert weak is False
