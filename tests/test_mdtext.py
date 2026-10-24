"""markdown 降级（`app/rag/mdtext.py`）—— Web 引用摘要与飞书卡片**共用一套规则**。

2026-10-25 走查 W6：Web 的「参考来源」里露出 `|站点|---|` 这种表格源码。
修法不是"在 Web 那边再写一份洗法"，而是把飞书那套规则抽出来共用 ——
两边各写一遍，迟早一边多处理一种形状、另一边没有。

⚠️ 判据统一是「**卡片上不许出现 markdown 源码**」，不是"表格要好看"：
   前端对摘要是**纯文本插值**（`{{ cite.snippet }}`），任何标记都会原样显示。
"""

from app.rag.mdtext import downgrade, snippet
from app.rag.prompt import make_snippet

CHUNK = """## 配送与时效

| 站点 | 时效 | 起算点 |
|---|---|---|
| 广州仓 | 次日达 | 付款时间 |
| 乌鲁木齐 | 5 天 | 发货时间 |

以上为**标准**时效。
"""


def test_block_table_becomes_key_value_lines() -> None:
    """块状表格 → `表头：值` 行；表头不再是单独一行，信息一条都不许丢。"""
    out = downgrade(CHUNK)

    assert "|" not in out
    assert "---" not in out
    assert "站点：广州仓｜时效：次日达｜起算点：付款时间" in out
    assert "乌鲁木齐" in out


def test_snippet_is_one_line_without_markdown_source() -> None:
    text = snippet(CHUNK)

    assert "\n" not in text
    assert "|" not in text and "---" not in text
    assert "**" not in text  # 纯文本插值：留着 `**` 就是露源码
    assert "广州仓" in text  # 内容不许被洗掉


def test_snippet_washes_inline_table_pressed_into_one_line() -> None:
    """语料里也见过整张表被压成一行 —— 它走不到块逻辑，靠兜底洗管道符。"""
    text = snippet("部件类别 | 期限 | |------|------| | 家具主体 | 2 年")

    assert "|" not in text and "---" not in text
    assert "家具主体" in text and "2 年" in text


def test_snippet_keeps_unpaired_underscores_and_asterisks() -> None:
    """单个 `_` / `*` 可能本来就是内容（字段名、乘法），不配对就不许洗。"""
    text = snippet("字段 content_status 与 2*3 的写法")

    assert "content_status" in text
    assert "2*3" in text


def test_snippet_truncates_with_ellipsis() -> None:
    assert make_snippet("字" * 500, limit=10).endswith("…")


def test_headings_are_plain_by_default_and_bold_for_feishu() -> None:
    """同一份规则、两种渠道：摘要要纯文字，卡片要加粗（`lark_md` 没有标题语法）。"""
    assert downgrade("## 保修期") == "保修期"
    assert downgrade("## 保修期", bold_headings=True) == "**保修期**"
