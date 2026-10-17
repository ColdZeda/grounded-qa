"""知识库入库 —— **离线命令行（灌语料用）**。

用法：

    uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi --dry-run
    uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi

⚠️ **逻辑不在这里**（2026-10-17 已抽出）：清洗/切分在 `app/kb/text.py`，
    判重/写库/嵌入在 `app/kb/ingest.py`。本文件只剩**找文件 + 打印进度**。

    为什么必须抽：入库现在有**两个入口** —— 这个 CLI（灌语料）和 mini worker（处理上传）。
    逻辑留在脚本里的话，worker 只能 `from scripts...` 去 import，而 `scripts/`
    **不在 `app` 包里**（镜像是 `uv sync --no-install-project`），
    那条 import 在容器里必然失败。属于"本地跑得好好的、部署就炸"那一类。

⚠️ **这是"最小可用"，刻意不做这些**（它们由上传 UI ＋ worker 负责）：

    · **不做上传** —— 命令行跑，够用就行
    · **不解析 PDF / docx** —— 只吃纯文本 / markdown（`TEXT_SUFFIXES`）
    · **不做增量** —— 改了文件要 `--replace` 重跑

---

⚠️ **语料定位（AGENTS §7.1 约定 1）**：本脚本灌进来的公开文档属于**知识库 A**。
    A 库**不能拿来当成绩** —— 模型本来就"背过"这些内容，检索到没检索到都能答对。
    A 库只用于**验证链路 ＋ 撑起演示**；成绩必须来自**知识库 B（自造语料）**。

## 语料怎么来的（可复现）

`..\\assets\\` 不进 git，所以取知识库 A 语料的清单固化在本文件末尾的 `CORPUS_FILES`，取法是：

```powershell
$base = 'https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs'
foreach ($f in $CORPUS_FILES) {
    Invoke-WebRequest -Uri "$base/$f" -OutFile (Join-Path $dest ($f -replace '/', '__'))
}
```

知识库 B 的语料**在仓库里**（`repo\\evals\\corpus\\senyu-home`），不需要下载。

## ⚠️ 还没有 embedding key 时怎么办

用 `--no-embed` 先把文本落库。`chunks.embedding` **本来就是可空的**
（`knowledge.py`：入库分「先落文本、再算向量」两阶段），所以这是一个**设计内的状态**：

    · **全文检索路**（pg_trgm `word_similarity`）**不需要向量**，照样能召回
    · **向量检索路**需要向量 —— 这条路此时是空的

补向量的唯一办法是配好 `EMBEDDING_API_KEY` 之后 **`--replace` 重跑**。
⚠️ 不加 `--replace` 的话，文档会被判成"已入库过"直接跳过 ——
**向量永远补不上，而且不报错**（脚本会在结束时把这句话再打印一遍）。

📌 **网页上传那条路不接受 `--no-embed` 这种情况**：用户在界面上传了文件，
   就必须真的能检索到；"只进了全文路"属于静默降级，所以 worker 会直接判失败。

## 结构：为什么同步和异步分得这么开

    · **同步**：参数解析、找文件、dry-run（纯本地磁盘）
    · **异步**：连库、调 embedding API

不是洁癖 —— ruff 的 `ASYNC240` 会拦住"在协程里做阻塞文件 I/O"。
那个规则是对的：`path.read_bytes()` 在事件循环里跑，会把同一循环上的其他任务一起卡住。
所以真正读文件的地方是 `asyncio.to_thread`（在 `app/kb/ingest.py` 的调用方这里）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models.tenant import Tenant
from app.db.session import dispose_engine, get_session_factory
from app.kb.ingest import IngestResult, ingest_bytes
from app.kb.text import TEXT_SUFFIXES, chunk_file
from app.rag.pipeline import build_clients
from app.rag.providers import EmbeddingClient, aclose_http_client


def _print_progress(done: int, total: int) -> None:
    """一行原地覆盖的进度条（批处理时知道它没卡住）。"""
    print(f"    嵌入 {done}/{total}", end="\r", flush=True)


class Summary:
    """跑完之后的汇总。**只负责打印**，不参与入库。"""

    def __init__(self) -> None:
        self.results: list[IngestResult] = []

    def add(self, result: IngestResult) -> None:
        self.results.append(result)
        mark = {"ok": "✓", "skip": "–", "fail": "✗"}[result.status]
        extra = f"  （{result.note}）" if result.note else ""
        print(f"  {mark} {result.filename}  [{result.chunks} 块]{extra}")

    def count(self, status: str) -> int:
        return sum(1 for item in self.results if item.status == status)

    @property
    def chunks(self) -> int:
        return sum(item.chunks for item in self.results)


async def _ingest(args: argparse.Namespace, *, root: Path, files: list[Path]) -> int:
    """真入库。只做异步的事：连库 + 调 embedding。"""
    settings = get_settings()

    embedder: EmbeddingClient | None = None
    if args.no_embed:
        print("embedding：**跳过**（--no-embed）—— 只落文本，向量检索路不可用\n")
    else:
        _chat, built, _reranker = build_clients(settings)
        if built is None:
            print("✗ 没有配置 EMBEDDING_API_KEY，无法生成向量。", file=sys.stderr)
            print("  只想先把文本落库的话用 --no-embed。", file=sys.stderr)
            return 2
        embedder = built
        print(f"embedding：{embedder.model}（dim={embedder.dim}）\n")

    summary = Summary()
    started = time.perf_counter()
    factory = get_session_factory()

    try:
        async with factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == args.tenant))
            if tenant is None:
                print(
                    f"✗ 找不到租户 slug={args.tenant}。先跑 `uv run python -m app.db.seed`。",
                    file=sys.stderr,
                )
                return 2

            for path in files:
                # ⚠️ 文件名带 `kb/` 前缀（如 `senyu/10-退换货/10-退换货总则.md`）：
                #    引用来源里显示的就是它，光看 `10-退换货总则.md` 不知道属于哪个库。
                filename = f"{args.kb}/{path.relative_to(root).as_posix()}"
                try:
                    data = await asyncio.to_thread(path.read_bytes)
                except OSError as exc:
                    summary.add(IngestResult(filename, "fail", note=f"读不了文件：{exc}"))
                    continue

                try:
                    summary.add(
                        await ingest_bytes(
                            session,
                            tenant_id=tenant.id,
                            filename=filename,
                            data=data,
                            embedder=embedder,
                            settings=settings,
                            replace=args.replace,
                            on_progress=_print_progress,
                        )
                    )
                except Exception as exc:
                    # ⚠️ 单个文件炸了**不能**终止整批：语料是几十个文件，
                    #    一个坏文件不该让其余的都没入库。但要**如实报出来**。
                    await session.rollback()
                    summary.add(IngestResult(filename, "fail", note=str(exc)[:150]))
                if embedder is not None:
                    print(" " * 28, end="\r")  # 擦掉进度行
    finally:
        await aclose_http_client()
        await dispose_engine()

    elapsed = time.perf_counter() - started
    print()
    print(
        f"完成：✓ {summary.count('ok')} 个文件 / {summary.chunks} 块"
        f" · – 跳过 {summary.count('skip')} · ✗ 失败 {summary.count('fail')}"
        f" · 耗时 {elapsed:.1f}s"
    )
    if args.no_embed:
        # ⚠️ 说清楚"现在能干什么、不能干什么"，以及**怎么补救** ——
        #    不说的话，下次直接再跑一遍不带 --replace，文档会被判为"已入库过"跳过，
        #    向量就**永远补不上**，而且不报错。
        print(
            "\n⚠️ 本次是 --no-embed：这些 chunk **没有向量**。\n"
            "   · 现在能用的：全文检索路（pg_trgm）\n"
            "   · 现在不能用的：向量检索路、精排（精排要 rerank key）\n"
            "   · 补向量：配好 EMBEDDING_API_KEY 后加 --replace 重跑（**不加会跳过**）"
        )
    return 1 if summary.count("fail") else 0


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="把一批文本/markdown 灌进知识库（离线命令行）",
    )
    parser.add_argument("--kb", required=True, help="知识库名，会作为文件名的前缀（如 fastapi）")
    parser.add_argument("--path", required=True, help="语料目录（递归读取 .md/.markdown/.txt）")
    parser.add_argument(
        "--tenant",
        default=get_settings().default_tenant_id,
        help="租户 slug（默认取配置里的 default_tenant_id）",
    )
    parser.add_argument(
        "--replace", action="store_true", help="同名文档已存在时先删掉重来（默认跳过）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印切分结果，不连库、不调 embedding",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="只落文本、不生成向量（chunks.embedding 留空）。全文路照样能检索；补向量要 --replace 重跑",
    )
    return parser.parse_args(argv)


def _dry_run(root: Path, files: list[Path], settings: Settings) -> int:
    """只看切分效果 —— 不连库、不花钱。**改切分参数时先跑这个。**"""
    print("【dry-run】不写库、不调 embedding。只打印切分结果。\n")
    total = 0
    for path in files:
        pieces = chunk_file(path, size=settings.chunk_size, overlap=settings.chunk_overlap)
        total += len(pieces)
        print(f"  {path.relative_to(root).as_posix()}  →  {len(pieces)} 块")

    print(f"\n合计 {total} 块。")

    if files:
        sample = chunk_file(files[0], size=settings.chunk_size, overlap=settings.chunk_overlap)
        if sample:
            print(f"\n--- 样例（{files[0].name} 第 1 块）---")
            print(sample[0][:600])
    return 0


def main() -> int:
    args = parse_args()

    # ---------- 同步部分：找文件（纯本地磁盘） ----------
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"✗ 目录不存在：{root}", file=sys.stderr)
        return 2

    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES)
    if not files:
        suffixes = "/".join(sorted(TEXT_SUFFIXES))
        print(f"✗ {root} 下没有可入库的文本文件（只吃 {suffixes}）", file=sys.stderr)
        return 2

    settings = get_settings()
    print(f"知识库：{args.kb}")
    print(f"来源：  {root}")
    print(f"文件：  {len(files)} 个")
    print(f"租户：  {args.tenant}")
    print(f"切分：  chunk_size={settings.chunk_size} overlap={settings.chunk_overlap}")
    print()

    if args.dry_run:
        return _dry_run(root, files, settings)

    # ---------- 异步部分：连库 + 调模型 ----------
    return asyncio.run(_ingest(args, root=root, files=files))


#: 取知识库 A（FastAPI 官方文档，MIT）语料用的文件清单。
#: ⚠️ **这是"知识库 A"（公开文档）**，只用于验证链路与演示，**不用于评分**（约定 1）。
#: 更新语料时改这里，并同步改模块 docstring 里的取法。
CORPUS_FILES: tuple[str, ...] = (
    "tutorial/first-steps.md",
    "tutorial/path-params.md",
    "tutorial/query-params.md",
    "tutorial/body.md",
    "tutorial/query-params-str-validations.md",
    "tutorial/body-multiple-params.md",
    "tutorial/bigger-applications.md",
    "tutorial/dependencies/index.md",
    "tutorial/dependencies/classes-as-dependencies.md",
    "tutorial/dependencies/sub-dependencies.md",
    "tutorial/dependencies/dependencies-in-path-operation-decorators.md",
    "tutorial/dependencies/global-dependencies.md",
    "tutorial/dependencies/dependencies-with-yield.md",
    "tutorial/security/index.md",
    "tutorial/security/first-steps.md",
)


if __name__ == "__main__":
    raise SystemExit(main())
