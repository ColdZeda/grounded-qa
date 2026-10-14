"""提示词组装与拒答文案。

⚠️ **提示词写在代码里，不落数据库。**
    它是**行为规范**，应该走 code review + 版本控制，而不是"某天有人在后台改了一句话，
    全站回答风格就变了"。要按租户定制提示词是另一个功能（v1.2 之后再说）。

⚠️ 这个模块的核心目标只有一个：**把模型按在证据上。**
    幻觉治理的第一道防线不是"更聪明的模型"，而是
    「只给它这几段原文 + 明确告诉它答不了就说答不了 + 让它标出出处」。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.rag.retrieval import RetrievedChunk

#: 证据不足时的固定回答。
#:
#: ⚠️ **这句是写死的，不经过模型。** 让 LLM 来"决定要不要拒答"是不可靠的 ——
#:    它天然倾向于给一个听起来合理的答案。拒答必须由**确定性代码**在**调用模型之前**决定
#:    （见 `pipeline.py`），这样"拒答率"才是一个能被信、能被优化的指标。
REFUSAL_ANSWER = (
    "抱歉，我在现有知识库里没有找到足够依据来回答这个问题。\n\n"
    "为避免给出不准确的信息，我不做推测。你可以：\n"
    "· 换一种问法，或补充更具体的条件；\n"
    "· 联系人工客服进一步确认。"
)

SYSTEM_PROMPT = """你是一个严谨的企业知识库问答助手。

回答规则（必须严格遵守）：
1. **只依据【参考资料】作答。** 不允许使用你自己的先验知识补充事实。
2. 每个事实性陈述后面标注来源编号，格式如 `[1]`、`[2]`；一句话里用了多段资料就标多个。
3. 如果【参考资料】不足以回答问题，**直接说明资料不足**，不要猜测、不要编造。
4. 不要提及"参考资料""上下文"这类内部词汇，直接回答用户的问题。
5. 回答使用与提问相同的语言，简洁、分点、可直接执行。

【参考资料】
{context}
"""


def build_context(hits: Sequence[RetrievedChunk], *, max_chars_per_chunk: int = 1200) -> str:
    """把命中的 chunk 拼成带编号的参考资料。

    ⚠️ **编号从 1 开始，且必须与 `citations` 的顺序一致** —— 模型回答里写的 `[2]`
        要能被前端映射回真实的 chunk。编号错位是"引用溯源"最隐蔽的失败方式：
        界面看起来有引用，点进去却是另一段文字。

    ⚠️ 单条截断（`max_chars_per_chunk`）：chunk 本身已经切过，但极端情况下（表格、
        代码块）可能很长。不截断会让 prompt 爆掉，而**爆掉的表现是模型截断/答非所问**，
        不是报错 —— 很难查。
    """
    if not hits:
        return "（无）"

    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        text = hit.content.strip()
        if len(text) > max_chars_per_chunk:
            text = text[:max_chars_per_chunk] + "…（已截断）"
        blocks.append(f"[{index}] {text}")
    return "\n\n".join(blocks)


def build_messages(
    *,
    question: str,
    hits: Sequence[RetrievedChunk],
    history: Sequence[dict[str, str]] = (),
) -> list[dict[str, str]]:
    """组装发给对话模型的 messages。

    ⚠️ **历史只带最近几轮**（由调用方裁剪）。不带历史 → 多轮追问（"那它的价格呢？"）
        完全无法回答；带太多 → 每次请求的 token 成本线性上涨，且**离题的旧上下文
        会把模型带偏**。
    """
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(context=build_context(hits)),
        }
    ]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append({"role": "user", "content": question})
    return messages


def make_snippet(text: str, *, limit: int = 160) -> str:
    """给引用卡片用的短摘要。

    ⚠️ 只截断、不做"智能摘要" —— 引用卡片的用途是**让人能核对原文**，
        任何再加工都可能引入与原文不符的表述，那就本末倒置了。
    """
    stripped = " ".join(text.split())
    return stripped if len(stripped) <= limit else stripped[:limit] + "…"
