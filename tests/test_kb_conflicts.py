"""阶段 C 冲突检测 —— **只测判据本身**（不连库、不调模型）。

三个"必须报"和五个"不许报"，都是**首跑实测**（2026-10-23，51 篇 / 199 处假冲突 → 1 处真冲突）
踩出来的具名用例。少一条，检测器就会退回到"满屏告警、没人看"。
"""

from __future__ import annotations

from app.kb.conflicts import (
    DocumentText,
    build_report,
    extract_facts,
    find_conflicts,
    notify_report,
    scope_of,
    topic_of,
)

# ------------------------------------------------------------
# 提取
# ------------------------------------------------------------

GERMANY_INTERNAL = """
# 新客服入职手册 · 退换货篇

## 无理由退货：先问收货地址

- 默认（总则）：签收后 **7 个自然日**，商品未使用、原包装与配件齐全。
- 德国站：**30 个自然日**，退回运费由顾客承担。
- 日本站：**8 天**，必须未使用、原包装完好。
"""

GERMANY_POLICY = """
# 退换货总则

顾客自签收之日起 **7 个自然日内**，可申请无理由退货。

⚠️ **德国站是例外**：德国法律赋予的 **14 天**法定撤回权不因"定制"而排除。
"""


def _doc(name: str, text: str, status: str = "active") -> DocumentText:
    return DocumentText(filename=name, content_status=status, text=text)


def test_topic_and_scope_vocabulary() -> None:
    assert topic_of("无理由退货期限") == "退货期限"
    assert topic_of("质量问题退换") == "质量退换期限"
    assert topic_of("客户说商品破损了") == "报障时限"
    # ⚠️ 伞形词不算主题：收进来会把"质量 30 天"归成"退货期限"，然后和真的 7 天打架
    assert topic_of("退换货流程") is None
    assert scope_of("德国站：30 天") == "德国站"
    assert scope_of("美国 30 天") is None  # 只认"XX站"，不认裸国名


def test_germany_30_vs_14_is_a_conflict() -> None:
    """**核心用例**：内部手册写 30 天、官网政策写 14 天 —— 就是要抓这一对。"""
    conflicts = find_conflicts(
        [
            _doc("senyu/93-干扰-内部FAQ/01-手册.md", GERMANY_INTERNAL, status="internal"),
            _doc("senyu/10-退换货/10-退换货总则.md", GERMANY_POLICY),
        ]
    )

    german = [item for item in conflicts if item.scope == "德国站"]
    assert german, (
        f"没抓到德国站冲突，实际：{[(c.topic, c.scope, c.left.value) for c in conflicts]}"
    )
    assert {german[0].left.value, german[0].right.value} == {30.0, 14.0}
    assert german[0].kind == "internal_vs_policy"


def test_flow_sentence_is_not_a_rule() -> None:
    """⚠️ "客服在 48 小时内审核" / "顾客在 7 天内把商品交给承运方" 是**流程**，不是期限规定。"""
    facts = extract_facts(
        _doc(
            "a.md",
            "# 退换货总则\n\n## 无理由退货\n\n2. 客服在 48 小时内审核，通过后生成面单。\n",
        )
    )
    assert facts == []


def test_calendar_year_is_not_a_duration() -> None:
    """⚠️ "2023 年版政策…" 里的 2023 是年份，不是"2023 年的保修期"。"""
    facts = extract_facts(
        _doc(
            "a.md",
            "# 版本变更公告\n\n## 保修期限\n\n新版政策自 2025-12-31 起生效，2023 年版各项政策整体废止。\n",
        )
    )
    assert all(fact.value <= 100 for fact in facts)


def test_rumour_and_explainer_documents_are_not_parties() -> None:
    """辟谣稿 / 讲解稿写的是**别人的**规则 —— 不参与配对（不挡的话，辟谣文自己成了冲突源）。"""
    debunk = _doc(
        "senyu/92-干扰-其他地区/04-站外流传的假政策澄清.md",
        "# 假政策澄清\n\n## 德国站\n\n网上的说法不成立：德国是 14 天法定撤回权。\n",
    )
    explainer = _doc(
        "senyu/94-干扰-相邻概念/01-保修与退换货的区别.md",
        "# 保修与退换货的区别\n\n## 德国站\n\n- 期限：签收后 3 天。\n",
    )
    policy = _doc(
        "senyu/10-退换货/10-退换货总则.md",
        "# 退换货总则\n\n⚠️ 德国站是例外：14 天法定撤回权。\n",
    )

    assert find_conflicts([debunk, policy]) == []
    assert find_conflicts([explainer, policy]) == []


def test_superseded_and_sibling_brand_are_not_compared() -> None:
    """⚠️ 2023 版 15 天 ≠ 冲突（是版本变更）；Lab 的 30 天 ≠ 冲突（另一门生意）。"""
    policy = _doc(
        "senyu/10-退换货/10-退换货总则.md", "# 退换货总则\n\n签收后 7 个自然日内无理由退货。\n"
    )
    legacy = _doc(
        "senyu/90-干扰-历史版本/01-2023版退货政策.md",
        "# 退货政策\n\n签收后 15 个自然日内无理由退货。\n",
        status="superseded",
    )
    lab = _doc(
        "senyu/91-干扰-森屿Lab/02-Lab退换货政策.md",
        "# Lab 退换货政策\n\n签收后 30 个自然日内无理由退货。\n",
        status="sibling_brand",
    )

    assert find_conflicts([policy, legacy, lab]) == []


def test_explicit_priority_statement_suppresses_the_pair() -> None:
    """明确写了"本页优先于《总则》"的不算冲突 —— 那是**规则分层**，不是矛盾。"""
    region = _doc(
        "senyu/10-退换货/11-地区差异.md",
        "# 地区差异\n\n本页规则优先于《退换货总则》。\n\n## 德国站\n\n签收后 14 天法定撤回权。\n",
    )
    policy = _doc(
        "senyu/10-退换货/10-退换货总则.md", "# 退换货总则\n\n签收后 7 个自然日内无理由退货。\n"
    )

    assert find_conflicts([region, policy]) == []


def test_same_value_is_not_a_conflict() -> None:
    left = _doc("a/1-退货总则.md", "# 退货总则\n\n签收后 7 个自然日内无理由退货。\n")
    right = _doc("b/2-区域.md", "# 区域\n\n签收后 7 个自然日内无理由退货。\n")
    assert find_conflicts([left, right]) == []


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------


def test_report_carries_denominators_and_merges_duplicates() -> None:
    """⚠️ 不许裸数字：必须带"看了几篇 / 抽出几条"。

    同一条规则在多篇文档里重复出现时，要**合并成一行**（列对方文档），
    否则一个真问题会刷成三行，看的人只会觉得告警很吵。
    """
    report = build_report(
        [
            _doc("senyu/93-干扰-内部FAQ/01-手册.md", GERMANY_INTERNAL, status="internal"),
            _doc("senyu/10-退换货/10-退换货总则.md", GERMANY_POLICY),
            _doc("senyu/20-物流与税费/20-物流.md", "# 物流\n\n美国站：签收后 5 天送达。\n"),
        ]
    )

    assert report["documents"] == 3
    assert report["facts"] >= 1
    assert report["conflicts"] == len(report["items"])
    for item in report["items"]:
        # 每条冲突都必须带**原句**，否则等于让看告警的人自己去翻文档
        assert item["left"]["sentence"] and item["right"]["sentence"]
        assert item["left"]["doc"] and item["right"]["docs"]


# ------------------------------------------------------------
# 推送（v1.4 A3：定时告警）
# ------------------------------------------------------------


def _report_with_conflict() -> dict:
    return build_report(
        [
            _doc("senyu/93-干扰-内部FAQ/01-手册.md", GERMANY_INTERNAL, status="internal"),
            _doc("senyu/10-退换货/10-退换货总则.md", GERMANY_POLICY),
        ]
    )


async def test_notify_says_nothing_when_there_is_no_conflict() -> None:
    """没有冲突 → **一条都不发**。

    ⚠️ 每天推一条"一切正常"，等于训练所有人忽略这个群 —— 等真有冲突时没人看。
    """
    sent: list[str] = []

    async def fake_push(text: str) -> bool:
        sent.append(text)
        return True

    clean = build_report(
        [_doc("a/1-退货总则.md", "# 退货总则\n\n签收后 7 个自然日内无理由退货。\n")]
    )

    assert clean["conflicts"] == 0
    assert await notify_report(clean, push=fake_push) is True
    assert sent == []


async def test_notify_reports_failure_when_nothing_was_delivered() -> None:
    """要求推送、却什么都没发出去（`ALERT_WEBHOOK_URL` 没配）→ **返回 False**。

    ⚠️ 这条防的是定时任务"天天成功、群里一条没有"：
       `push_text` 在没配 webhook 时是**静默返回 False** 的，不在这里翻成失败，
       systemd 那边看到的永远是绿的（`AGENTS §10`「某一步静默没生效」）。
    """
    report = _report_with_conflict()
    assert report["conflicts"] == 1

    async def push_nothing(text: str) -> bool:
        return False

    assert await notify_report(report, push=push_nothing) is False


async def test_notify_sends_the_report_only_when_it_has_a_conflict() -> None:
    report = _report_with_conflict()
    sent: list[str] = []

    async def fake_push(text: str) -> bool:
        sent.append(text)
        return True

    assert await notify_report(report, push=fake_push) is True
    assert len(sent) == 1
    # 推出去的必须是**人读版**：带分母、带两份文档、带原句
    assert "1 处冲突" in sent[0]
    assert "德国站" in sent[0]
    assert "30" in sent[0] and "14" in sent[0]
