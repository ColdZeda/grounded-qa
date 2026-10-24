"""阶段 C：**冲突告警** —— 同一主题、同一适用范围，两个不同的数字。

要治的病：客服手册写"德国站 30 天"、官网政策写"德国站 14 天法定撤回"，
**两份都摆在知识库里，谁都没发现**。等客户撞上才有人知道。

⚠️ 判据刻意收窄成三个条件**同时**成立，才叫冲突：

    ① 同一个**主题**（退货期限 / 质量退换期限 / 保修期限 / 报障时限）
    ② 同一个**适用范围**（美国站 / 德国站 / 全局 …）
    ③ 同一个**单位族**（天 / 月 / 年 / 小时），且数值不同

⚠️ **为什么要 ②**：语料里"总则 7 天 vs 美国站 30 天"**不是冲突** —— 那是分地区规则，
    文档自己写了"本页规则优先于《退换货总则》"。不筛 scope 的话，检测器第一次跑
    就会喷一堆这种"看起来矛盾"的误报，然后**再也没人看告警**。
    📌 告警的价值在于"每一条都值得点开"，不在于"发现得多"。

⚠️ **为什么只比同单位族**：`48 小时` 与 `7 天` 换算成同一个数就"冲突"，
    但它们根本不是同一个动作（前者是报障时限，后者是寄回时限）。
    宁可漏，不可误 —— 跨单位一律记为"不可比"（`CANDIDATES.md` 有记账）。

⚠️ **它抓的是"数字不一致"，不是"谁错了"。** 结论要人来看 ——
    输出里带**原句**和**文档名**，就是为了让人能在 30 秒内判断。
    ⚠️ 反过来，`kind` 字段把「内部材料 vs 现行政策」和「现行 vs 现行」分开：
        前者的运维动作是**改内部稿**，后者才需要产品决策。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk, Document
from app.db.repository import TenantScopedRepository
from app.rag import mdtext

#: 主题词表：**标题或句子**命中关键词即归到该主题。顺序有意义（先命中先返回）。
#: ⚠️ **刻意不收"退换货"**这个伞形词：语料里"质量问题退换"也含它，
#:    收进来会让"质量 30 天"被归成"无理由退货期限"，于是和真的 7 天打架（首跑实测）。
_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("退货期限", ("无理由退货", "无理由", "撤回权", "撤回")),
    ("质量退换期限", ("质量问题", "质量缺陷")),
    ("保修期限", ("保修期", "保修")),
    # ⚠️ **破损**必须排在"报障"前面，而且不能和它合并成一个主题：
    #    破损是 **48 小时**、丢件/少件是 **7 天** —— 合成一个"报障时限"会造出
    #    "48 小时 vs 7 天"的假冲突，而真正要抓的（内部稿把破损写成 7 天）
    #    反而淹没在里面（v1.4 A4 实测：合并时 3 处假冲突，拆开后只剩真的）。
    ("破损报障时限", ("破损",)),
    ("丢件少件报障时限", ("丢件", "少件", "错发", "报障")),
)

#: 地区 → 适用范围。**没命中就是"全局"**（总则类文档的规则适用于所有站）。
_REGIONS: tuple[str, ...] = ("美国站", "德国站", "日本站", "新加坡", "马来西亚", "中国")

#: 期限表达式。⚠️ **刻意不含"工作日"** —— "7~15 个工作日到账"是退款时效，
#: 和"7 天无理由"不是一个主题，混进来必误报。
_DURATION = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>个自然日|自然日|个月|小时|天|日|年)")

#: 单位 → 单位族（只在同族内比较，见模块说明）。
_FAMILY = {
    "个自然日": "day",
    "自然日": "day",
    "天": "day",
    "日": "day",
    "个月": "month",
    "月": "month",
    "年": "year",
    "小时": "hour",
}

#: **跨单位也能比**的那一对：小时 ↔ 天（`1 天 = 24 小时` 是定义，不是估算）。
#: ⚠️ **只收这一对**。`1 个月 = 30 天` 是**约等于**，拿它去判"4 个月 vs 120 天"
#:    会把两条都对的规则报成冲突（月长不固定，2 月只有 28 天）—— 宁可漏，不可乱报。
_CANONICAL_HOURS = {"hour": 1.0, "day": 24.0}

#: 展示用单位名。报告里**每一侧各用自己的单位**。
_UNIT_LABEL = {"day": "天", "hour": "小时", "month": "个月", "year": "年"}

#: 括注里通常写"长于总则的 7 天"这类**对照**，取数字前先剥掉，否则会把参引当成规则值。
_PAREN = re.compile(r"[（(][^）)]*[）)]")

#: 真正的"规则句"必须有个**锚点**：期限从哪算、是多少天、或者以地区开头成条。
#: ⚠️ 这一条治的是"**流程句冒充规则句**"：客服手册里"客服在 48 小时内审核"、
#:    "顾客在 7 天内把商品交给承运方"都不是期限规定，却都带数字和"内"。
#:    首跑（2026-10-23）32 处冲突里大半是这两个句式造出来的。
_ANCHOR = re.compile(r"签收|期限|起算|自.*起|无理由|撤回权")

#: 以地区开头的条目（`- 德国站：30 个自然日…`）—— 这类句子自带范围和数值，就是规则。
_ENTRY = re.compile(r"^[-*]?\s*(?:美国站|德国站|日本站|新加坡|马来西亚)[：:]")

#: **引用别人的说法**的句子不算自己的规则。
#: ⚠️ `92/04-站外流传的假政策澄清` 整篇都在复述谣言（"英国 30 天无理由退货"），
#:    不挡的话，辟谣文自己会变成"冲突源" —— 这是最讽刺的一类误报。
_RUMOUR = ("流传", "传闻", "谣言", "说法", "网传", "误传", "早期预览")

#: 跨文档**优先声明**：`《退换货总则》` 这种引用。
#: ⚠️ 只用来**抑制误报**（明确写了谁优先就不算冲突），不用于判断谁对。
_TITLE_REF = re.compile(r"《([^》]{2,40})》")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

#: 入库切块时，每个 chunk 前面会被加上**章节路径**（`[文档标题 > 小节]`，见 `app/kb/ingest.py`）。
#: ⚠️ 从库里取出来的文档**没有 `#`**，章节信息全在这个方括号前缀里 —— 不认它，
#:    "德国站 → 期限 14 天" 这种规则句就会丢掉"德国站"这个范围，检测直接失效。
_BREADCRUMB = re.compile(r"^\[([^\]]{2,200})\]\s*$")

#: **只当"规则"读的文档**：对比 / 解释 / 辟谣型材料写的是"A 是 X、B 是 Y"或"网上说 X"，
#: ⚠️ 它们陈述的是**别人的**规则，不是自己的政策 —— 当成冲突方会喷一堆假冲突
#:    （2026-10-23 首跑：51 篇 / 199 处冲突，几乎全是这一类造出来的）。
#: 仍然被扫描（提取事实），只是**不参与配对**。
_NOT_A_POLICY = ("干扰-相邻概念", "站外流传")

#: 参与**配对**的状态：现行政策 ＋ 内部材料。
#: ⚠️ `superseded` 不参与 —— 2023 版 15 天和现行 7 天**不是冲突**，是版本变更（阶段 B2 已解决）。
#: ⚠️ `draft` 未生效、`sibling_brand` 是另一门生意（Lab 政策独立），都不该和主品牌比。
_COMPARABLE = ("active", "internal")

#: 报告里"代表文档"的优先顺序：**现行政策**最能代表这一侧的说法，
#: 内部稿 / 草案 / 存档 / 兄弟品牌依次往后（文档名只用来打破平局）。
_STATUS_PRIORITY = {"active": 0, "internal": 1, "draft": 2, "superseded": 3, "sibling_brand": 4}

#: 推送函数（注入点）：`(text) -> 是否真的发出去了`。
#: 测试塞假实现，生产用 `app.core.alerts.push_text`。
PushFn = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class DocumentText:
    """一篇待检测文档。"""

    filename: str
    content_status: str
    text: str


@dataclass(frozen=True)
class Fact:
    """一条"期限事实"：某篇文档说，在某个适用范围下，某主题是多少。"""

    topic: str
    scope: str
    family: str
    value: float
    doc: str
    sentence: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc": self.doc,
            "status": self.status,
            "topic": self.topic,
            "scope": self.scope,
            "value": self.value,
            "unit_family": self.family,
            # ⚠️ 原句要洗掉 markdown 源码再给出去（2026-10-25，和 W6 同一类问题）：
            #    很多事实是从 markdown 表格里抽出来的，原句就是 `| 家具主体 | 2 年 | 签收日 |`。
            #    页面上是**纯文本**渲染，不洗就会把表格源码怼到人脸上 —— 而它是"证据"，
            #    少一列也等于改了证据，所以只换形状（`｜` 分隔），一个字都不删。
            "sentence": mdtext.snippet(self.sentence, limit=120),
        }


@dataclass(frozen=True)
class Conflict:
    """一对数字对不上的事实。

    `family` 是**用来比较的族**：两边同族时就是那个族；`天 ↔ 小时` 换算后比较时是
    `"time"`（见 `_CANONICAL_HOURS`）。⚠️ 展示时**每一侧各用自己的单位** ——
    "7 天 vs 48 小时" 写成 "7 vs 48（day）" 是在说假话。
    """

    topic: str
    scope: str
    family: str
    left: Fact
    right: Fact

    @property
    def kind(self) -> str:
        """`internal_vs_policy`（内部稿与现行政策不一致）或 `active_vs_active`。"""
        return _kind_of(self.left.status, self.right.status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "topic": self.topic,
            "scope": self.scope,
            "unit_family": self.family,
            "left": self.left.as_dict(),
            "right": self.right.as_dict(),
        }


def _kind_of(left_status: str, right_status: str) -> str:
    """这一对冲突属于哪类 —— 两边的**运维动作不同**（前者改内部稿，后者走产品决策）。"""
    statuses = {left_status, right_status}
    if "internal" in statuses or "draft" in statuses:
        return "internal_vs_policy"
    return "active_vs_active"


def topic_of(text: str) -> str | None:
    """一句话（或一个标题）属于哪个主题。认不出 → `None`。"""
    for topic, keywords in _TOPICS:
        if any(keyword in text for keyword in keywords):
            return topic
    return None


def scope_of(text: str) -> str | None:
    """这一行属于哪个适用范围。没有地区词 → `None`（由标题的"粘性"决定）。"""
    for region in _REGIONS:
        if region in text:
            return region
    return None


def extract_facts(document: DocumentText) -> list[Fact]:
    """把一篇文档拆成事实列表。

    ⚠️ **主题和范围都允许"粘性继承"**：语料是 markdown，
        `## 德国站` 之后的 `- 期限：签收后 14 天` 自己不含任何关键词。
        只看单行的话，**恰恰会漏掉德国站那条**（也就是我们唯一确认的真实冲突）。
    """
    facts: list[Fact] = []
    topic = topic_of(document.text.splitlines()[0]) if document.text else None
    scope: str | None = None

    for raw in document.text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            # 引注块是声明/免责，不是规则值 —— 混进来会造出"30 天"这种假事实
            continue

        heading = _HEADING.match(line)
        if heading is not None:
            title = heading.group(2)
            topic = topic_of(title) or topic
            scope = scope_of(title) or scope
            continue

        crumb = _BREADCRUMB.match(line)
        if crumb is not None:
            title = crumb.group(1)
            topic = topic_of(title) or topic
            scope = scope_of(title) or scope
            continue

        cleaned = _PAREN.sub("", line)
        if any(word in cleaned for word in _RUMOUR):
            continue
        matches = list(_DURATION.finditer(cleaned))
        # ⚠️ 一行里出现**同一个值**多次（"签收后 **7 天**内…看到 **7 天**内的破损单"）
        #    不是"多条规则"，只是同一句话说两遍 —— 这种要**照常提取**。
        #    v1.4 A4 之前这里是 `len(matches) != 1 → 跳过`，于是语料里两处故意写错的
        #    （保修"统一 1 年"、破损"7 天内"）**一条都没被抽出来**。
        #    出现**不同**的值（"美国 30 天 / 德国 14 天"）才真的拆不准 ⟹ 仍然跳过。
        distinct = {(float(m.group("value")), m.group("unit")) for m in matches}
        if len(distinct) != 1:
            continue
        if _ENTRY.match(cleaned) is None and _ANCHOR.search(cleaned) is None:
            continue
        line_topic = topic_of(cleaned) or topic
        if line_topic is None:
            continue
        line_scope = scope_of(cleaned) or scope or "全局"
        match = matches[0]
        unit = match.group("unit")
        value = float(match.group("value"))
        if _FAMILY[unit] == "year" and value > 100:
            # ⚠️ "2023 年版政策…" 里的 2023 是**年份**，不是"2023 年的保修期"。
            #    不挡的话，版本变更公告会和保修期限表打出一堆"2023 vs 2"的假冲突（首跑实测）。
            continue
        facts.append(
            Fact(
                topic=line_topic,
                scope=line_scope,
                family=_FAMILY[unit],
                value=value,
                doc=document.filename,
                sentence=cleaned,
                status=document.content_status,
            )
        )
    return facts


def _aliases(filename: str) -> set[str]:
    """文档的"可被引用名"：`10-退换货/10-退换货总则.md` → `退换货总则`。"""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return {stem, re.sub(r"^[\d\-]+", "", stem), re.sub(r"^\d+-", "", stem)}


def _comparison_family(left: Fact, right: Fact) -> str | None:
    """这两条事实能在哪个"族"里比？不能比 → `None`。

    · 同族 → 就是那个族；
    · **天 / 小时** → `"time"`（换算成小时比，见 `_CANONICAL_HOURS`）；
    · 其余一律不能比（月与年不互换 —— `1 个月 ≠ 30 天`）。
    """
    if left.family == right.family:
        return left.family
    if left.family in _CANONICAL_HOURS and right.family in _CANONICAL_HOURS:
        return "time"
    return None


def _in_hours(fact: Fact) -> float:
    """只在 `_comparison_family()` 返回 `"time"` 时调用。"""
    return fact.value * _CANONICAL_HOURS[fact.family]


def _same_duration(left: Fact, right: Fact, family: str) -> bool:
    """换算到同一把尺子之后是否相等 —— `48 小时` 与 `2 天` **不是**冲突。"""
    if family == "time":
        return abs(_in_hours(left) - _in_hours(right)) < 1e-9
    return left.value == right.value


def find_conflicts(documents: Sequence[DocumentText]) -> list[Conflict]:
    """两两比对，找出「同主题 + 同范围 + **能不能比** + 值不同」的冲突。"""
    comparable = [
        document
        for document in documents
        if document.content_status in _COMPARABLE
        and not any(marker in document.filename for marker in _NOT_A_POLICY)
    ]
    facts = [fact for document in comparable for fact in extract_facts(document)]
    references = {
        document.filename: set(_TITLE_REF.findall(document.text)) for document in comparable
    }

    conflicts: list[Conflict] = []
    for index, left in enumerate(facts):
        for right in facts[index + 1 :]:
            if left.doc == right.doc:
                continue
            if (left.topic, left.scope) != (right.topic, right.scope):
                continue
            # ⚠️ 单位族放在**能比之后**判，不再要求两边字符串相同 —— 否则
            #    "破损 7 天" 和 "破损 48 小时" 永远配不到一起（v1.4 A4）。
            family = _comparison_family(left, right)
            if family is None:
                continue
            if _same_duration(left, right, family):
                continue
            # 明确写了"谁优先"的一对**不算冲突**（例：地区差异明确优先于总则）
            if references[left.doc] & _aliases(right.doc):
                continue
            if references[right.doc] & _aliases(left.doc):
                continue
            conflicts.append(
                Conflict(
                    topic=left.topic,
                    scope=left.scope,
                    family=family,
                    left=left,
                    right=right,
                )
            )
    return conflicts


def build_report(documents: Sequence[DocumentText]) -> dict[str, Any]:
    """报告体：**带分母**（看了几篇 / 抽出几条事实），不是孤零零一个"3 处冲突"。

    ⚠️ 同一条规则经常在**多篇**文档里重复出现（总则 §地区差异 §澄清稿），
        一次冲突会以"内部稿 vs 每篇各一份"的形式报出来 —— 不合并的话，
        一个真问题会被刷成三行，看的人只会觉得"告警很吵"。
        合并键取 **主题＋范围＋单位族＋两边的值**，把对方文档并成一个列表。
    """
    comparable = [
        document
        for document in documents
        if document.content_status in _COMPARABLE
        and not any(marker in document.filename for marker in _NOT_A_POLICY)
    ]
    facts = [fact for document in comparable for fact in extract_facts(document)]
    conflicts = find_conflicts(documents)

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for conflict in conflicts:
        left, right = conflict.left, conflict.right
        # ⚠️ 合并键**不含文档名**：同一个错值可能同时被"物流政策"和"竞品对比"写着，
        #    按文档名分会把**同一个问题**刷成两行（v1.4 A4 实测：5 条里有 2 条是这种重复）。
        #    值排序后再进键，左右颠倒的同一条冲突才会并到一起。
        key = (conflict.topic, conflict.scope, conflict.family, *sorted((left.value, right.value)))
        item = merged.get(key)
        if item is None:
            item = conflict.as_dict()
            item["left"]["docs"] = [left.doc]
            item["right"]["docs"] = [right.doc]
            merged[key] = item
        else:
            # 左右可能颠倒（谁先被抽出来取决于文档顺序）—— 按**值**去找该并到哪一侧。
            # ⚠️ 不能按"左边并左边"，那样颠倒的那条会**静默丢掉**（v1.4 A4 实测）。
            for value, doc in ((left.value, left.doc), (right.value, right.doc)):
                for side in (item["left"], item["right"]):
                    if value == side["value"] and doc not in side["docs"]:
                        side["docs"].append(doc)

    # ⚠️ 合并后要**重选代表文档**：同一边可能既有"官网政策"又有"竞品对比培训材料"，
    #    把培训材料当政策摆出来是误导（v1.4 A4 在生产报告里真出现过一次）。
    #    `docs` 按状态排序（active 最前），代表文档跟着换成排第一的那篇 —— **原句一起换**，
    #    否则文件名和引文对不上。
    status_of = {document.filename: document.content_status for document in comparable}
    fact_of: dict[tuple[str, float], list[Fact]] = {}
    for fact in facts:
        fact_of.setdefault((fact.doc, fact.value), []).append(fact)

    def _best_sentence(candidates: Sequence[Fact], scope: str, topic: str) -> Fact:
        """同一篇文档里可能有好几句写着**同一个值** —— 挑最能代表这条冲突的那句。

        例：内部质检稿里"丢件 7 天"和"破损 7 天"都在这一篇里，值是同一个 7，
        ⚠️ 随便挑一句会让报告写着"破损"却引用**丢件**的原句（实测出现过）。
        所以先按**句子自己的主题**筛，再挑带得上范围的，最后才退回第一句。
        """
        # ⚠️ 只**排除**"自己写着另一个主题"的句子（丢件那句），不能反过来只留
        #    "自己带主题词"的句子 —— 条目式规则（"- 德国站：30 个自然日"）往往
        #    主题是靠章节标题继承的，句子里根本没有"退货"两个字。
        pool = [
            candidate for candidate in candidates if topic_of(candidate.sentence) in (None, topic)
        ]
        pool = pool or list(candidates)
        if scope and scope != "全局":
            scoped = [candidate for candidate in pool if scope in candidate.sentence]
            if scoped:
                pool = scoped
        # 同等条件下取**最短**那句：短句通常是条目式规则，长句往往是说明性段落 ——
        # 后者也写着同一个值，但引出来读着像在狡辩。
        return min(pool, key=lambda candidate: len(candidate.sentence))

    for item in merged.values():
        for side in (item["left"], item["right"]):
            side["docs"].sort(
                key=lambda name: (_STATUS_PRIORITY.get(status_of.get(name, ""), 9), name)
            )
            candidates = fact_of.get((side["docs"][0], side["value"])) or []
            if candidates:
                side.update(_best_sentence(candidates, item["scope"], item["topic"]).as_dict())
        # ⚠️ **现行政策固定放左边**：左右顺序不该取决于"哪篇先被抽出来" ——
        #    同一个知识库，本机跑和生产跑给出相反的顺序，读的人会以为看错了报告。
        if _STATUS_PRIORITY.get(item["left"]["status"], 9) > _STATUS_PRIORITY.get(
            item["right"]["status"], 9
        ):
            item["left"], item["right"] = item["right"], item["left"]
        item["kind"] = _kind_of(item["left"]["status"], item["right"]["status"])

    return {
        "documents": len(comparable),
        "skipped_documents": len(documents) - len(comparable),
        "facts": len(facts),
        "conflicts": len(merged),
        "items": list(merged.values()),
    }


def render_text(report: dict[str, Any]) -> str:
    """人读版。⚠️ 每条都带**原句** —— 没有原句的告警等于"你自己去找"。"""
    head = (
        f"知识库冲突检查：{report['documents']} 篇参与配对（跳过 {report['skipped_documents']} 篇"
        f"讲解稿 / 存档版 / 兄弟品牌）／抽出 {report['facts']} 条期限事实／"
        f"**{report['conflicts']} 处冲突**"
    )
    if not report["conflicts"]:
        return head + "\n（同主题、同适用范围、同单位族的数字全部一致）"
    lines = [head]
    for item in report["items"]:
        left, right = item["left"], item["right"]
        others = "、".join(right["docs"])
        lines.append(
            f"\n· [{item['kind']}] {item['topic']}（{item['scope']}）："
            f"{left['value']:g} {_UNIT_LABEL.get(left['unit_family'], left['unit_family'])}"
            f" vs {right['value']:g} {_UNIT_LABEL.get(right['unit_family'], right['unit_family'])}"
            f"\n    A {left['doc']}（{left['status']}）：{left['sentence']}"
            f"\n    B {others}（{right['status']}）：{right['sentence']}"
        )
    return "\n".join(lines)


async def notify_report(report: dict[str, Any], *, push: PushFn | None = None) -> bool:
    """把报告推到群机器人。返回**是否真的发出去了**（"无事可报"也算成功）。

    ⚠️ **没冲突就什么都不发**：每天推一条"一切正常"，等于训练所有人忽略这个群。
    ⚠️ **要求推送却发不出去 → 返回 False**，调用方必须把它当成失败：
        `push_text` 在 `ALERT_WEBHOOK_URL` 没配时是**静默返回 False** 的 ——
        不在这里翻成失败，定时任务就会天天"成功"而群里一条没有
        （`AGENTS §10` 那条"某一步静默没生效"就是这个病）。
    ⚠️ 投递被拒绝（业务码非 0）时 `push_text` 会**抛** `RuntimeError` —— 照样以失败告终。
    """
    if not report.get("conflicts"):
        return True
    if push is None:
        from app.core.alerts import push_text

        push = push_text
    return bool(await push("🚨 知识库冲突检查\n" + _plain(render_text(report))))


def _plain(text: str) -> str:
    """把 `render_text` 里的轻量 markdown 抹掉 —— **群机器人发的是纯文本**。

    ⚠️ 飞书的 `msg_type=text` 不渲染 markdown：`**1 处冲突**` 会原样显示成星号
       （2026-10-23 真机截图里就是这样）。我们只在 `render_text` 里用了 `**` 一种标记，
       所以这里只抹它 —— 真要做富文本得换成卡片（`msg_type=interactive`），
       而 `push_text` 是**飞书 / 企微共用**的告警通道，为一句加粗换消息类型不值当。
    """
    return text.replace("**", "")


async def collect(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[DocumentText]:
    """从库里取当前租户**所有文档的全文**（按 `ordinal` 拼回整篇）。

    ⚠️ 走 `TenantScopedRepository`（约定 12）—— 这里读的是全租户的知识库，
        不按用户过滤：冲突是**知识的属性**，不是某个人的。
    """
    document_repo = TenantScopedRepository(session, tenant_id=tenant_id, model=Document)
    documents = list((await session.scalars(document_repo.select())).all())
    if not documents:
        return []

    chunk_repo = TenantScopedRepository(session, tenant_id=tenant_id, model=Chunk)
    stmt = (
        chunk_repo.select()
        .where(Chunk.document_id.in_([document.id for document in documents]))
        .order_by(Chunk.document_id, Chunk.ordinal)
    )
    texts: dict[uuid.UUID, list[str]] = {document.id: [] for document in documents}
    for chunk in (await session.scalars(stmt)).all():
        texts[chunk.document_id].append(chunk.content)

    return [
        DocumentText(
            filename=document.filename,
            content_status=document.content_status.value,
            text="\n".join(texts[document.id]),
        )
        for document in documents
    ]


__all__ = [
    "Conflict",
    "DocumentText",
    "Fact",
    "PushFn",
    "build_report",
    "collect",
    "extract_facts",
    "find_conflicts",
    "notify_report",
    "render_text",
    "scope_of",
    "topic_of",
]


async def _main(argv: Sequence[str] | None = None) -> int:
    """CLI：`uv run python -m app.kb.conflicts [--notify] [--json]`。

    ⚠️ **不放进任何一个容器的启动流程**：它是体检，不是业务。
        生产的调用方是服务器上的 **systemd timer**（`grounded-conflicts.timer`，
        每天 09:30，见 `ops/server-notes.md`），unit 调 `deploy/ops/conflicts-check.sh`。
    """
    import argparse
    import json
    import sys

    from sqlalchemy import select as sa_select

    from app.core.config import get_settings
    from app.db.models.tenant import Tenant
    from app.db.session import dispose_engine, get_session_factory

    parser = argparse.ArgumentParser(prog="python -m app.kb.conflicts")
    parser.add_argument("--tenant", default=None, help="租户 slug（默认取配置里的 default）")
    parser.add_argument("--notify", action="store_true", help="把报告推到群机器人")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    args = parser.parse_args(argv)

    settings = get_settings()
    factory = get_session_factory()
    try:
        async with factory() as session:
            slug = args.tenant or settings.default_tenant_id
            tenant = await session.scalar(sa_select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise SystemExit(f"库里没有 slug={slug!r} 的租户 —— 先跑 `python -m app.db.seed`")
            report = build_report(await collect(session, tenant_id=tenant.id))
    finally:
        await dispose_engine()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    if args.notify and not await notify_report(report):
        print(
            "告警没推出去：ALERT_WEBHOOK_URL 没配（群里不会有这条）—— 按失败退出，"
            "免得定时任务天天'成功'而没人收到过任何东西",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_main()))
