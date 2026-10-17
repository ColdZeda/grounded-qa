"""评测报告页的聚合逻辑 —— **纯单元测试**（造 payload，不读真文件、不连库）。

⚠️ 这里守的是那句"**不许裸数字**"：
    每条指标都要带分母，每份快照都要带生成时间；缺值要显示成 `—` 而不是 0。
"""

from __future__ import annotations

from app.api.reports import (
    _display_time,
    _num,
    _ratio,
    _summarize_answer_layer,
    _summarize_retrieval,
)


def test_display_time_formats_stamp() -> None:
    assert _display_time("20261017T112745Z") == "2026-10-17 11:27:45 UTC"


def test_display_time_does_not_guess_on_unknown_format() -> None:
    """认不出来就原样带出来 —— 猜出来的时间比没有时间更糟。"""
    assert _display_time("昨天下午") == "昨天下午"


def test_ratio_never_invents_a_denominator() -> None:
    assert _ratio(3, 5) == "3/5"
    # 分母为零是"不存在"，不是"零分"
    assert _ratio(0, 0) == "—"
    assert _num(None) == "—"
    assert _num(0.9199) == "0.920"


def test_summarize_retrieval_keeps_config_and_denominator() -> None:
    payload = {
        "kind": "retrieval",
        "generated_at": "20261017T112745Z",
        "top_k": 5,
        "config": {
            "retrieve_top_k": 20,
            "rrf_k": 60,
            "fulltext_min_similarity": 0.2,
            "embedding_model": "qwen3.7-text-embedding-flash",
            "rerank_model": "qwen3.7-text-rerank",
        },
        "meta": {"kb": "senyu", "questions": 25},
        "ablations": [{"name": "rrf+rerank", "hit_at_k": 1.0, "hit_count": 25, "mrr_at_10": 0.98}],
    }

    snapshot = _summarize_retrieval(payload, filename="retrieval-x.json", stamp="20261017T112745Z")

    assert snapshot.kind == "retrieval"
    assert snapshot.questions == 25
    # 分子分母必须一起出现
    assert any("25/25" in metric.value for metric in snapshot.metrics)
    # 那次运行的配置必须带出来 —— 换个 fulltext_min 数字就会变
    assert snapshot.config["fulltext_min_similarity"] == "0.2"
    assert snapshot.config["消融组"] == "rrf+rerank"


def test_summarize_refusal_marks_the_zero_cost_path() -> None:
    payload = {
        "kind": "refusal",
        "generated_at": "20261017T110457Z",
        "kb": "senyu",
        "llm_model": "deepseek-flash",
        "results": [
            {"id": "r1", "passed": True, "refused": True, "llm_calls": 0},
            {"id": "r2", "passed": True, "refused": False, "llm_calls": 1, "must_say_ok": True},
            {"id": "r3", "passed": False, "refused": False, "llm_calls": 1, "must_say_ok": False},
        ],
    }

    snapshot = _summarize_answer_layer(
        payload, filename="refusal-x.json", stamp="20261017T110457Z", kind="refusal"
    )

    assert snapshot.questions == 3
    by_name = {metric.name: metric.value for metric in snapshot.metrics}
    assert by_name["行为正确"] == "2/3"
    assert by_name["真的拒答了"] == "1/3"
    # 拒答是调用前的判断 —— 这一项要能看出"省了几次调用"
    assert by_name["一次模型都没调"] == "1/3"
    assert by_name["内容要点达标"] == "1/2"
    assert snapshot.config["对话模型"] == "deepseek-flash"


def test_summarize_generation_counts_forbidden_phrases() -> None:
    payload = {
        "kind": "generation",
        "generated_at": "20261017T112536Z",
        "kb": "senyu",
        "llm_model": "deepseek-flash",
        "results": [
            {"id": "g1", "passed": True, "cite_ok": True, "forbidden_hit": ""},
            {"id": "g2", "passed": False, "cite_ok": None, "forbidden_hit": "1 年"},
        ],
    }

    snapshot = _summarize_answer_layer(
        payload, filename="generation-x.json", stamp="20261017T112536Z", kind="generation"
    )

    by_name = {metric.name: metric.value for metric in snapshot.metrics}
    assert by_name["全通过"] == "1/2"
    # 只统计"真的检查过"的那些题，没检查的不该被算成错
    assert by_name["引用正确"] == "1/1"
    assert by_name["出现禁语"] == "1/2"
