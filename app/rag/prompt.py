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


#: **闭卷**（关掉检索）时用的提示词。只用于评测的对照组，线上不走这里。
#:
#: ⚠️ **为什么不能复用 `SYSTEM_PROMPT`、把【参考资料】填成"（无）"**：
#:    那份提示词的第 1、3 条写着"只依据参考资料""资料不足就说资料不足" ——
#:    于是模型会**照着提示词把每道题都拒掉**，测出来的是"提示词服从性"，
#:    而不是"模型自己记住了多少"。闭卷要测的是**参数化记忆**（model prior），
#:    所以必须给它一份允许动用先验知识的提示词。
#:
#: ⚠️ **偏差方向要写清楚（诚实优先）**：第 2 条允许它在不确定时给出最佳猜测 ——
#:    这确实把闭卷推向"更容易编"。但这不是抹黑对手：一个不用 RAG 的模型，
#:    在真实客服场景里就是这么表现的（它不会因为不知道就闭嘴）。
#:    想量"不许猜"的极限，那是另一组实验（结果恒为"全部拒答"，没有信息量）。
#:
#: 📌 与开卷提示词**只在"有没有证据"这一件事上不同**：语言、简洁分点、不提内部词汇
#:    这三条要求两边一模一样，否则差值里会混进"提示词风格"的成分。
CLOSED_BOOK_SYSTEM_PROMPT = """你是一个企业客服问答助手。

回答规则：
1. 直接回答用户的问题；不要提及"参考资料""上下文""检索"这类内部词汇。
2. 如果你不确定，可以说明不确定，但请给出你认为最可能的回答。
3. 回答使用与提问相同的语言，简洁、分点、可直接执行。
"""


#: **读图**用的提示词：一次调用把图变成四个结构化字段。
#:
#: ⚠️ **为什么要求 JSON 而不是"自由描述"**：自由描述没法做三件事 ——
#:    ① 可测（评测要断言"图里的单号被读到了"）② 可分流（`transcription` 只喂生成，
#:    `scene` 才进检索）③ 可降级（解析失败时能识别出来并退回整段）。
#:
#: ⚠️ **为什么同一套提示词要同时覆盖"照片"和"文书"**：输入差异极大 ——
#:    破损纸箱的照片只有 `scene`，订单截图只有 `transcription`。
#:    只按一类图写提示词，另一类会被硬套成"描述一张图"。
#:
#: ⚠️ `has_exact_fields` **宁可误报**：它只决定"要不要去查一下订单表"，
#:    多查一次是免费的（一次 SQL）；漏报会让"我的单呢"答不上来。
IMAGE_ANALYSIS_PROMPT = """你把一张用户上传的图片转成结构化字段，供后续检索使用。

只输出**一个 JSON 对象**（不要 markdown 代码块、不要任何解释），字段固定为：
{
  "image_type": "screenshot | document | photo | handwritten | chart | other",
  "scene": "图中物体、部位、状态或版式的一句话描述",
  "transcription": "图中有业务含义的文字",
  "has_exact_fields": true 或 false
}

判断规则：
1. `image_type` 按图的性质选一个：截图 / 文书单据 / 实拍照片 / 手写 / 图表 / 其它。
2. `scene` 只描述图里真实存在的东西，不推测、不补充、不评价。
   纯文字截图也要描述版式（例如"白底页面，顶部一行订单号与金额"）。
3. `transcription` 只转写有业务含义的文字：单号、金额、日期、商品名、报错原文、
   条款关键句；忽略水印、页码、APP 导航栏、店名、按钮文案。
   文字太多就只摘这些关键字段，其余用"…"省略。
4. `has_exact_fields`：图里出现 6 位以上连续数字、条形码、或形如 SF10001 的单号时为 true。
   不确定就给 true（宁可多查一次，也不要漏掉）。
5. 没有可读文字的图（纯场景照片）：`transcription` 给空字符串。
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


def build_closed_book_messages(
    *,
    question: str,
    history: Sequence[dict[str, str]] = (),
) -> list[dict[str, str]]:
    """组装**闭卷**请求的 messages（没有检索、没有引用）。

    与 `build_messages` 的唯一差别是 system 里没有【参考资料】那一段 ——
    历史与用户消息的拼法保持一致，这样两组对照的差异只剩"有没有证据"。

    ⚠️ 闭卷不存在引用，所以提示词里也**没有**"标注 `[n]`"这条要求：
        给了编号规则却没有编号可用，模型会开始**编编号**，判分时又得额外过滤。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": CLOSED_BOOK_SYSTEM_PROMPT}]
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
