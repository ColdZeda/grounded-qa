"""markdown → 纯文本的**降级规则**（Web 引用摘要 与 飞书卡片共用同一份）。

## 为什么要有这个文件

同一套「表格 / 标题 / 分割线」的洗法原本只写在 `app/channels/feishu.py` 里 ——
因为**飞书的 `lark_md` 不认表格**，不洗就会在群里露出 `|---|---|`。

但 Web 端「参考来源」的摘要也取自 chunk 原文，而知识库里的订单表本身就是
markdown 表格 ⟹ 引用卡片上露出一串 `|站点|---|` 这种源码（走查 W6 记的就是它）。
于是同一件事有了第二个需要它的地方。

**规则必须只有一份**：两边各写一遍洗法，迟早一边加了"无表头的表格"处理、
另一边没加，然后同一份语料在两个渠道长得不一样，还没人说得清哪个对。

## 三处差异（用参数表达，不是复制一份）

| 差异 | 飞书 | Web 摘要 |
|---|---|---|
| `# 标题` | 降级成 `**加粗**`（卡片没有标题语法） | 去掉 `#`，只留文字 |
| 表格行 | 每行前面加 `- `（列表在卡片里更好读） | 不加，摘要本来就是一行 |
| 单独的 `[3]` | 并回上一行（飞书里点不了，得说明"依据在这句"） | 不处理（Web 端是结构化引用，前端自己渲染） |

⚠️ **一条信息都不许丢**：表格宁可降级得难看，也不能把某一列静默删掉 ——
   摘要是给人**核对原文**用的，少一列就等于换了意思。
"""

from __future__ import annotations

import re

#: 表格的**分隔行**（`|---|---|` / `|:--:|--|`）：只由 `|` `-` `:` 和空格组成。
TABLE_SEP = re.compile(r"^[\s|:\-]+$")

#: `---` / `***` / `___` 独立成行 = 分割线。`lark_md` 不认，会原样露出。
HRULE = re.compile(r"^ {0,3}([-*_])\s*(?:\1\s*){2,}$")

#: `## 标题`：卡片里没有标题语法，降级成整行加粗。
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*$")

#: 整行只有一个引用标记 `[3]`。
LONE_MARKER = re.compile(r"^\s*\[(\d{1,2})\]\s*$")

#: 兜底用：**行内**的分隔行片段（`|------|` / `|:--:|`）。块状表格由 `downgrade()` 处理，
#: 但语料里也见过整张表被压成一行（`a | b | |---|---| | c | d`），那种不走块逻辑。
_INLINE_SEP = re.compile(r"\|?\s*:?-{2,}:?\s*\|?")

#: 连续的全角竖线挤成一个（行内表格降级后常见 `｜｜`）。
_MULTI_PIPE = re.compile(r"(?:｜\s*)+")

#: 行内强调 / 行内代码 / 删除线：摘要卡是**纯文本**（前端 `{{ snippet }}`），
#: 留着 `**` 和反引号就是又一种"露源码"。**只配对才洗** —— 单个 `*` `_`
#: 可能本来就是内容（`snake_case`、`2*3`），洗了反而改坏原文。
_EMPHASIS = re.compile(
    r"\*\*(?P<strong>.+?)\*\*|__(?P<strong2>.+?)__|`(?P<code>[^`]+)`|~~(?P<del>.+?)~~"
)

#: `[文字](url)` → `文字`：摘要里放不下 URL，也不该让人看一坨链接源码。
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

#: **行内**引用标记 `[3]` / `[12]`（顾客侧没有来源区时，它无所指）。
#: ⚠️ 只认纯数字 —— `[多件或需拆旧]` / `[文字](链接)` 都不是这个形状，不能被误删。
_INLINE_MARKER = re.compile(r"[ \t]*\[(\d{1,3})\]")

#: 删掉标记后可能留下的"空格 + 标点"（`撤回 。`）。
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([，。；：、！？）」』])")


def _strip_inline(text: str) -> str:
    """洗掉行内标记但**保留文字本身**。"""
    text = _LINK.sub(r"\1", text)

    def pick(match: re.Match[str]) -> str:
        return next(group for group in match.groups() if group is not None)

    return _EMPHASIS.sub(pick, text)


def table_rows_to_lines(rows: list[list[str]], *, has_sep: bool) -> list[str]:
    """`| a | b |` 的表格 → 一行一条的 `key：value` 文本（**不带项目符号**）。

    两列是最常见的形状（`部件类别 | 保修期限`），直接写成 `key：value`；
    多列就把表头带上，写成 `key1：v1｜key2：v2`。
    项目符号 / 行间连接符留给调用方 —— 飞书要 `- `，摘要要空格。
    """
    if has_sep and len(rows) >= 2:
        header, body = rows[0], rows[1:]
    else:  # 没有分隔行 ⇒ 认不出哪行是表头，全部当数据（别拿第一行冒险）
        header, body = [], rows

    lines: list[str] = []
    for row in body:
        if len(header) == 2 and len(row) >= 2:
            lines.append(f"{row[0]}：{row[1]}")
        elif header:
            cells = [f"{header[i]}：{row[i]}" for i in range(min(len(header), len(row))) if row[i]]
            lines.append("｜".join(cells))
        else:
            lines.append("｜".join(cell for cell in row if cell))
    return lines


def downgrade(
    text: str,
    *,
    bold_headings: bool = False,
    list_tables: bool = False,
    merge_lone_citations: bool = False,
) -> str:
    """把 markdown 降级成"一行一段"的纯文本。

    参数就是上表那三处渠道差异，**默认值 = Web 摘要要的样子**（最朴素的那个）。
    """
    out: list[str] = []
    table: list[list[str]] = []
    has_sep = False

    def flush() -> None:
        nonlocal has_sep
        if table:
            prefix = "- " if list_tables else ""
            out.extend(prefix + line for line in table_rows_to_lines(table, has_sep=has_sep))
            table.clear()
        has_sep = False

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # 表格：连续的 `|` 行收集起来**整块**转换（表头只在第一行）
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if TABLE_SEP.match(stripped):
                has_sep = True
            else:
                table.append([cell.strip() for cell in stripped.strip("|").split("|")])
            continue
        flush()

        if not stripped:
            out.append("")
            continue
        if HRULE.match(line):
            out.append("")  # 换成空行：一句 "---" 在卡片/摘要里都没有信息量
            continue
        heading = HEADING.match(line)
        if heading:
            title = heading.group(1)
            if not title:
                out.append("")
            else:
                out.append(f"**{title}**" if bold_headings else title)
            continue
        marker = LONE_MARKER.match(line) if merge_lone_citations else None
        if marker:
            # 模型常把 `[3]` 单独甩一行（前面还空了一行）；并回上一行才是"这段的依据在 3"
            while out and not out[-1].strip():
                out.pop()
            if out:
                out[-1] = f"{out[-1].rstrip()} [{marker.group(1)}]"
                continue
        out.append(line)

    flush()
    return "\n".join(out).strip()


def strip_citation_markers(text: str, *, keep: frozenset[int] | set[int] | None = None) -> str:
    """删掉正文里的**行内**引用编号 `[3]`（顾客侧专用）。

    ## 为什么顾客侧要删

    2026-10-28 走查（A12）：飞书里已经**不再附"参考来源"**（顾客不需要知道知识库里有哪几篇），
    但正文里的 `[1]` `[2]` 还留着 ⟹ 那个数字**无所指**，顾客只会觉得"这是什么意思"。
    要么给来源，要么删编号 —— 中间态是最差的。

    ## 只删"真的是引用"的那些

    * 只认**纯数字**：`[多件或需拆旧]`、`[文字](链接)` 不动；
    * 给了 `keep` 就只删**里面这些编号**（= 本次真的引用到的），
      这样正文里万一有个 `[7]` 而它并不是引用，也不会被误删；
    * 删完收拾一下留下的"空格 + 标点"（`撤回 。` → `撤回。`）。

    ⚠️ **不碰 Web 端**：那边的编号能点开、能核对原文，删了等于把证据删了。
    """
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if keep is not None and index not in keep:
            return match.group(0)
        return ""

    cleaned = _INLINE_MARKER.sub(replace, text)
    if cleaned == text:
        return text
    return _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)


def snippet(text: str, *, limit: int = 160) -> str:
    """把一段 chunk 原文压成**一行**摘要（引用卡片用）。

    三步，缺一不可：
    ① `downgrade()`：块状表格 → `表头：值`、`#` 去掉、`---` 丢掉；
    ② 压成一行（`" ".join(split())`）—— 摘要卡片只给一行；
    ③ **兜底洗源码**：行内被压平的表格（`a | b | |---|---|`）不是块，走不到 ①，
       但露出的 `|` 和 `|---|` 一样是"源码"；行内 `**加粗**` / 反引号同理
       （前端是纯文本插值，留着就是原样显示）。

    ⚠️ 只截断、不做"智能摘要" —— 摘要的用途是**让人能核对原文**，
       任何再加工都可能引入与原文不符的表述，那就本末倒置了。
    """
    flat = " ".join(downgrade(text).split())
    flat = _INLINE_SEP.sub("｜", flat)
    flat = flat.replace("|", "｜")
    flat = _MULTI_PIPE.sub("｜", flat)
    flat = _strip_inline(flat)
    flat = " ".join(flat.split()).strip("｜ ")
    return flat if len(flat) <= limit else flat[:limit] + "…"
