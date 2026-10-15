"""知识库入库 —— **离线最小版**。

用法：

    uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi --dry-run
    uv run python scripts/kb/ingest.py --kb fastapi --path ../assets/knowledge-base/fastapi

⚠️ **这是"最小可用"，刻意不做这些**（它们属于 v1.0 第 3 项的 Web / 异步部分）：

    · **不做上传 UI** —— 命令行跑，够用就行
    · **不走 Redis 队列 / mini worker** —— `app/workers/main.py` 的 `handle_job` 仍是 TODO
    · **不解析 PDF / docx** —— 只吃纯文本 / markdown
    · **不做增量** —— 改了文件要 `--replace` 重跑

它解决的事情只有一件：**让检索链路有东西可检索**。
没有语料时页面问什么都只能拒答，引用溯源和逐字流式这些最该展示的东西是隐形的。

---

⚠️ **语料定位（AGENTS §7.1 约定 1）**：本脚本灌进来的是**公开文档**，属于**知识库 A**。
    A 库**不能拿来当成绩** —— 模型本来就"背过"这些内容，检索到没检索到都能答对。
    A 库只用于**验证链路 ＋ 撑起演示**；成绩必须来自**知识库 B（自造语料）**。

## 语料怎么来的（可复现）

`..\\assets\\` 不进 git，所以取语料的清单固化在本文件末尾的 `CORPUS_FILES`，取法是：

```powershell
$base = 'https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs'
foreach ($f in $CORPUS_FILES) {
    Invoke-WebRequest -Uri "$base/$f" -OutFile (Join-Path $dest ($f -replace '/', '__'))
}
```

## 幂等

按 `(tenant_id, content_hash)` 判重，`content_hash = sha256(文件原始字节)` —— 与
`app/db/models/knowledge.py` 里写明的口径一致。所以：

    · 同一个文件跑两遍 → 第二遍跳过
    · 文件内容改了 → **算一份新文档**（旧的那份要 `--replace` 或手工删）

## ⚠️ 还没有 embedding key 时怎么办

用 `--no-embed` 先把文本落库。`chunks.embedding` **本来就是可空的**
（`knowledge.py`：入库分「先落文本、再算向量」两阶段），所以这是一个**设计内的状态**：

    · **全文检索路**（pg_trgm `word_similarity`）**不需要向量**，照样能召回
    · **向量检索路**需要向量 —— 这条路此时是空的

补向量的唯一办法是配好 `EMBEDDING_API_KEY` 之后 **`--replace` 重跑**。
⚠️ 不加 `--replace` 的话，文档会被判成"已入库过"直接跳过 ——
**向量永远补不上，而且不报错**（脚本会在结束时把这句话再打印一遍）。

## 结构：为什么同步和异步分得这么开

    · **同步**：参数解析、找文件、dry-run（纯本地磁盘）
    · **异步**：连库、调 embedding API

不是洁癖 —— ruff 的 `ASYNC240` 会拦住"在协程里做阻塞文件 I/O"。
那个规则是对的：`path.read_bytes()` 在事件循环里跑，会把同一循环上的其他任务一起卡住。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.models.tenant import Tenant
from app.db.session import dispose_engine, get_session_factory
from app.rag.pipeline import build_clients
from app.rag.providers import EmbeddingClient, aclose_http_client

#: 能吃进来的后缀。**故意只留文本类** —— PDF/docx 要引入解析库，是第 3 项的事。
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})

#: 一次 embedding 请求带多少条。
#: ⚠️ 各家 API 都有批量上限（百炼 `text-embedding-v3` 大约 10~25），
#:    取 10 是保守值 —— 宁可多几个请求，也不要一次 400 之后去猜上限是多少。
EMBED_BATCH = 10


# ------------------------------------------------------------
# 文本清洗
# ------------------------------------------------------------
#: `## 标题 { #anchor }` —— mkdocs 的锚点，对读者和模型都是噪音
ANCHOR_RE = re.compile(r"\s*\{\s*#[^}]*\}\s*$")
#: `{* ../../docs_src/dependencies/tutorial001.py hl[8:9] *}` —— 代码**引用指令**
CODE_INCLUDE_RE = re.compile(r"^\{\*\s*([^\s*]+)[^*]*\*\}\s*$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
#: 终端输出示例里用来**上色**的标签，形如 `<span style="color: green;">INFO</span>`。
#: ⚠️ **这些要连代码块一起清**（用法见 `clean_markdown`）：
#:    它们不是代码，是文档站点渲染终端配色的痕迹。留着的话，引用摘要里会出现
#:    `<span style="color: green;">INFO</span>: Uvicorn running on ...` 这种东西。
PRESENTATION_TAG_RE = re.compile(r"</?(?:span|font|b|u)\b[^>]*>", re.IGNORECASE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
#: 斜体。`(?!\s)` 是为了**不误吃列表符号** —— `* 项目` 里的星号后面是空格。
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
BLANK_RUN_RE = re.compile(r"\n{3,}")


def clean_markdown(raw: str) -> str:
    """把 mkdocs 风格的 markdown 洗成**纯文本**。

    **为什么必须洗**：`chunks.content` 会被两处消费 ——
      ① 喂给模型当上下文；② **直接当引用摘要显示在页面上**
         （`prompt.make_snippet()` 只截断，不做格式转换）。
    不洗的话，用户会在引用卡片里看到 `**FastAPI**`、`{ #dependencies }`、
    `{* ../../docs_src/... *}` 这些东西 —— 前端也**不渲染 markdown**（全篇不用 `v-html`）。

    ⚠️ **只清洗代码块之外的行。** 代码里的 `#` 是注释、`[]` 是下标、`*` 是乘号 ——
        一律重写会把示例代码改坏。围栏内部**原样保留**。
    """
    out: list[str] = []
    in_fence = False

    for line in raw.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue  # 去掉围栏标记本身，代码内容留着
        if in_fence:
            # ⚠️ 代码块整体**原样保留**，但"上色标签"例外 —— 见 PRESENTATION_TAG_RE
            out.append(PRESENTATION_TAG_RE.sub("", line))
            continue

        stripped = line.strip()

        # `/// note | 标题` … `///` 是 mkdocs 的 admonition：标记行去掉、内容留着
        if stripped.startswith("///"):
            continue

        include = CODE_INCLUDE_RE.match(stripped)
        if include:
            # 代码引用指向的文件**没下载**，所以只留一句可读的提示。
            # 原样留着的话，引用摘要里会出现一串 `../../docs_src/... hl[8:9]`。
            out.append(f"[示例代码：{Path(include.group(1)).name}]")
            continue

        line = IMAGE_RE.sub("", line)
        line = LINK_RE.sub(r"\1", line)  # 只留链接文字，URL 是噪音
        line = HTML_TAG_RE.sub("", line)
        line = ANCHOR_RE.sub("", line)
        line = line.replace("`", "")
        line = BOLD_RE.sub(r"\1", line)
        line = ITALIC_RE.sub(r"\1", line)
        out.append(line.rstrip())

    return BLANK_RUN_RE.sub("\n\n", "\n".join(out)).strip()


# ------------------------------------------------------------
# 切分
# ------------------------------------------------------------
@dataclass(frozen=True)
class Section:
    """一个标题下的正文。`heading_path` 形如 `Dependencies > First Steps`。"""

    heading_path: str
    body: str


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def split_sections(text: str) -> list[Section]:
    """按 markdown 标题切段，并记住**标题路径**。

    ⚠️ 要跟踪代码围栏：代码块里的 `# 注释` 不是标题。
       不跟踪的话，Python 代码里的注释会把文档切得稀碎、标题层级也会乱掉。
    """
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (层级, 标题)
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = " > ".join(title for _, title in stack)
            sections.append(Section(heading_path=path, body=body))
        buffer.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            continue

        match = None if in_fence else HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue

        flush()
        level = len(match.group(1))
        title = match.group(2).strip()
        # 同级或更深的标题先弹出去，保证路径是"当前所在的那条链"
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

    flush()
    return sections


def _cut(text: str, *, lo: int, hi: int, forward: bool) -> int | None:
    """在 `[lo, hi)` 里找一个**切点**。

    ⚠️ 分两轮找，顺序很重要：
        ① 先找**换行** —— 段落边界，切在这里上下文最完整；
        ② 找不到再找**空格** —— 至少别把单词劈成两半。
    ⚠️ 为什么需要第 ② 轮：FastAPI 文档**一个自然段就是一行**，常常超过 100 字。
        只认换行的话，`[lo, hi)` 里一个换行都没有，就只能退回"按字符硬切"，
        于是出现 `me package that this module...` 这种**从单词中间开始**的块。
    """
    for char in ("\n", " "):
        index = text.find(char, lo, hi) if forward else text.rfind(char, lo, hi)
        if index != -1:
            return index
    return None


def _windows(text: str, *, size: int, overlap: int) -> list[str]:
    """按字符窗口切，并**尽量在段落/词边界断开**，避免把句子、单词或代码从中间劈开。"""
    if size <= 0:
        raise ValueError("size 必须为正")

    out: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(length, start + size)
        if end < length:
            # 只在窗口后半段找切点：太靠前会导致每块都很短
            cut = _cut(text, lo=start + size // 2, hi=end, forward=False)
            if cut is not None and cut > start:
                end = cut

        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= length:
            break

        # ⚠️ 重叠窗口的**起点**同样要对齐边界。否则下一块会从半句话 / 半个词中间开始 ——
        #    实测见过 `it that is an int, and by default is 100.` 和
        #    `me package that this module ...` 这两种残块开头，
        #    读起来像文本损坏，喂给模型也是垃圾上下文。
        next_start = end - overlap
        if next_start > start:
            cut = _cut(text, lo=next_start, hi=end, forward=True)
            if cut is not None:
                next_start = cut + 1

        # ⚠️ 必须保证**真的前进**，否则窗口原地打转 -> 死循环
        start = next_start if next_start > start else end

    return out


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """切成检索用的块。

    ⚠️ **每块前面都带上标题路径**。这是白捡的检索质量：
        光看"Let's first focus on the dependency." 根本不知道在讲什么；
        加上 `[Dependencies > First Steps]` 之后，**向量和关键词都变得有指向**，
        而且引用摘要自己就说明白了出处。
    """
    chunks: list[str] = []

    for section in split_sections(text):
        prefix = f"[{section.heading_path}]\n" if section.heading_path else ""
        body = section.body.strip()
        if not body:
            continue

        budget = size - len(prefix)
        if budget <= 0:  # 标题路径自己就超长（极深嵌套），退化成不带前缀
            chunks.extend(_windows(body, size=size, overlap=overlap))
            continue

        if len(body) <= budget:
            chunks.append(f"{prefix}{body}".strip())
            continue

        chunks.extend(
            f"{prefix}{piece}".strip() for piece in _windows(body, size=budget, overlap=overlap)
        )

    return [chunk for chunk in chunks if chunk]


def chunk_file(path: Path, *, size: int, overlap: int) -> list[str]:
    """读文件 → 清洗 → 切分。**同步函数**（见模块 docstring 里关于 ASYNC240 的说明）。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    return chunk_text(clean_markdown(raw), size=size, overlap=overlap)


# ------------------------------------------------------------
# 入库
# ------------------------------------------------------------
@dataclass
class Result:
    filename: str
    status: str  # ok / skip / fail
    chunks: int = 0
    note: str = ""


@dataclass
class Summary:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)
        mark = {"ok": "✓", "skip": "–", "fail": "✗"}[result.status]
        extra = f"  （{result.note}）" if result.note else ""
        print(f"  {mark} {result.filename}  [{result.chunks} 块]{extra}")

    def count(self, status: str) -> int:
        return sum(1 for item in self.results if item.status == status)

    @property
    def chunks(self) -> int:
        return sum(item.chunks for item in self.results)


async def _embed_all(embedder: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """分批嵌入。顺序与输入一致（`EmbeddingClient.embed` 已按 `index` 排序）。"""
    vectors: list[list[float]] = []
    total = len(texts)

    for start in range(0, total, EMBED_BATCH):
        vectors.extend(await embedder.embed(texts[start : start + EMBED_BATCH]))
        print(f"    嵌入 {min(start + EMBED_BATCH, total)}/{total}", end="\r", flush=True)

    if vectors:
        print(" " * 28, end="\r")  # 擦掉进度行
    if len(vectors) != total:
        raise RuntimeError(f"嵌入条数对不上：期望 {total}，实际 {len(vectors)}")
    return vectors


async def ingest_one(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: str,
    root: Path,
    path: Path,
    embedder: EmbeddingClient | None,
    settings: Settings,
    replace: bool,
) -> Result:
    """入库一个文件。**单个文件失败不影响其他文件**（调用方兜住异常）。"""
    # ⚠️ 文件读取走 `to_thread`：这是**阻塞 I/O**，直接在协程里跑会卡住事件循环
    #    （ruff ASYNC240 拦的就是这个）。清洗和切分是纯 CPU 的微秒级操作，不值得再开线程。
    data = await asyncio.to_thread(path.read_bytes)
    content_hash = hashlib.sha256(data).hexdigest()
    filename = f"{kb}/{path.relative_to(root).as_posix()}"

    existing = await session.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.filename == filename)
    )
    if existing is not None:
        if not replace:
            return Result(filename, "skip", note="已入库过（要重来加 --replace）")
        # ⚠️ **必须用 Core 的 delete()，不能用 `session.delete(obj)`。**
        #    `Document.chunks` 是 `lazy="raise"`（AGENTS §7.7），ORM 级联删除会先去
        #    加载那个集合 → 直接抛 LazyLoadError。走 Core 语句则由数据库的
        #    `ON DELETE CASCADE` 负责删 chunks。
        await session.execute(delete(Document).where(Document.id == existing.id))
        await session.commit()

    pieces = chunk_text(
        clean_markdown(data.decode("utf-8", errors="replace")),
        size=settings.chunk_size,
        overlap=settings.chunk_overlap,
    )
    if not pieces:
        return Result(filename, "skip", note="洗完之后没有可入库的内容")

    document = Document(
        tenant_id=tenant_id,
        filename=filename,
        content_type="text/markdown" if path.suffix in {".md", ".markdown"} else "text/plain",
        size_bytes=len(data),
        content_hash=content_hash,
        # ⚠️ 先落 PROCESSING 并**提交**：这样"文档在库里"这件事在算向量之前就成立，
        #    失败时还能把它标成 FAILED 被列表看见（状态机见 knowledge.py 的 docstring）。
        status=DocumentStatus.PROCESSING,
    )
    session.add(document)
    await session.commit()

    vectors: list[list[float] | None]
    if embedder is None:
        # `--no-embed`：只落文本，不生成向量。
        # ⚠️ 这是**设计内的状态**，不是残缺 —— `chunks.embedding` 本来就是可空的
        #    （见 `knowledge.py`：入库分「先落文本、再算向量」两阶段）。
        #    全文检索路（pg_trgm）不需要向量，所以这种文档**照样能被检索到**。
        vectors = [None] * len(pieces)
    else:
        try:
            vectors = list(await _embed_all(embedder, pieces))
        except Exception as exc:
            document.status = DocumentStatus.FAILED
            document.error_message = f"{type(exc).__name__}: {exc}"[:1000]
            await session.commit()
            return Result(filename, "fail", note=str(exc)[:120])

    session.add_all(
        [
            Chunk(
                tenant_id=tenant_id,
                document_id=document.id,
                ordinal=ordinal,
                content=piece,
                embedding=vector,
                # 元数据锁的两半，和向量一起写（AGENTS §7.1 约定 3）。
                # 没生成向量时两者都留空 —— 写了反而是在撒谎。
                embedding_model=embedder.model if embedder else None,
                embedding_dim=embedder.dim if embedder else None,
            )
            for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
        ]
    )
    document.status = DocumentStatus.READY
    await session.commit()

    return Result(filename, "ok", chunks=len(pieces))


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
                try:
                    summary.add(
                        await ingest_one(
                            session,
                            tenant_id=tenant.id,
                            kb=args.kb,
                            root=root,
                            path=path,
                            embedder=embedder,
                            settings=settings,
                            replace=args.replace,
                        )
                    )
                except Exception as exc:
                    # ⚠️ 单个文件炸了**不能**终止整批：语料是几十个文件，
                    #    一个坏文件不该让其余的都没入库。但要**如实报出来**。
                    await session.rollback()
                    rel = path.relative_to(root).as_posix()
                    summary.add(
                        Result(filename=f"{args.kb}/{rel}", status="fail", note=str(exc)[:150])
                    )
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
        description="把一批文本/markdown 灌进知识库（离线最小版）",
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


#: 取语料用的文件清单（FastAPI 官方文档，MIT）。
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
