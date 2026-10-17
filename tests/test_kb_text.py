"""清洗 / 切分 / 后缀判定 —— **纯单元测试**（不连库、不花钱）。

⚠️ 这些断言是**重构的保险**：入库逻辑 2026-10-17 从 `scripts/kb/ingest.py`
    抽到 `app/kb/`（worker 要复用），切分行为**必须一个字都不变** ——
    变了就等于把 231 个块的语料重新切了一遍，而评测数字是拿老切法跑的。
"""

from __future__ import annotations

from app.kb.ingest import is_supported
from app.kb.text import chunk_bytes, chunk_text, clean_markdown, split_sections


def test_clean_markdown_strips_anchors_and_inline_marks() -> None:
    raw = "## 标题 { #some-anchor }\n\n**加粗** 和 [链接](https://example.com) 和 `代码`\n"
    cleaned = clean_markdown(raw)

    assert "{ #some-anchor }" not in cleaned
    assert "**" not in cleaned
    assert "https://example.com" not in cleaned  # 只留链接文字
    assert "链接" in cleaned
    assert "`" not in cleaned


def test_clean_markdown_keeps_code_fence_content() -> None:
    """代码块**原样保留**：`#` 是注释、`*` 是乘号，重写会改坏示例。"""
    raw = "说明\n\n```python\n# 这是注释\nx = a * b\n```\n"
    cleaned = clean_markdown(raw)

    assert "# 这是注释" in cleaned
    assert "x = a * b" in cleaned
    assert "```" not in cleaned  # 围栏标记本身去掉


def test_split_sections_tracks_heading_path_and_ignores_code_comments() -> None:
    raw = "# A\n\n正文一\n\n```bash\n# 不是标题\n```\n\n## B\n\n正文二\n"
    sections = split_sections(raw)

    paths = [section.heading_path for section in sections]
    assert paths == ["A", "A > B"]


def test_chunk_text_prefixes_heading_path() -> None:
    """每块前面都要带标题路径 —— 引用摘要靠它说明出处，检索也靠它定向。"""
    chunks = chunk_text("# 退换货\n\n无理由退货 7 天。\n", size=800, overlap=100)

    assert len(chunks) == 1
    assert chunks[0].startswith("[退换货]")


def test_chunk_text_on_empty_input_returns_nothing() -> None:
    assert chunk_text("", size=800, overlap=100) == []
    assert chunk_bytes(b"   \n\n", size=800, overlap=100) == []


def test_chunk_bytes_counts_match_size_budget() -> None:
    """一条长正文要被切成多块，且每块都不超过 `size`。"""
    body = "字" * 3000
    chunks = chunk_bytes(f"# 标题\n\n{body}\n".encode(), size=400, overlap=50)

    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in chunks)


def test_chunk_bytes_tolerates_non_utf8() -> None:
    """非 UTF-8 字节不该让整批入库停下（替换字符一眼能看出来，比静默丢内容好）。"""
    chunks = chunk_bytes(b"# T\n\nabc\xff\xfe def\n", size=800, overlap=100)

    assert len(chunks) == 1
    assert "abc" in chunks[0]


def test_is_supported_only_textual_suffixes() -> None:
    assert is_supported("政策.md")
    assert is_supported("政策.MARKDOWN")  # 后缀大小写不敏感
    assert is_supported("a.txt")
    # PDF / docx **故意不吃**：加进来就要多一类"解析出来是垃圾但没人发现"的静默失败
    assert not is_supported("说明书.pdf")
    assert not is_supported("报表.docx")
    assert not is_supported("没有后缀")
