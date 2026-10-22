"""闭卷模式（`RagPipeline(closed_book=True)`）：只给评测用的对照组。

为什么值得单独测：

    闭卷**不是一条线上路径**，它只活在评测里。一旦它悄悄退化成"照样检索"，
    对照实验的结论就会变成"开卷和闭卷一样好"——**差值消失，而报表照样好看**。
    这是典型的静默失效，必须用断言钉住：

        1. 闭卷**一次都不许**碰检索（不给它 session，碰了就会炸）
        2. 闭卷**一次都不许**调 embedding / rerank（给的是"一调就抛"的假客户端）
        3. 闭卷不发拒答（没有证据就无从判"证据够不够"）
        4. 闭卷的 system 里**没有**【参考资料】那一段

另外钉一条**反向**的：默认（开卷）路径仍然走检索、仍然把证据塞进提示词 ——
否则"闭卷没检索"这个断言在"两条路都没检索"时也会通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.rag import prompt as prompt_module
from app.rag.pipeline import Answer, RagPipeline, Sources
from app.rag.providers import ModelAPIError
from app.rag.retrieval import RetrievedChunk


class FakeChat:
    """记录收到的 messages，并回一段固定文本（顺便回报 usage）。"""

    def __init__(self, pieces: Sequence[str] = ("七", "天")) -> None:
        self.pieces = pieces
        self.calls = 0
        self.seen: list[list[dict[str, str]]] = []

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        usage: dict[str, int] | None = None,
    ) -> AsyncIterator[str]:
        self.calls += 1
        self.seen.append([dict(item) for item in messages])
        if usage is not None:
            usage.update({"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        for piece in self.pieces:
            yield piece


class ExplosiveModels:
    """embedding / rerank 的"一调就炸"替身。闭卷永远不该碰它们。"""

    model = "should-not-be-called"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("闭卷不许调 embedding")

    async def rerank(self, *args: Any, **kwargs: Any) -> list[tuple[int, float]]:
        raise AssertionError("闭卷不许调 rerank")


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ordinal=0,
        content="七天无理由退货，自签收之日起算。",
        score=0.95,
    )


async def _fake_retrieve(
    question: str, *, ref_time: object = None
) -> tuple[list[RetrievedChunk], float, str]:
    """开卷对照用的固定检索结果（不连库）。

    ⚠️ `ref_time` 这个关键字**必须留着**：`RagPipeline.answer` 会把它传下来
        （v1.3 B2 的时间基准）。签名对不上 = 整个开卷对照组 TypeError，
        而报错信息看起来像"测试桩写错了"，很容易被当成无关噪音。
    """
    return [_chunk()], 0.95, "rerank"


def _closed(chat: FakeChat) -> RagPipeline:
    """闭卷 pipeline。⚠️ `session=None` 是刻意的：碰了检索就会 AttributeError。"""
    return RagPipeline(
        session=None,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        chat=chat,  # type: ignore[arg-type]
        embedder=ExplosiveModels(),  # type: ignore[arg-type]
        reranker=ExplosiveModels(),  # type: ignore[arg-type]
        closed_book=True,
    )


async def _drain(pipeline: RagPipeline) -> tuple[Sources, Answer, list[str]]:
    sources: Sources | None = None
    final: Answer | None = None
    deltas: list[str] = []
    async for event in pipeline.answer(question="退货有多少天？"):
        if isinstance(event, Sources):
            sources = event
        elif isinstance(event, Answer):
            final = event
        else:
            deltas.append(event.text)
    assert sources is not None, "没有发出 Sources 事件"
    assert final is not None, "没有发出 Answer 事件"
    return sources, final, deltas


async def test_closed_book_skips_retrieval_and_generation_of_citations() -> None:
    chat = FakeChat()
    sources, final, deltas = await _drain(_closed(chat))

    # 没有证据 → 没有引用、没有置信度、不拒答
    assert sources.citations == []
    assert sources.confidence is None
    assert sources.refused is False
    assert final.citations == []
    assert final.confidence is None
    assert final.refused is False
    assert final.extra.get("score_kind") == "closed_book"

    # 但回答本身照常流式产出
    assert "".join(deltas) == "七天"
    assert final.text == "七天"


async def test_closed_book_calls_chat_exactly_once_and_never_touches_models() -> None:
    chat = FakeChat()
    _, final, _ = await _drain(_closed(chat))

    # 一次检索都没做 = embedding / rerank 一次没调；ExplosiveModels 没炸就是证明
    assert chat.calls == 1
    # usage 必须被收集（成本计量在对照组里也要能算）
    assert final.usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


async def test_closed_book_prompt_has_no_evidence_block() -> None:
    chat = FakeChat()
    await _drain(_closed(chat))

    system = chat.seen[0][0]
    assert system["role"] == "system"
    assert "【参考资料】" not in system["content"]
    # 开卷那份提示词的第一条是"只依据参考资料"——闭卷不能带着它，
    # 否则模型会照着提示词把每道题都拒掉，测出来的就不是"参数化记忆"
    assert "只依据【参考资料】作答" not in system["content"]
    # 语言要求两边必须一致，否则差值里会混进"提示词风格"
    assert "相同的语言" in system["content"]


async def test_open_book_still_uses_evidence_and_cites() -> None:
    """反向守卫：默认路径不许被"闭卷化"。"""
    chat = FakeChat()
    pipeline = RagPipeline(
        session=None,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        chat=chat,  # type: ignore[arg-type]
        closed_book=False,
    )
    pipeline._retrieve = _fake_retrieve  # type: ignore[method-assign]

    sources, final, _ = await _drain(pipeline)

    assert len(sources.citations) == 1
    assert sources.confidence == 0.95
    assert final.refused is False
    assert final.extra.get("score_kind") == "rerank"
    # 证据确实进了提示词
    system = chat.seen[0][0]["content"]
    assert "【参考资料】" in system
    assert "七天无理由退货" in system


async def test_closed_book_has_no_refusal_even_with_empty_knowledge_base() -> None:
    """一个"库里啥也没有"的闭卷请求：**照样答**（对照组要量的是它会不会编）。

    如果哪天有人把拒答也接进闭卷，这条会红 —— 那时对照组会恒为 0 分，
    看起来"RAG 增益极大"，其实是尺子坏了。
    """
    chat = FakeChat(pieces=("不知道。",))
    sources, final, _ = await _drain(_closed(chat))
    assert sources.refused is False
    assert final.refused is False
    assert final.text == "不知道。"


async def test_closed_book_keeps_partial_text_when_model_fails() -> None:
    """模型中途挂掉：已经拿到的文字**不能丢**，错误要如实记录（与开卷一致）。"""

    class HalfBrokenChat(FakeChat):
        async def stream(
            self,
            messages: Sequence[dict[str, str]],
            usage: dict[str, int] | None = None,
        ) -> AsyncIterator[str]:
            self.calls += 1
            yield "七天"
            raise ModelAPIError("模拟中断")

    _, final, deltas = await _drain(_closed(HalfBrokenChat()))
    assert deltas == ["七天"]
    assert final.text == "七天"
    assert final.error is not None


def test_closed_book_messages_keep_history_order() -> None:
    """历史拼法必须与开卷一致（只有 system 不同）。"""
    messages = prompt_module.build_closed_book_messages(
        question="那德国呢？",
        history=[
            {"role": "user", "content": "退货多少天？"},
            {"role": "assistant", "content": "7 天。"},
        ],
    )
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"] == "那德国呢？"
    assert messages[0]["content"] == prompt_module.CLOSED_BOOK_SYSTEM_PROMPT
