"""飞书渠道的**纯逻辑**测试。

⚠️ 这里**不连飞书、不装 `lark-oapi`、不连数据库**：
   解析、去重、切分、渲染都是纯函数，测试就该只测这些。
   `_answer` 那条链路要连库，属于集成测试的范围（和 `test_chat_image.py` 一样，
   库里没有业务表时会被 conftest 跳过）。
"""

from __future__ import annotations

import pytest

from app.channels.feishu import (
    MAX_REPLY_CHARS,
    FeishuChannel,
    FeishuEvent,
    parse_event,
    render_reply,
    split_reply,
    strip_mentions,
    to_feishu_markdown,
)
from app.rag.pipeline import Answer, Citation


def _payload(
    *,
    text: str = "保修多久",
    message_type: str = "text",
    sender_type: str = "user",
    chat_type: str = "p2p",
    content: str | None = None,
) -> dict:
    """造一条 `im.message.receive_v1` 事件（形状照飞书 v2 回调抄的）。"""
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": sender_type, "sender_id": {"open_id": "ou_test"}},
            "message": {
                "message_id": "om_test",
                "chat_id": "oc_test",
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content if content is not None else f'{{"text":"{text}"}}',
            },
        },
    }


def test_parse_text_message() -> None:
    event = parse_event(_payload(text="保修多久"))
    assert event is not None
    assert event.text == "保修多久"
    assert event.chat_id == "oc_test"
    assert event.is_group is False


def test_parse_strips_at_mention_in_group() -> None:
    """群聊里 `@机器人` 会变成 `@_user_1` 留在正文开头。

    ⚠️ 不去掉的话它会进**检索 query** —— trgm 和向量都会把它当噪声。
    """
    event = parse_event(_payload(text="@_user_1 保修多久", chat_type="group"))
    assert event is not None
    assert event.text == "保修多久"
    assert event.is_group is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"message_type": "image"},  # 非文本：v1.2 不做飞书侧多模态
        {"sender_type": "bot"},  # 别的机器人：不回，否则会互相刷
        {"text": ""},  # 空正文：没什么可答的
    ],
)
def test_parse_ignores(kwargs: dict) -> None:
    assert parse_event(_payload(**kwargs)) is None


def test_parse_tolerates_broken_content_json() -> None:
    """`content` 是 JSON 字符串。解析失败时**当纯文本用** —— 丢消息比多问一次更糟。"""
    event = parse_event(_payload(content="保修多久"))
    assert event is not None
    assert event.text == "保修多久"


def test_parse_tolerates_missing_event() -> None:
    assert parse_event({}) is None
    assert parse_event({"event": {}}) is None


def test_strip_mentions_multiple() -> None:
    assert strip_mentions("@_user_1 @_user_2 你好") == "你好"
    assert strip_mentions("你好") == "你好"


def test_split_reply_short_is_single_chunk() -> None:
    assert split_reply("短答案") == ["短答案"]


def test_split_reply_never_loses_text() -> None:
    """长回复按段落切。**不许截断**：把切出来的再拼回去，应该和原文等价
    （只允许空白差异 —— 切点会吃掉边界上的空行）。"""
    paragraph = "这是一段很长的回答内容，用来把回复撑过上限。" * 6
    # 30 段 × ≈138 字 ≈ 4100 字，确实超过 MAX_REPLY_CHARS（3500）
    text = "\n\n".join([paragraph] * 30)
    chunks = split_reply(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_REPLY_CHARS for chunk in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_render_reply_appends_sources_with_metrics() -> None:
    """来源必须带**分数口径**（精排分 / 余弦）—— 同一个数字不同含义。"""
    answer = Answer(
        text="家具主体保修 2 年。",
        refused=False,
        citations=[
            Citation(
                index=1,
                chunk_id="c1",
                document_id="d1",
                score=0.98,
                score_kind="rerank",
                snippet="家具主体保修期为 24 个月",
            )
        ],
        confidence=0.98,
        latency_ms=1200,
    )

    rendered = render_reply(answer, show_sources=True)

    assert rendered.startswith("家具主体保修 2 年。")
    assert "[1]" in rendered
    assert "rerank 0.980" in rendered


def test_render_reply_can_hide_sources() -> None:
    answer = Answer(
        text="家具主体保修 2 年。",
        refused=False,
        citations=[],
        confidence=None,
        latency_ms=10,
    )
    assert render_reply(answer, show_sources=True) == "家具主体保修 2 年。"
    assert render_reply(answer, show_sources=False) == "家具主体保修 2 年。"


def test_to_feishu_markdown_downgrades_table_to_bullets() -> None:
    """飞书卡片**不支持表格**。不降级的话群里看到的是 `|---|---|` 源码
    （2026-10-22 真机联调实测）。

    ⚠️ 降级只许换形状，**信息一条都不能丢** —— 表头 + 每个单元格都要在场。
    """
    text = (
        "保修期：\n\n"
        "| 部件类别 | 保修期限 |\n"
        "|---|---|\n"
        "| 家具主体（床架、书桌） | **2 年** |\n"
        "| 电子件（电机） | 1 年 |\n"
    )

    out = to_feishu_markdown(text)

    assert "|" not in out  # 一个管道符都不许留下
    assert "---" not in out
    assert "- 家具主体（床架、书桌）：**2 年**" in out
    assert "- 电子件（电机）：1 年" in out
    assert "部件类别" not in out.split("- 家具主体")[0]  # 表头被吃进 key-value，不再单列


def test_to_feishu_markdown_table_without_separator_keeps_every_cell() -> None:
    """没有 `|---|` 分隔行时认不出表头 —— 那就**全部当数据**，不许把第一行当表头吃掉。"""
    out = to_feishu_markdown("| 甲 | 乙 |\n| 丙 | 丁 |")

    assert "甲" in out and "乙" in out
    assert "丙" in out and "丁" in out


def test_to_feishu_markdown_downgrades_heading_and_rule() -> None:
    """`#` 标题和 `---` 分割线 `lark_md` 都不认 → 标题加粗、分割线换空行。"""
    out = to_feishu_markdown("## 保修期\n\n正文\n\n---\n\n结尾")

    assert "##" not in out
    assert "**保修期**" in out
    assert "---" not in out
    assert "正文" in out and "结尾" in out


def test_to_feishu_markdown_joins_lone_citation_marker() -> None:
    """模型常把 `[3]` 单独甩一行 —— 并回上一行，否则群里孤零零一个数字很奇怪。"""
    out = to_feishu_markdown("家具主体 **2 年**，该版本已废止。\n\n[3]\n\n下一段")

    assert "[3]" in out
    assert "[3]" not in out.splitlines()  # 没有任何一行**只有** [3]
    assert "该版本已废止。 [3]" in out


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_markdown(self, chat_id: str, markdown: str) -> None:
        self.sent.append((chat_id, markdown))


class _FakeChat:
    """占位：`handle_event` 在 `_answer` 被替换后不会碰它。"""


def _channel(sender: _FakeSender) -> FeishuChannel:
    return FeishuChannel(sender=sender, chat=_FakeChat())  # type: ignore[arg-type]


async def test_handle_event_answers_once_per_message_id() -> None:
    """飞书会重投事件；同一个 `message_id` **只答一次**。

    ⚠️ 不挡的话：同一个问题答两遍，而且库里会留两条一模一样的消息。
    """
    sender = _FakeSender()
    channel = _channel(sender)
    calls: list[FeishuEvent] = []

    async def fake_answer(event: FeishuEvent) -> str:
        calls.append(event)
        return "答案"

    channel._answer = fake_answer  # type: ignore[method-assign]

    payload = _payload(text="保修多久")
    await channel.handle_event(payload)
    await channel.handle_event(payload)

    assert len(calls) == 1
    assert sender.sent == [("oc_test", "答案")]


async def test_handle_event_survives_pipeline_failure() -> None:
    """生成炸了也要**回一句人话** —— 否则用户只看到"机器人不理我"。"""
    sender = _FakeSender()
    channel = _channel(sender)

    async def boom(event: FeishuEvent) -> str:
        raise RuntimeError("模型挂了")

    channel._answer = boom  # type: ignore[method-assign]

    await channel.handle_event(_payload())

    assert len(sender.sent) == 1
    assert "失败" in sender.sent[0][1]
