"""文本清洗与切分 —— **纯函数**（不碰库、不碰网络、不碰配置）。

⚠️ 这里**只吃文本类**（`.md` / `.markdown` / `.txt`）。PDF / docx 要引入解析库，
    是另一个话题（v1.2），**别顺手加**：加了解析面就多一类"解析出来的文本是垃圾、
    但没人发现"的静默失败。

## 为什么必须清洗

`chunks.content` 会被两处消费：
  ① 喂给模型当上下文；② **直接当引用摘要显示在页面上**
     （`prompt.make_snippet()` 只截断，不做格式转换）。
不清洗的话，用户会在引用卡片里看到 `**FastAPI**`、`{ #dependencies }`、
`{* ../../docs_src/... *}` 这些东西 —— 前端也**不渲染 markdown**（全篇不用 `v-html`）。

## 为什么每块前面都带标题路径

光看"Let's first focus on the dependency." 根本不知道在讲什么；
加上 `[Dependencies > First Steps]` 之后，**向量和关键词都变得有指向**，
而且引用摘要自己就说明白了出处。这是白捡的检索质量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 能吃进来的后缀。**故意只留文本类** —— PDF/docx 要引入解析库，是 v1.2 的事。
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})


# 文本清洗
#: `## 标题 { #anchor }` —— mkdocs 的锚点，对读者和模型都是噪音
ANCHOR_RE = re.compile(r"\s*\{\s*#[^}]*\}\s*$")
#: `{* ../../docs_src/dependencies/tutorial001.py hl[8:9] *}` —— 代码**引用指令**
CODE_INCLUDE_RE = re.compile(r"^\{\*\s*([^\s*]+)[^*]*\*\}\s*$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
#: 终端输出示例里用来**上色**的标签，形如 `<span style="color: green;">INFO</span>`。
#: ⚠️ **这些要连代码块一起清**（用法见 `clean_markdown`）：
#:    它们不是代码，是文档站点渲染终端配色的痕迹。留着的话，引用摘要里会出现
#:    `<span style="color: green;">INFO</span>: Uvicorn running on ...` 这种东西。
PRESENTATION_TAG_RE = re.compile(r"</?(?:span|font|b|u)\b[^>]*>", re.IGNORECASE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
#: 斜体。`(?!\s)` 是为了**不误吃列表符号** —— `* 项目` 里的星号后面是空格。
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
BLANK_RUN_RE = re.compile(r"\n{3,}")


def clean_markdown(raw: str) -> str:
    """把 mkdocs 风格的 markdown 洗成**纯文本**。

    ⚠️ **只清洗代码块之外的行。** 代码里的 `#` 是注释、`[]` 是下标、`*` 是乘号 ——
        一律重写会把示例代码改坏。围栏内部**原样保留**。
    """
    out: list[str] = []
    in_fence = False

    for line in raw.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue  # 去掉围栏标记本身，代码内容留着
        if in_fence:
            # ⚠️ 代码块整体**原样保留**，但"上色标签"例外 —— 见 PRESENTATION_TAG_RE
            out.append(PRESENTATION_TAG_RE.sub("", line))
            continue

        stripped = line.strip()

        # `/// note | 标题` … `///` 是 mkdocs 的 admonition：标记行去掉、内容留着
        if stripped.startswith("///"):
            continue

        include = CODE_INCLUDE_RE.match(stripped)
        if include:
            # 代码引用指向的文件**没下载**，所以只留一句可读的提示。
            # 原样留着的话，引用摘要里会出现一串 `../../docs_src/... hl[8:9]`。
            out.append(f"[示例代码：{Path(include.group(1)).name}]")
            continue

        line = IMAGE_RE.sub("", line)
        line = LINK_RE.sub(r"\1", line)  # 只留链接文字，URL 是噪音
        line = HTML_TAG_RE.sub("", line)
        line = ANCHOR_RE.sub("", line)
        line = line.replace("`", "")
        line = BOLD_RE.sub(r"\1", line)
        line = ITALIC_RE.sub(r"\1", line)
        out.append(line.rstrip())

    return BLANK_RUN_RE.sub("\n\n", "\n".join(out)).strip()


# 切分
@dataclass(frozen=True)
class Section:
    """一个标题下的正文。`heading_path` 形如 `Dependencies > First Steps`。"""

    heading_path: str
    body: str


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def split_sections(text: str) -> list[Section]:
    """按 markdown 标题切段，并记住**标题路径**。

    ⚠️ 要跟踪代码围栏：代码块里的 `# 注释` 不是标题。
       不跟踪的话，Python 代码里的注释会把文档切得稀碎、标题层级也会乱掉。
    """
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (层级, 标题)
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = " > ".join(title for _, title in stack)
            sections.append(Section(heading_path=path, body=body))
        buffer.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            continue

        match = None if in_fence else HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue

        flush()
        level = len(match.group(1))
        title = match.group(2).strip()
        # 同级或更深的标题先弹出去，保证路径是"当前所在的那条链"
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

    flush()
    return sections


def _cut(text: str, *, lo: int, hi: int, forward: bool) -> int | None:
    """在 `[lo, hi)` 里找一个**切点**。

    ⚠️ 分两轮找，顺序很重要：
        ① 先找**换行** —— 段落边界，切在这里上下文最完整；
        ② 找不到再找**空格** —— 至少别把单词劈成两半。
    ⚠️ 为什么需要第 ② 轮：FastAPI 文档**一个自然段就是一行**，常常超过 100 字。
        只认换行的话，`[lo, hi)` 里一个换行都没有，就只能退回"按字符硬切"，
        于是出现 `me package that this module...` 这种**从单词中间开始**的块。
    """
    for char in ("\n", " "):
        index = text.find(char, lo, hi) if forward else text.rfind(char, lo, hi)
        if index != -1:
            return index
    return None


def _windows(text: str, *, size: int, overlap: int) -> list[str]:
    """按字符窗口切，并**尽量在段落/词边界断开**，避免把句子、单词或代码从中间劈开。"""
    if size <= 0:
        raise ValueError("size 必须为正")

    out: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(length, start + size)
        if end < length:
            # 只在窗口后半段找切点：太靠前会导致每块都很短
            cut = _cut(text, lo=start + size // 2, hi=end, forward=False)
            if cut is not None and cut > start:
                end = cut

        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= length:
            break

        # ⚠️ 重叠窗口的**起点**同样要对齐边界。否则下一块会从半句话 / 半个词中间开始 ——
        #    实测见过 `it that is an int, and by default is 100.` 和
        #    `me package that this module ...` 这两种残块开头，
        #    读起来像文本损坏，喂给模型也是垃圾上下文。
        next_start = end - overlap
        if next_start > start:
            cut = _cut(text, lo=next_start, hi=end, forward=True)
            if cut is not None:
                next_start = cut + 1

        # ⚠️ 必须保证**真的前进**，否则窗口原地打转 -> 死循环
        start = next_start if next_start > start else end

    return out


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """切成检索用的块（每块带标题路径前缀）。"""
    chunks: list[str] = []

    for section in split_sections(text):
        prefix = f"[{section.heading_path}]\n" if section.heading_path else ""
        body = section.body.strip()
        if not body:
            continue

        budget = size - len(prefix)
        if budget <= 0:  # 标题路径自己就超长（极深嵌套），退化成不带前缀
            chunks.extend(_windows(body, size=size, overlap=overlap))
            continue

        if len(body) <= budget:
            chunks.append(f"{prefix}{body}".strip())
            continue

        chunks.extend(
            f"{prefix}{piece}".strip() for piece in _windows(body, size=budget, overlap=overlap)
        )

    return [chunk for chunk in chunks if chunk]


def chunk_bytes(data: bytes, *, size: int, overlap: int) -> list[str]:
    """字节 → 文本 → 块。**入库的唯一入口**（CLI 与 worker 共用同一条路）。

    ⚠️ `errors="replace"`：遇到非 UTF-8 的文件不要抛异常，而是**留下替换字符继续**。
        原因是入库是批量的，一个编码坏掉的文件不该让整批停下；而替换字符（U+FFFD）
        在引用摘要里一眼就能看出来，**比静默丢内容好**。
    """
    return chunk_text(data.decode("utf-8", errors="replace"), size=size, overlap=overlap)


def chunk_file(path: Path, *, size: int, overlap: int) -> list[str]:
    """读文件 → 清洗 → 切分。**同步函数**。

    ⚠️ 同步是刻意的：`path.read_bytes()` 是阻塞 I/O，在协程里直接跑会卡住事件循环
        （ruff 的 `ASYNC240` 拦的就是这个）。调用方要自己决定放不放 `to_thread`。
    """
    return chunk_bytes(path.read_bytes(), size=size, overlap=overlap)
