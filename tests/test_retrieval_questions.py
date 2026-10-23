"""检索题集的**机器自检**：`must_hit` 标错了，这里必须先红。

⚠️ 为什么需要它：`must_hit` 是评测的靶纸。标成一篇**压根没写这个答案**的文档，
   那题永远 0 分，看起来却像"检索能力差"——**不报错、只是分数悄悄失真**。
   v1.4 B2 起每道新题都带 `evidence:`（答案在文档里的那句原文），这里逐条断言它真的在文里。

判据：
1. id 连续（ret-01…ret-NN），问句不重复；
2. 每条 `must_hit` 路径在 `evals/corpus/senyu-home/` 下**真实存在**；
3. 有 `evidence` 的题：那句话必须出现在**某个** `must_hit` 文档里（逐字）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = REPO_ROOT / "evals" / "questions" / "retrieval.yaml"
CORPUS = REPO_ROOT / "evals" / "corpus" / "senyu-home"


def _items() -> list[dict]:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def _corpus_text(name: str) -> str:
    path = CORPUS / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_ids_are_continuous_and_questions_are_unique() -> None:
    items = _items()
    assert [item["id"] for item in items] == [f"ret-{n:02d}" for n in range(1, len(items) + 1)]
    questions = [item["question"] for item in items]
    assert len(questions) == len(set(questions)), "有重复问句"


def test_every_must_hit_path_exists() -> None:
    missing = [
        (item["id"], name)
        for item in _items()
        for name in item["must_hit"]
        if not (CORPUS / name).exists()
    ]
    assert missing == []


def test_new_questions_are_annotated_with_evidence() -> None:
    """v1.4 B2 新增的题必须带 `evidence` —— 光有 `must_hit` 是一句**无法核对的主张**。"""
    without = [
        item["id"] for item in _items() if item["id"] >= "ret-26" and not item.get("evidence")
    ]
    assert without == []


@pytest.mark.parametrize("item", _items(), ids=lambda item: item["id"])
def test_evidence_really_appears_in_a_must_hit_document(item: dict) -> None:
    evidence = item.get("evidence")
    if not evidence:
        pytest.skip("老题（v1.4 B2 之前）没有登记 evidence")
    texts = [_corpus_text(name) for name in item["must_hit"]]
    assert any(evidence in text for text in texts), (
        f"{item['id']} 的 evidence {evidence!r} 在 {item['must_hit']} 里找不到 —— "
        "要么 must_hit 标错了，要么 evidence 抄错了"
    )
