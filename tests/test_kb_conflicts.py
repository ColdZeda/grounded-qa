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
    # ⚠️ 破损与丢件/少件是**两个主题**：前者 48 小时、后者 7 天（v1.4 A4）
    assert topic_of("客户说商品破损了") == "破损报障时限"
    assert topic_of("少件 / 错发") == "丢件少件报障时限"
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
# v1.4 A4：放宽召回（同值重复 / 跨单位 / 主题拆分）
# ------------------------------------------------------------

DAMAGE_POLICY_48H = """
# 丢件与破损的报障时限

| 情形 | 报障时限 | 必填材料 |
|---|---|---|
| **外箱破损 / 商品破损** | **签收后 48 小时内** | 外箱六面照片 |
"""

QC_DAMAGE_7D = """
# 质检与报障判定标准（内部）

## 报障窗口怎么算

- **破损**：签收日后 **7 天**内，一线看到 7 天内的破损单直接建紧急单。
"""

QC_LOST_7D = """
# 质检与报障判定标准（内部）

- **丢件**：签收日后 **7 天**内可报障。
"""

WARRANTY_POLICY = """
# 保修与维修

## 保修期限

| 部件类别 | 期限 | 起算点 |
|---|---|---|
| **家具主体** | **2 年** | 签收日 |
"""

SCRIPT_ONE_YEAR = """
# 客服话术库·高频问题

10. 「森屿的保修是全线统一 1 年，从签收日开始算，超过 1 年就不在保修范围内了。」
"""


def test_repeated_same_value_in_one_line_is_one_fact() -> None:
    """同一行把**同一个值**说两遍，不是"多条规则" —— 要照常抽出来。

    ⚠️ 语料里两处故意写错的正是这种写法（"**7 天**内…看到 **7 天**内的破损单"）。
       v1.4 A4 之前这条被判成"拆不准"直接跳过 ⟹ 漏抓。
    """
    facts = extract_facts(_doc("senyu/93-内部FAQ/03-质检标准（内部）.md", QC_DAMAGE_7D, "internal"))

    damage = [fact for fact in facts if fact.topic == "破损报障时限"]
    assert [fact.value for fact in damage] == [7.0]


def test_repeated_different_values_in_one_line_is_still_skipped() -> None:
    """一行里两个**不同**的值（"美国 30 天、德国 14 天"）仍然不猜 —— 宁可漏，不可乱报。"""
    text = "# 地区差异\n\n签收后 7 天内无理由退货；美国站 30 天、德国站 14 天。\n"
    values = {fact.value for fact in extract_facts(_doc("senyu/10-退换货/11-地区差异.md", text))}
    assert not values & {30.0, 14.0}


def test_damage_window_7_days_vs_48_hours_is_a_conflict() -> None:
    """**破损**窗口：现行 48 小时 vs 内部稿 7 天 —— 跨单位也要抓到。

    ⚠️ 这两个数在各自的单位里都"看起来正常"，只有换算到同一把尺子上才看得出矛盾
       （48 小时 = 2 天 ≠ 7 天）。
    """
    conflicts = find_conflicts(
        [
            _doc("senyu/20-物流时效与运费.md", DAMAGE_POLICY_48H),
            _doc("senyu/93-内部FAQ/03-质检标准（内部）.md", QC_DAMAGE_7D, status="internal"),
        ]
    )

    assert len(conflicts) == 1
    assert conflicts[0].topic == "破损报障时限"
    assert conflicts[0].family == "time"  # 跨单位比较
    assert {conflicts[0].left.value, conflicts[0].right.value} == {48.0, 7.0}


def test_same_duration_across_units_is_not_a_conflict() -> None:
    """`48 小时` 与 `2 天` 是**同一个窗口**，不许报冲突。"""
    two_days = DAMAGE_POLICY_48H.replace("48 小时内", "2 天内")

    conflicts = find_conflicts(
        [
            _doc("senyu/20-物流时效与运费.md", DAMAGE_POLICY_48H),
            _doc("senyu/20-物流时效与运费-副本.md", two_days),
        ]
    )

    assert conflicts == []


def test_lost_parcel_7_days_does_not_collide_with_damage_48_hours() -> None:
    """丢件 7 天 与 破损 48 小时是**两个主题**，不能配对。

    ⚠️ 这正是 A4 把 `报障时限` 拆成 `破损报障时限` ＋ `丢件少件报障时限` 的原因：
       合在一起时这类配对会盖住真正的那个错（内部稿把**破损**写成 7 天）。
    """
    conflicts = find_conflicts(
        [
            _doc("senyu/20-物流时效与运费.md", DAMAGE_POLICY_48H),
            _doc("senyu/93-内部FAQ/03-质检标准（内部）.md", QC_LOST_7D, status="internal"),
        ]
    )

    assert conflicts == []


def test_warranty_one_year_vs_two_years_is_a_conflict() -> None:
    """**保修**：话术库"全线统一 1 年" vs 政策"家具主体 2 年"。

    ⚠️ 那句 `1 年` 出现了**两次**（"统一 1 年…超过 1 年"），旧逻辑按"一行多个期限"跳过 ——
       于是内训稿里最危险的那句永远抓不到。
    """
    conflicts = find_conflicts(
        [
            _doc("senyu/40-服务/40-保修与维修.md", WARRANTY_POLICY),
            _doc("senyu/93-内部FAQ/02-客服话术库.md", SCRIPT_ONE_YEAR, status="internal"),
        ]
    )

    assert len(conflicts) == 1
    assert conflicts[0].topic == "保修期限"
    assert {conflicts[0].left.value, conflicts[0].right.value} == {1.0, 2.0}


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


def test_report_picks_the_sentence_that_matches_the_topic() -> None:
    """同一篇内部稿里"破损 7 天"和"丢件 7 天"值相同 —— 报告必须引用**破损**那句。

    ⚠️ 随便挑一句会让报告写着"破损报障时限"、引的却是**丢件**的原句（A4 实测出现过）。
    """
    report = build_report(
        [
            _doc("senyu/20-物流时效与运费.md", DAMAGE_POLICY_48H),
            _doc(
                "senyu/93-内部FAQ/03-质检标准（内部）.md",
                QC_DAMAGE_7D + "\n- **丢件**：签收日后 **7 天**内可报障。\n",
                status="internal",
            ),
        ]
    )

    item = report["items"][0]
    week = item["left"] if item["left"]["value"] == 7.0 else item["right"]
    assert "破损" in week["sentence"]
    assert "丢件" not in week["sentence"]


def test_report_prefers_the_policy_document_as_representative() -> None:
    """同一边有两篇文档写着同一个值时，代表文档选**现行政策**。

    ⚠️ 这条是被生产报告打出来的：破损那一侧的 `docs` 里有"竞品对比培训材料"，
       报告却把它当政策摆出来 —— 读者会以为竞品的说法就是我们的政策。
    """
    competitor = """
# 竞品对比培训材料（内部）

| 维度 | 森屿家居 | 竞品 |
|---|---|---|
| 破损报障 | 签收后 48 小时 | 未公开 |
"""
    report = build_report(
        [
            _doc("senyu/20-物流时效与运费.md", DAMAGE_POLICY_48H),
            _doc("senyu/93-内部FAQ/06-竞品对比培训材料（内部）.md", competitor, status="internal"),
            _doc("senyu/93-内部FAQ/03-质检标准（内部）.md", QC_DAMAGE_7D, status="internal"),
        ]
    )

    assert report["conflicts"] == 1
    item = report["items"][0]
    policy_side = item["left"] if item["left"]["value"] == 48.0 else item["right"]
    assert policy_side["doc"].endswith("20-物流时效与运费.md")
    assert policy_side["status"] == "active"
    # 两篇都记在这一侧的 `docs` 里 —— 挑代表文档不许把另一篇丢掉
    assert len(policy_side["docs"]) == 2
    # ⚠️ **现行政策固定在左边**：左右顺序不该取决于"哪篇先被抽出来"
    assert item["left"]["status"] == "active"


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
