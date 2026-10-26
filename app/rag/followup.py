"""追问检索：短问句 ＋ 指代词时，把**上一轮问句**拼进检索 query（v1.4 C1'）。

问题长这样：用户先问「退货运费谁出」，接着说「**德国呢**」。第二句里没有任何政策词，
全文（trgm）和向量都检索不到《地区差异》那一段 —— 但它显然是同一个话题的追问。

⚠️ **为什么是规则拼接，不是让模型改写**（`PLAN §8.7` D2）：
   多一次模型调用 ＝ 多一处不确定性，而且改错**无法解释**（同一个问题两次运行召回不同，
   badcase 归因就断了）。规则拼接的每一步都能复述给用户听。

⚠️ **什么时候才拼（刻意保守，三条缺一不拼）**：

    ① 这一问 **≤ 15 字** —— 长问句自己就够清楚了，拼上一轮只会**稀释**召回；
    ② 且含**指代词**（它 / 这单 / 那个 / 呢 …）—— 这类词自己不能独立成意；
    ③ 且历史里找得到一条**有实质内容的**用户问句（跳过与本问完全相同的、
       以及**本身也是追问的**那些 —— 连着追问三四句时要一直往前找，见
       `_previous_user_question`）。

    ⚠️ 宁可不改，也不要改错：拼错了会把召回**引到上一轮的话题**上，
       而用户看到的仍是一条"看起来正常"的回答 —— 这是最难查的那类坏。

⚠️ 只影响**检索**。提示词里永远是用户这一问的原话 ——
   把「德国呢」改写成别的句子塞给模型，等于替用户重述问题。

> ⚠️ **LEGACY（2026-10-26 起）：只服务 `pipeline`（含 agent 的回落路径），agent 路径不用它。**
> agent 的多轮记忆靠 `conversations.meta.scratchpad`（那是"真记住查到的事实"），
> 不靠“用规则猜上一句”。
> ⚠️ **为什么不删**：回落路径还在用，删了就是“最坏情况比今天还差”，
>    直接破坏 `PLAN §13.3` 的兜底承诺。退役条件见 `CANDIDATES.md` W16。
"""

from __future__ import annotations

from collections.abc import Sequence

#: 超过这个长度就不算"追问"（长问句自己带足了语境）。
MAX_FOLLOWUP_CHARS = 15

#: 指代词。⚠️ 刻意收窄：`这` / `那` 单字太常见（"这个怎么处理"不是追问），
#: 只认能明确指代上一轮话题的写法。
_PRONOUNS = ("它", "这单", "那单", "这票", "这个", "那个", "呢")

#: 拼进 query 时上一轮问句的长度上限 —— 太长的历史问句会把这一问整个淹掉。
_MAX_PREVIOUS_CHARS = 60


def _is_thin(content: str) -> bool:
    """这句**自己就是个追问**（短 ＋ 带指代词）⟹ 它本身没有实质内容。

    ⚠️ 不能只看长度：`能退款吗`（5 字）虽然短，但**自己能独立成意**，
       跳过它会把检索引到更早、已经跑偏的话题上。
       所以判据必须和"这一问拼不拼"的条件**是同一个**，否则两处会慢慢跑偏
       （改了一处、忘了另一处）。
    """
    return len(content) <= MAX_FOLLOWUP_CHARS and any(word in content for word in _PRONOUNS)


def _previous_user_question(history: Sequence[dict[str, str]], *, current: str) -> str:
    """最近一条**有实质内容**的用户问句。

    ⚠️ 跳过两类：
        ① 与本问**完全相同**的 —— `app/api/chat.py` 是**先把用户这一问落库、
           再读历史**的，所以 `history` 的最后一条往往就是本问自己。
           不跳过的话，会把它自己拼回去（"德国呢 德国呢"）。
        ② **本身就是追问**的（短 ＋ 指代词）—— 连着追问几句时**继续往前找**，
           直到碰上那句真正带话题的问句。这就是"连续短句累积"。

    📌 不这么做会怎样：「退货运费谁出」→「德国呢」→「那法国呢」，
       第三句只拼第二句的话，检索 query 就是「德国呢 那法国呢」——
       里面**一个政策词都没有**，等于没拼。

    ⚠️ 只累积**一条**（不是把中间那几句都串起来）：三句连拼会把 query 撑长，
       反而稀释召回；能对上文档的通常就是最早那句有实质内容的。
    """
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        content = (message.get("content") or "").strip()
        if not content or content == current or _is_thin(content):
            continue
        return content
    return ""


def previous_question(question: str, history: Sequence[dict[str, str]] = ()) -> str:
    """这次检索会并入的"上一轮问句"；**不拼就返回 `""`**。

    ⚠️ 给**界面**用：用户消息上那个 chip「已合并上一轮检索」要说清并入了什么。
       和 `retrieval_query()` **共用同一个判断** ⟹ chip 亮 ⟺ 检索真的被改写过，
       不会出现"界面说合并了、其实没合并"这种自欺。
    """
    text = (question or "").strip()
    if not text or len(text) > MAX_FOLLOWUP_CHARS:
        return ""
    if not any(word in text for word in _PRONOUNS):
        return ""
    return _previous_user_question(history, current=text)


def retrieval_query(question: str, history: Sequence[dict[str, str]] = ()) -> str:
    """要拿去**检索**的 query。不是追问 → 原样返回 `question`。

    ⚠️ 上一轮问句放**前面**、这一问放**后面**：检索按"整串像不像某段话"打分，
       「德国呢」里没有任何政策词，而「退货运费谁出」才是能和文档对上话的那半句。
    """
    text = (question or "").strip()
    previous = previous_question(text, history)
    if not previous:
        return text
    return f"{previous[:_MAX_PREVIOUS_CHARS]} {text}"


def is_followup(question: str, history: Sequence[dict[str, str]] = ()) -> bool:
    """这次是不是真的做了拼接（给 `Answer.extra` 记账 / badcase 归因用）。"""
    return retrieval_query(question, history) != (question or "").strip()


__all__ = ["MAX_FOLLOWUP_CHARS", "is_followup", "previous_question", "retrieval_query"]
