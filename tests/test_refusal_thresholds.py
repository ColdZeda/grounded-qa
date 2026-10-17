"""拒答阈值：**两套阈值**的边界行为（2026-10-17，PLAN §8.4 D2 收口）。

为什么单独测这个：

    用一套阈值同时应付两种分数时，接口照样返回 200、拒答文案也照旧，
    **唯一的症状是"该答的题被拒了"** —— 而用户在界面上看到的是
    "抱歉，我在现有知识库里没有找到足够依据"，看起来完全合理。
    这类"静默的坏"必须靠断言钉住，不能靠肉眼。

只测 `_should_refuse` 这一个纯判断（不连库、不调模型）：
它只依赖 `self._settings`，所以用 `__new__` 造个空壳就够 ——
顺便也说明了这个判断**不掺任何 I/O**，是可以被确定性地复现的。
"""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.rag.pipeline import RagPipeline
from app.rag.retrieval import RetrievedChunk

#: 余弦 0.50：低于 rerank 阈值（0.60）、高于余弦阈值（0.45）——
#: 这正是"用错尺子就会误拒"的那个区间。
_AMBIGUOUS = 0.50


def _pipeline() -> RagPipeline:
    """不连库的 pipeline 空壳：只需要 `_settings`。"""
    pipe = RagPipeline.__new__(RagPipeline)
    pipe._settings = get_settings()
    return pipe


def _hits() -> list[RetrievedChunk]:
    """至少一条命中 —— 否则会先命中"一条都没检索到 → 拒答"那条分支。"""
    return [
        RetrievedChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ordinal=0,
            content="七天无理由退货，自签收之日起算。",
            score=_AMBIGUOUS,
        )
    ]


def test_cosine_uses_its_own_threshold_not_the_rerank_one() -> None:
    """同一个 0.50：按余弦口径**不拒**，按 rerank 口径**拒**。

    这条就是 D2 的回归守卫 —— 如果哪天有人把两个阈值合并回一个，
    它会立刻红。
    """
    pipe = _pipeline()
    settings = pipe._settings
    assert settings.answer_min_cosine < _AMBIGUOUS < settings.answer_min_score

    assert pipe._should_refuse(_hits(), _AMBIGUOUS, "vector_cosine") is False
    assert pipe._should_refuse(_hits(), _AMBIGUOUS, "rerank") is True


def test_cosine_below_its_own_threshold_refuses() -> None:
    pipe = _pipeline()
    below = pipe._settings.answer_min_cosine - 0.01
    assert pipe._should_refuse(_hits(), below, "vector_cosine") is True


def test_rerank_above_threshold_answers() -> None:
    pipe = _pipeline()
    above = pipe._settings.answer_min_score + 0.01
    assert pipe._should_refuse(_hits(), above, "rerank") is False


def test_no_hits_refuses_regardless_of_kind() -> None:
    """一条都没检索到 → 拒答。这一条**先于**所有阈值判断。"""
    pipe = _pipeline()
    for kind in ("rerank", "vector_cosine", "none"):
        assert pipe._should_refuse([], 0.99, kind) is True


def test_none_confidence_never_refuses() -> None:
    """只有 trgm 命中（置信度不可用）→ **不拒答**，把出处给用户自己核对。

    ⚠️ 别把它改成"拒答"：那等于在"没有可信分数"时把拒答打开，
       与"拒答必须有确定性依据"的纪律冲突（见 architecture §5.2）。
    """
    pipe = _pipeline()
    assert pipe._should_refuse(_hits(), None, "none") is False
