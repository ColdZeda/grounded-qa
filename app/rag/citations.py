"""引用**可核对性**审计：回答里的 `[n]` 到底指对了没有。

⚠️ 为什么需要它（2026-10-23 真机抓到）：

    B2 真机验收时，回答把"布艺与填充件 **6 个月**"标成 `[2]`，而那两行写在 `[3]` 里，
    `[2]` 是另一张**没有任何期限数字**的表。界面有引用、点进去是另一段话 ——
    **不逐条核对就发现不了**。这正是"引用溯源"最隐蔽的失败方式。

⚠️ **本模块只做"抓"，不做"改"。** 三个理由：

    ① 流式回答**已经发出去了**，事后改写等于让用户看到前后两个版本；
    ② 我们的检查器自己也会错 —— 误报时删掉一个**正确**的引用，比串号更糟
       （用户失去核对入口，而且我们没有任何记录）；
    ③ 先能度量，才谈得上修。串号率是 0 还是 12%，决定的修法完全不同。

    ⟹ 产物是 `Answer.extra["citation_audit"]` ＋ 一行 warning ＋ 一个计数器。
       真要改输出，等这个数字攒起来再说（`CANDIDATES.md` 有记账）。

⚠️ 判据刻意宽松（**宁可放过，不可误杀**）：
    · 只核**数字 + 单位**这类"最不可替换"的原子 —— "退货"和"退换"是等价的，
      拿它们判"支不支持"会把正确的引用判成错的；
    · 一句里标了多个编号时，只要原子出现在**其中任一**被引段落里就算通过
      （一句话用两段资料各支撑一半，是合法写法）。
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
    atoms = [_norm(atom) for atom in _ATOM.findall(_MARKER.sub("", sentence))]
    return [atom for atom in atoms if atom and not _is_calendar(atom)]


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
        if extra_evidence:
            # 不占编号（不可被 `[n]` 引用），但**参与核对**。
            pool += _norm(extra_evidence)
        missing = tuple(atom for atom in atoms if atom not in pool)
        if missing:
            issues.append(CitationIssue(markers=markers, atoms=missing, sentence=segment.strip()))

    return CitationAudit(checked=checked, unsupported=len(issues), issues=issues)


__all__ = ["CitationAudit", "CitationIssue", "audit"]
