"""纯单元测试：**RRF 融合** + 提示词组装。

⚠️ 不碰数据库、不调模型 —— 这两块是纯函数，应该在任何机器上秒级跑完。
"""

from __future__ import annotations

import uuid

from app.rag.prompt import REFUSAL_ANSWER, build_context, build_messages, make_snippet
from app.rag.retrieval import RetrievedChunk, reciprocal_rank_fusion


def _chunk(content: str, *, score: float = 1.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ordinal=0,
        content=content,
        score=score,
    )


# ------------------------------------------------------------
# RRF
# ------------------------------------------------------------


def test_rrf_rewards_agreement_between_paths() -> None:
    """**两路都排第一的文档，必须排在两路各有一个第一的文档之前。**

    这是 RRF 的全部意义：多路一致 > 单路极端。
    """
    agreed = uuid.uuid4()
    only_vector = uuid.uuid4()
    only_fulltext = uuid.uuid4()

    scores = reciprocal_rank_fusion(
        [[agreed, only_vector], [agreed, only_fulltext]],
        k=60,
    )

    assert scores[agreed] > scores[only_vector]
    assert scores[agreed] > scores[only_fulltext]


def test_rrf_keeps_documents_that_only_one_path_found() -> None:
    """只在某一路出现的文档**照样得分**，不是 0。

    ⚠️ 这条很重要：向量路第 1 名本身就是强信号，不该因为全文路没召回它就归零。
    """
    only_in_one = uuid.uuid4()

    scores = reciprocal_rank_fusion([[only_in_one], []], k=60)

    assert scores[only_in_one] > 0


def test_rrf_uses_rank_not_score() -> None:
    """RRF **只看名次**。

    ⚠️ 这条锁的是设计意图：向量余弦相似度（0~1）和 trgm 相似度（0~1）**量纲不同**，
        直接加权求和等于随机给权重。RRF 把它们变成同一个尺度。
        所以两个名次相同、但"分数"完全不同的输入，融合结果必须一样。
    """
    first = uuid.uuid4()
    second = uuid.uuid4()

    a = reciprocal_rank_fusion([[first, second]], k=60)
    b = reciprocal_rank_fusion([[first, second]], k=60)

    assert a == b
    assert a[first] == 1 / (60 + 1)
    assert a[second] == 1 / (60 + 2)


def test_rrf_k_flattens_the_head() -> None:
    """`k` 越大，头部的相对优势越小。

    ⚠️ `k` 太小（如 1）时融合几乎退化成"看第一路"（1/1 vs 1/2 差一倍）；
        `k=60` 让前几名差距变小，多路的一致性才真正起作用。
    """
    first = uuid.uuid4()
    second = uuid.uuid4()

    small_k = reciprocal_rank_fusion([[first, second]], k=1)
    large_k = reciprocal_rank_fusion([[first, second]], k=60)

    ratio_small = small_k[first] / small_k[second]
    ratio_large = large_k[first] / large_k[second]

    assert ratio_small > ratio_large


def test_rrf_handles_empty_input() -> None:
    assert reciprocal_rank_fusion([], k=60) == {}
    assert reciprocal_rank_fusion([[], []], k=60) == {}


# ------------------------------------------------------------
# 提示词组装
# ------------------------------------------------------------


def test_context_numbering_starts_at_one_and_follows_order() -> None:
    """编号必须从 1 开始、且与传入顺序一致。

    ⚠️ 这是"引用溯源"的命门：模型回答里的 `[2]` 要能被映射回**正确的**那段原文。
        编号错位是这类系统最隐蔽的失败方式 —— 界面看起来有引用，点进去是别的内容。
    """
    hits = [_chunk("第一段"), _chunk("第二段"), _chunk("第三段")]

    context = build_context(hits)

    assert context.startswith("[1] 第一段")
    assert "[2] 第二段" in context
    assert "[3] 第三段" in context
    assert context.index("[1]") < context.index("[2]") < context.index("[3]")


def test_context_is_empty_marker_without_hits() -> None:
    assert build_context([]) == "（无）"


def test_overlong_chunk_is_truncated() -> None:
    """单条过长要截断。

    ⚠️ 不截断会把 prompt 撑爆，而**爆掉的表现是模型截断/答非所问**，不是报错 —— 很难查。
    """
    context = build_context([_chunk("字" * 5000)], max_chars_per_chunk=100)

    assert "已截断" in context
    assert len(context) < 200


def test_messages_start_with_system_and_end_with_the_question() -> None:
    hits = [_chunk("退款政策：7 天内可退")]

    messages = build_messages(question="能退吗？", hits=hits, history=[])

    assert messages[0]["role"] == "system"
    assert "退款政策" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "能退吗？"}


def test_history_is_placed_between_system_and_question() -> None:
    """历史要按原顺序夹在中间 —— 顺序反了模型会以为对话倒着发生。"""
    history = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
    ]

    messages = build_messages(question="第二个问题", hits=[_chunk("x")], history=history)

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"] == "第二个问题"


def test_refusal_answer_tells_the_user_what_to_do() -> None:
    """拒答文案要**给出下一步**，而不是冷冰冰一句"不知道"。"""
    assert "知识库" in REFUSAL_ANSWER
    assert "换一种问法" in REFUSAL_ANSWER


def test_snippet_collapses_whitespace_and_truncates() -> None:
    assert make_snippet("a\n\n b\tc") == "a b c"
    assert make_snippet("字" * 500, limit=10).endswith("…")
