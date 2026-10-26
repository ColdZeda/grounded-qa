"""引用**可核对性**审计：回答里的 `[n]` 到底指对了没有。

⚠️ 为什么需要它（2026-10-23 真机抓到）：

    B2 真机验收时，回答把"布艺与填充件 **6 个月**"标成 `[2]`，而那两行写在 `[3]` 里，
    `[2]` 是另一张**没有任何期限数字**的表。界面有引用、点进去是另一段话 ——
    **不逐条核对就发现不了**。这正是"引用溯源"最隐蔽的失败方式。

⚠️ **审计**本身只做"抓"，不做"改"。"改"由下面的 `repair()` 单开一条路，规矩完全另算。原先三条理由：

    ① 流式回答**已经发出去了**，事后改写等于让用户看到前后两个版本；
    ② 我们的检查器自己也会错 —— 误报时删掉一个**正确**的引用，比串号更糟
       （用户失去核对入口，而且我们没有任何记录）；
    ③ 先能度量，才谈得上修。串号率是 0 还是 12%，决定的修法完全不同。

    ⟹ **2026-10-27 起多了一个例外：`repair()`（修法 D）。** ①②③ 逐条看现在是这样：

        · ① **仍然成立** ⟹ 所以 `repair()` **必须在文字发出去之前**生效：
          agent 那条路答案本来就是一整段（`finish` 的参数）；pipeline 那条路靠
          `StreamingRepair` **按句攒齐再发**（流式粒度从"逐字"变成"逐句"）。
        · ② **仍然成立** ⟹ 所以 `repair()` **什么都不删**：它只把编号**指到真正写着
          那个数字的块**，而且只在「这一句**只有一个**编号」且「有且只有**一块**能支持
          这句话的**全部**原子」时才动手 —— 多一个候选、少一个候选，都一个字不改。
        · ③ **已经量过了**（306 句）：真串号 ≈2.5%、形态**全是错块号**、
          校准后判据精确率 5/5。有这个数才敢改。

    ⟹ 现在的分工：**`audit()` 度量**（模型错了几次）、**`repair()` 兜底**（用户看到的那份是对的）。
       两个计数器分开记：`gq_citation_support_total` / `gq_citation_repaired_total`。

⚠️ 判据刻意宽松（**宁可放过，不可误杀**）：
    · 只核**数字 + 单位**这类"最不可替换"的原子 —— "退货"和"退换"是等价的，
      拿它们判"支不支持"会把正确的引用判成错的；
    · 一句里标了多个编号时，只要原子出现在**其中任一**被引段落里就算通过
      （一句话用两段资料各支撑一半，是合法写法）。

⚠️ **2026-10-27 校尺（串号抽样之后改的，别改回去）**：
    真机抽了 240 句可核句子（历史 70 条消息 ＋ 20 道题跑两轮 ＋ 单题复现），
    本模块报了 16 句"对不上"，**逐条复核后只有 2 句是真串号（0.83%）**，
    其余 14 句全是**尺子自己造的**：

        ① 列表/标题序号被当成数据 —— "2. 补拍照片 [1][4]"、"**3. 有效期** [2]"（6 例）
        ② 单位附着差异 —— 原文表格写"日本 8"（"天"在列头），模型渲染成"8 天"（4 例）
        ③ 订单事实 —— 订单快照里是裸值 `8`，模型写"已签收 8 天"（3 例，且 agent 的
           交卷自查会因此**误退正确答案重写一次**，白花一轮 token）
        ④ 模型自数 —— "已开通地区（5 个）"里的 5 是它数出来的，原文没有这个数（1 例）

    ⟹ 判据从"数字＋单位连在一起"改成 **"数字必核、单位只作辅助"**：
       原子通过的条件是 `原子完整出现` **或** `数字出现在原文里、且单位也在这一块里出现过`。
       **代价是"单位写错"（把 7 天写成 7 个月）会被放过** —— 这是**故意**的取舍：
       那个数字仍然要核，而"错块号"型串号（真串号唯一的形态）照样抓得住。
       抽样里"单位写错"**一例都没有**，"单位没连写"却占误报的四分之一。

    ⟹ 另外两类不再当数据：**列表/标题的序号**、**ISO 日期**（`2026-10-17` 会被拆成
       2026 / 10 / 17 三个原子，而它来自订单系统，不是条款数值 —— 和 `_CALENDAR`
       豁免 `2024 年 3 月` 是同一个理由）。

    📌 **已知残留误报**：模型自己数出来的数量（"共 5 个"）核不了，仍会被报。
       宁可留着 —— 它比"把正确引用删掉"轻得多。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.rag.retrieval import RetrievedChunk

#: 引用标记 `[3]`。⚠️ 只认 1~2 位数字：`[2025]` 这种是年份，不是引用。
_MARKER = re.compile(r"\[(\d{1,2})\]")

#: 句子的切分点。**保留分隔符**，否则引用标记可能被切到下一句里。
_SENTENCE = re.compile(r"[^。！？!?\n]*[。！？!?\n]?")

#: **行首的列表 / 标题序号**（`2. 补拍…`、`**3. 有效期**`、`### 4. 积分`）。
#: ⚠️ 这是**排版**，不是数据 —— 不剥掉的话，每一条编号列表都会被判串号
#:    （2026-10-27 抽样：16 处报警里 6 处是它）。
#: ⚠️ 末尾 `(?!\d)` 是留给小数的：`3.5 米` 里的 `3.` 后面是数字，不算序号。
_LEADING_ORDINAL = re.compile(r"^[\s>*_#|]*\d{1,2}\s*[.、)）](?!\d)\s*")

#: ISO 日期（`2026-10-17`）。**整段抹掉**，否则会被拆成 2026 / 10 / 17 三个原子。
#: ⚠️ 它来自订单系统或用户原话，不是条款数值 —— 和 `_CALENDAR` 豁免 `2024 年 3 月`
#:    是同一个理由：**不排除的话，任何带具体日期的回答都会被判串号**。
_ISO_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")

#: "最不可替换"的原子：数字 + 可选单位。单位表按语料实际出现过的收，
#: 收不全没关系 —— 没单位的数字照样会被抓成原子。
_ATOM = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:个工作日|工作日|个自然日|自然日|个百分点|个月|小时|分钟|天|日|年|月|周|次|件|折|%"
    r"|元|美元|公斤|kg|厘米|毫米|米)?"
)


def _norm(text: str) -> str:
    """比较用的归一化：**只抹空白和千分位逗号**。

    ⚠️ 不做同义词 / 全半角 / 大小写转换 —— 每多一条规则就多一种"我改了你却没改"的走样，
        而且改动发生在**证据**那一侧时，会让一个原本不支持的引用"看起来通过"。
    """
    return text.replace(",", "").replace(" ", "").replace("\u3000", "").lower()


def _norm_spaced(text: str) -> str:
    """只抹千分位逗号、**保留空白** —— 专供"某个数字是不是独立出现"那一步。

    ⚠️ **不能复用 `_norm`**：它连空白一起删，`2026-10-17 8 件` 会粘成 `2026-10-178件`，
        于是订单快照里的裸值 `8` 被判成"原文里没有"（2026-10-27 校尺时实测踩到）。
        数字粘连是这一整类判定的天敌：`8` 也不能去匹配 `18` / `80`。
    """
    return text.replace(",", "").replace("\u3000", " ").lower()


#: **日历分量**（`2024年` / `3月` / `15日`）不算"必须能在原文里找到"的原子。
#:
#: ⚠️ 这一条是**真机打出来的**（2026-10-23 02:06，飞书）：回答写
#:    "2024 年 3 月签收的订单，其 1 年保修期已于 2025 年 3 月前后到期 [3]"，
#:    被本模块判成串号 —— 但 `2024 年 3 月` 来自**用户提问**、`2025 年 3 月` 是模型
#:    **算出来**的，只有 `1 年` 才需要在原文里。
#:    不排除的话，**任何做了日期推算的回答都会被判串号**，这个比例就没意义了。
#: 📌 真正的串号风险在"7 天 / 30 天 / 2 年 / 6 个月"这类**条款数值**上，那些照样要核。
_CALENDAR = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>年|月|日)$")
#: 单位 → 算作"日历分量"的数值区间（闭区间）。
#: ⚠️ **年看下限、月日看上限**：`2024年` 是年份，`3月`/`15日` 是月日；
#:    而 `2年` / `13月` / `45日` 更像条款期限，仍要核。
_CALENDAR_RANGES = {"年": (1900.0, float("inf")), "月": (0.0, 12.0), "日": (0.0, 31.0)}


def _is_calendar(atom: str) -> bool:
    """`2024年` / `3月` / `15日` → True；`2年` / `6个月` / `30天` → False。

    ⚠️ `个月` 与 `月` **必须区分**：`6 个月` 是条款数值（要核），`3 月` 是日历（不核）。
    """
    match = _CALENDAR.match(atom)
    if match is None:
        return False
    low, high = _CALENDAR_RANGES[match.group("unit")]
    return low <= float(match.group("num")) <= high


def _atoms_of(sentence: str) -> list[str]:
    """一句话里的数字原子（**先去引用标记**，否则 `[3]` 里的 3 会被当成数据）。"""
    text = _LEADING_ORDINAL.sub("", sentence)
    text = _ISO_DATE.sub(" ", text)
    atoms = [_norm(atom) for atom in _ATOM.findall(_MARKER.sub("", text))]
    return [atom for atom in atoms if atom and not _is_calendar(atom)]


#: 原子开头的数字部分（`6个月` → `6`、`1000元` → `1000`）。原子一定以数字开头。
_ATOM_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _has_number(pool: str, number: str) -> bool:
    """`number` 是否作为**独立数字**出现在原文里。

    ⚠️ 两侧不许接别的数字：否则核 `8` 会被原文里的 `18` / `80` 蒙混过关 ——
        那正是"数字必核"最容易被绕过的地方。
    """
    return re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", pool) is not None


def _supported(atom: str, pool: str, pool_spaced: str) -> bool:
    """这个原子被 `pool`（若干被引块的原文拼起来）支持吗？

    ① 原子**完整出现** → 支持；
    ② 否则拆成"数字 + 单位"：**数字必须在原文里**，单位**只要这一块里出现过**就算
       （数字的"在不在"用 `pool_spaced` 判，见 `_norm_spaced` 为什么不能用同一份）。
       ⟹ "数字必核、单位只作辅助"。

    ⚠️ 第 ② 条是**故意的放宽**（2026-10-27 抽样校准）：表格里"日本 8"（"天"在列头）、
        "199"（"元"在列头）都是**原文确实支持**的写法，模型渲染成"8 天"/"199 元"
        却被判串号 —— 这类占误报的四分之一。代价是"单位写错"（7 天说成 7 个月）
        会被放过；但抽样里这种一例都没有，而"错块号"（真串号唯一的形态）照样抓得住。

    ⚠️ 两条路**都**要求"数字独立出现"（`_has_number`）——
        2026-10-27 实测踩到：`家具主体保修 3 年` 去比《2023 年版保修条款》，
        纯子串匹配会**因为标题里的 "2023 年版" 而通过**（"3年" 正好是 "2023年版" 的子串）。
        数字必须两侧不接别的数字，才算原文里有这个数。
    """
    match = _ATOM_NUMBER.match(atom)
    if match is None:
        return False
    number = match.group(0)
    if not _has_number(pool_spaced, number):
        return False
    if atom in pool:
        return True
    unit = atom[len(number) :]
    return not unit or unit in pool


@dataclass(frozen=True)
class CitationIssue:
    """一条"核对不上"的引用。"""

    markers: tuple[int, ...]
    atoms: tuple[str, ...]
    sentence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "markers": list(self.markers),
            "atoms": list(self.atoms),
            "sentence": self.sentence[:120],
        }


@dataclass(frozen=True)
class CitationAudit:
    """一次审计的结果。**只有计数与明细，没有"是否通过"的单一布尔** ——
    串号率是比例，不是开关。"""

    checked: int = 0
    """有引用标记、且含可核数字的句子数。"""
    unsupported: int = 0
    """其中核对不上的句子数。"""
    issues: list[CitationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.unsupported == 0

    @property
    def rate(self) -> float:
        """串号率。⚠️ `checked == 0` 时返回 0.0（没有可核的句子 ≠ 有问题）。"""
        return self.unsupported / self.checked if self.checked else 0.0

    def summary(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "unsupported": self.unsupported,
            "rate": round(self.rate, 4),
            "issues": [issue.as_dict() for issue in self.issues[:3]],
        }


def audit(
    answer_text: str,
    hits: Sequence[RetrievedChunk],
    *,
    extra_evidence: str = "",
) -> CitationAudit:
    """核对回答里每个 `[n]`：它后面那句话里的数字，真的在第 n 段里吗？

    Args:
        answer_text: 模型的完整回答（**流式拼完之后**）。
        hits: 用来拼【参考资料】的那批 chunk，**顺序必须与 `[n]` 编号一致**。
        extra_evidence: **不占编号**的附加证据（agent 留给订单事实用）。

            ⚠️ 为什么要有这个参数：订单事实（签收日 2026-10-17、签收 8 天）
            确实是证据，但它**不是知识库块、也不该占 `[n]`**。
            并进 `hits` 会把编号整个错位；不并又会把正确答案判成串号。

    Returns:
        计数 ＋ 明细。`hits` 为空（闭卷 / 拒答）时返回全零。
    """
    if not answer_text or not hits:
        return CitationAudit()

    contents = [_norm(hit.content) for hit in hits]
    spaced = [_norm_spaced(hit.content) for hit in hits]
    checked = 0
    issues: list[CitationIssue] = []

    for segment in _SENTENCE.findall(answer_text):
        if not segment.strip():
            continue
        markers = tuple(sorted({int(m) for m in _MARKER.findall(segment)}))
        if not markers:
            continue
        atoms = _atoms_of(segment)
        if not atoms:
            # 一句话标了引用却没有任何可核的数字 —— 不判对错，也不计入分母。
            # ⚠️ 刻意**不惩罚**："这条政策请咨询人工 [2]" 这类句子本来就全是名词。
            continue
        checked += 1
        pool = "".join(contents[m - 1] for m in markers if 1 <= m <= len(contents))
        pool_spaced = "".join(spaced[m - 1] for m in markers if 1 <= m <= len(contents))
        if extra_evidence:
            # 不占编号（不可被 `[n]` 引用），但**参与核对**。
            pool += _norm(extra_evidence)
            pool_spaced += _norm_spaced(extra_evidence)
        missing = tuple(atom for atom in atoms if not _supported(atom, pool, pool_spaced))
        if missing:
            issues.append(CitationIssue(markers=markers, atoms=missing, sentence=segment.strip()))

    return CitationAudit(checked=checked, unsupported=len(issues), issues=issues)


# ---------------------------------------------------------------- 修法 D：改编号

#: 句子的结束符。⚠️ 必须与 `_SENTENCE` 里的那个字符类**保持一致** ——
#: 否则"流式攒句"和"整段切句"切出来的句子不一样，修补的结果就会和审计对不上。
_TERMINATORS = "。！？!?\n"


@dataclass(frozen=True)
class CitationFix:
    """一处**被改掉的编号**。写日志 / 落库都靠它，没有它等于"系统悄悄动了证据"。"""

    sentence: str
    """出问题的那一句（截断到 120 字，够人看）。"""
    marker: int
    """模型原来标的编号。"""
    corrected: int
    """改成哪个编号（一定是本次引用列表里的一号）。"""
    chunk_id: str
    """新编号指向的那一块 —— 数字确实在这块原文里。"""

    def as_dict(self) -> dict[str, object]:
        return {
            "sentence": self.sentence[:120],
            "marker": self.marker,
            "corrected": self.corrected,
            "chunk_id": self.chunk_id,
        }

    def describe(self) -> str:
        return f"[{self.marker}]→[{self.corrected}] {self.sentence[:60]}"


@dataclass(frozen=True)
class CitationRepair:
    """修补结果。**`fixes` 为空表示一个字都没动。**"""

    text: str
    fixes: list[CitationFix] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.fixes)


def _repair_text(
    text: str,
    hits: Sequence[RetrievedChunk],
    contents: list[str],
    spaced: list[str],
    extra: str,
    extra_spaced: str,
) -> tuple[str, list[CitationFix]]:
    """逐句过一遍，能确定的就改编号。**返回改过的文本 ＋ 明细。**"""
    out: list[str] = []
    fixes: list[CitationFix] = []
    for segment in _SENTENCE.findall(text):
        fixed, fix = _repair_segment(segment, hits, contents, spaced, extra, extra_spaced)
        out.append(fixed)
        if fix is not None:
            fixes.append(fix)
    return "".join(out), fixes


def _repair_segment(
    segment: str,
    hits: Sequence[RetrievedChunk],
    contents: list[str],
    spaced: list[str],
    extra: str,
    extra_spaced: str,
) -> tuple[str, CitationFix | None]:
    """一句能不能改？**三条都要满足才动手**：

    1. 这一句**只有一个**编号 —— 多个编号时"该改哪一个"说不清，不动；
    2. 现在的编号**确实不支持**这句话（先确认它是错的，别去动对的）；
    3. 有**且只有一块**能支持这句话的**全部**原子 —— 0 块（没证据）或 2 块以上（说不清
       是哪一块）都不动。

    ⚠️ 第 3 条是"支持**全部**原子"，不是"支持那些对不上的原子"：
        只补上缺的那一个数，剩下的是否对得更没保证 —— 那等于用一半证据改编号。
    """
    markers = sorted({int(m) for m in _MARKER.findall(segment)})
    if len(markers) != 1:
        return segment, None
    marker = markers[0]
    atoms = _atoms_of(segment)
    if not atoms:
        return segment, None

    total = len(contents)

    def supported_by(index: int) -> bool:
        if not 1 <= index <= total:
            return False
        pool = contents[index - 1] + extra
        pool_spaced = spaced[index - 1] + extra_spaced
        return all(_supported(atom, pool, pool_spaced) for atom in atoms)

    if supported_by(marker):
        return segment, None  # 本来就对，一个字都不动

    candidates = [k for k in range(1, total + 1) if k != marker and supported_by(k)]
    if len(candidates) != 1:
        return segment, None

    corrected = candidates[0]
    fixed = segment.replace(f"[{marker}]", f"[{corrected}]")
    return fixed, CitationFix(
        sentence=segment.strip(),
        marker=marker,
        corrected=corrected,
        chunk_id=str(hits[corrected - 1].chunk_id),
    )


def repair(
    answer_text: str,
    hits: Sequence[RetrievedChunk],
    *,
    extra_evidence: str = "",
) -> CitationRepair:
    """把指错的编号**指到真正写着那个数字的块**（修法 D）。

    ⚠️ **只改编号，不改一个字**：句子、标点、顺序全部原样保留。
    ⚠️ **必须用在文字发出去之前**（agent 在 `finish` 里、pipeline 用 `StreamingRepair`），
       否则用户会看到前后两个版本 —— 那比串号更糟。
    ⚠️ 判据见 `_repair_segment` 那三条。`hits` 为空（闭卷 / 拒答）时原样返回。
    """
    if not answer_text or not hits:
        return CitationRepair(answer_text, [])
    contents = [_norm(hit.content) for hit in hits]
    spaced = [_norm_spaced(hit.content) for hit in hits]
    extra = _norm(extra_evidence) if extra_evidence else ""
    extra_spaced = _norm_spaced(extra_evidence) if extra_evidence else ""
    text, fixes = _repair_text(answer_text, hits, contents, spaced, extra, extra_spaced)
    return CitationRepair(text, fixes)


class StreamingRepair:
    """**按句攒齐再发**，让编号修补做在文字发出去之前。

    为什么要它：pipeline 是逐字流式，`[3]` 到的时候那一句早就发出去了。
    ⟹ 把"发"的粒度从**逐字**改成**逐句**（`。！？!?\n` 都算句末），
      每句凑齐 → 修补 → 再发。典型句子 ~30 字，肉眼只差一点点。

    ⚠️ **攒太久就原样发**（`max_hold`）：宁可不修，也不能让流式卡住。
       触发时那一句跑的是**未修补**的原文，会被 `audit()` 如实计入。
    ⚠️ `feed()` 返回的可能是空串（这句还没完）；调用方照常 `yield` 即可。

    用法（pipeline）：:

        buffer = StreamingRepair(hits, extra_evidence=order_facts)
        async for piece in chat.stream(...):
            raw.append(piece)          # 原文留着算"模型串号率"
            text = buffer.feed(piece)
            if text:
                yield Delta(text=text)
        tail = buffer.flush()
    """

    #: 一句最多攒这么多字符。超了就原样发（长句 / 表格行都可能撞上）。
    MAX_HOLD = 200

    def __init__(
        self,
        hits: Sequence[RetrievedChunk],
        *,
        extra_evidence: str = "",
        max_hold: int = MAX_HOLD,
    ) -> None:
        self._hits = list(hits)
        self._contents = [_norm(hit.content) for hit in hits]
        self._spaced = [_norm_spaced(hit.content) for hit in hits]
        self._extra = _norm(extra_evidence) if extra_evidence else ""
        self._extra_spaced = _norm_spaced(extra_evidence) if extra_evidence else ""
        self._max_hold = max_hold
        self._buffer = ""
        self._raw: list[str] = []
        #: 全部修补明细（一次回答可能修多处）。
        self.fixes: list[CitationFix] = []

    @property
    def raw_text(self) -> str:
        """**未**修补的原文 —— 给 `audit()` 量"模型错了几次"用。"""
        return "".join(self._raw)

    def feed(self, piece: str) -> str:
        """喂一段增量，返回**可以原样发出去**的文本（可能是空串）。"""
        self._raw.append(piece)
        self._buffer += piece
        out: list[str] = []
        while True:
            cut = self._first_terminator()
            if cut <= 0:
                break
            fixed, fixes = _repair_text(
                self._buffer[:cut],
                self._hits,
                self._contents,
                self._spaced,
                self._extra,
                self._extra_spaced,
            )
            self.fixes.extend(fixes)
            out.append(fixed)
            self._buffer = self._buffer[cut:]
        if len(self._buffer) > self._max_hold:
            out.append(self._buffer)
            self._buffer = ""
        return "".join(out)

    def flush(self) -> str:
        """收尾：把没等到句末的残句发出去（这一刻它已经是最后一句，可以修了）。"""
        if not self._buffer:
            return ""
        fixed, fixes = _repair_text(
            self._buffer,
            self._hits,
            self._contents,
            self._spaced,
            self._extra,
            self._extra_spaced,
        )
        self.fixes.extend(fixes)
        self._buffer = ""
        return fixed

    def _first_terminator(self) -> int:
        for index, char in enumerate(self._buffer):
            if char in _TERMINATORS:
                return index + 1
        return 0


__all__ = [
    "CitationAudit",
    "CitationFix",
    "CitationIssue",
    "CitationRepair",
    "StreamingRepair",
    "audit",
    "repair",
]
