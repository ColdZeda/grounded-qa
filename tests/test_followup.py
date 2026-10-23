"""追问检索（v1.4 C1'）—— **纯规则**，只测"什么时候拼、怎么拼"。

⚠️ 为什么这个函数值得单测：拼错了不会报错，只会把召回**引到上一轮的话题**上，
   而用户看到的仍是一条"看起来正常"的回答 —— 最难查的那类坏。
"""

from __future__ import annotations

from app.rag.followup import MAX_FOLLOWUP_CHARS, is_followup, retrieval_query

HISTORY = [
    {"role": "user", "content": "退货运费谁出"},
    {"role": "assistant", "content": "无理由退货由顾客承担原价运费。"},
]


def test_short_followup_gets_previous_question_prefixed() -> None:
    """「德国呢」自己检索不到东西 —— 拼上上一轮问句才有语义。"""
    assert retrieval_query("德国呢", HISTORY) == "退货运费谁出 德国呢"


def test_previous_question_goes_first() -> None:
    """⚠️ 上一轮放**前面**：检索按"整串像不像某段话"打分，
    「德国呢」里没有任何政策词，能对上文档的是前面那半句。"""
    rewritten = retrieval_query("这个能退吗", HISTORY)
    assert rewritten.startswith("退货运费谁出")


def test_long_question_is_never_rewritten() -> None:
    """长问句自己够清楚，拼上一轮只会**稀释**召回。"""
    long_question = "我在慕尼黑买了个刻字的餐盘现在想退，德国站的规则和总则哪个优先呢？"
    assert len(long_question) > MAX_FOLLOWUP_CHARS
    assert retrieval_query(long_question, HISTORY) == long_question


def test_no_pronoun_means_no_rewrite() -> None:
    """没有指代词 = 这一问自己能独立成意，不要动它。"""
    assert retrieval_query("保修多久", HISTORY) == "保修多久"


def test_no_history_means_no_rewrite() -> None:
    assert retrieval_query("德国呢", ()) == "德国呢"
    assert retrieval_query("德国呢", []) == "德国呢"


def test_first_question_of_a_conversation_is_never_rewritten() -> None:
    """会话第一句：历史里只有它自己 → 不拼。

    ⚠️ 这也是"评测为什么零回归"的原因：`evals/run.py` 从来不传历史。
    """
    history = [{"role": "user", "content": "德国呢"}]
    assert retrieval_query("德国呢", history) == "德国呢"


def test_assistant_turns_are_ignored() -> None:
    """上一轮**用户**问句才算数 —— 拿助手的回答去检索是另一回事。"""
    history = [{"role": "assistant", "content": "我们支持 7 天无理由退货。"}]
    assert retrieval_query("德国呢", history) == "德国呢"


def test_current_question_appearing_in_history_is_skipped() -> None:
    """⚠️ `app/api/chat.py` 是**先落库本问、再读历史**的。

    不跳过"与本问相同"的那条，就会把它自己拼回去（"德国呢 德国呢"）。
    """
    history = [*HISTORY, {"role": "user", "content": "德国呢"}]
    assert retrieval_query("德国呢", history) == "退货运费谁出 德国呢"


def test_very_long_previous_question_is_capped() -> None:
    """历史问句太长会把这一问整个淹掉 —— 截到 60 字再说。"""
    history = [{"role": "user", "content": "很长的问题" * 30}]
    rewritten = retrieval_query("那日本呢", history)
    previous_part, _, current_part = rewritten.rpartition(" ")
    assert current_part == "那日本呢"
    assert len(previous_part) <= 60


def test_is_followup_matches_what_retrieval_query_does() -> None:
    assert is_followup("德国呢", HISTORY) is True
    assert is_followup("保修多久", HISTORY) is False
    assert is_followup("德国呢", ()) is False
