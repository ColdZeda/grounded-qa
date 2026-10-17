"""入库：把一份文本变成库里的 `Document` ＋ `Chunk`。

**两个调用方，同一条路**：
  · 离线 CLI（`scripts/kb/ingest.py`）—— 灌语料
  · mini worker（`app/workers/main.py`）—— 处理网页上传

## 为什么拆成"准备"和"处理"两步

    prepare_document()   算 hash → 判重 → 建一行 `Document(status=PENDING)`
    process_document()   切分 → 嵌入 → 写块 → 改 `status`

上传接口**只做 prepare**，然后把 `document_id` 丢进队列就返回 —— 请求路径上
不碰 embedding API（那是秒级、还会失败的外部调用）。worker 拿到 id 再做 process。

⚠️ 于是**文件字节必须自己找个地方待着**（`gq-uploads` 卷），
    因为 `Document` 表里只存元数据。见 `app/api/documents.py`。

## 幂等

判重按 **`(tenant_id, content_hash)`** —— 与 `documents` 表的唯一约束**同一个口径**。
不按文件名判重是因为：同一个文件改个名字再传一遍，按名字看是"新文档"，
但唯一约束会直接把它顶成 IntegrityError（500），用户看到的是"服务器错误"。

⚠️ 同名文档**仍然单独处理**（`replace`）：语料文件**内容改过**时，hash 变了，
    按 hash 判重会认为它是新文档，旧的那份**留在库里继续被检索到** ——
    于是同一件事有两个版本，答案看运气。所以 CLI 重跑要能按**文件名**换掉旧版。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.kb.text import TEXT_SUFFIXES, chunk_bytes
from app.rag.providers import EmbeddingClient

logger = logging.getLogger("grounded.kb")

#: 一次 embedding 请求带多少条。
#: ⚠️ 各家 API 都有批量上限（百炼 `text-embedding-v3` 大约 10~25），
#:    取 10 是保守值 —— 宁可多几个请求，也不要一次 400 之后去猜上限是多少。
EMBED_BATCH = 10

#: 进度回调：(已完成, 总数)。
ProgressFn = Callable[[int, int], None]


class IngestError(RuntimeError):
    """入库失败。

    ⚠️ 抛之前**必须已经把 `Document.status` 落成 `FAILED` 并写了 `error_message`**。
        否则用户界面上那个文档会永远转圈，而且没有任何线索。
    """


class NothingToIngest(IngestError):
    """清洗 + 切分之后一块都没有（空文件、或者整篇都是被清掉的标记）。"""


class TooManyChunks(IngestError):
    """切出来的块数超过 `max_chunks_per_document`。见 `config.py` 里为什么是"失败"。"""


def sha256_hex(data: bytes) -> str:
    """文档指纹。**用 sha256 不用 md5** —— md5 已被证明可构造碰撞。"""
    return hashlib.sha256(data).hexdigest()


def guess_content_type(filename: str) -> str:
    """按后缀猜 MIME。够用就行，不引 `mimetypes` 那一套。"""
    lowered = filename.lower()
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    return "text/plain"


def is_supported(filename: str) -> bool:
    """文件名后缀是不是我们吃得下的文本类。"""
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in TEXT_SUFFIXES)


# ------------------------------------------------------------
# 第一步：准备一行 Document
# ------------------------------------------------------------
@dataclass(frozen=True)
class Prepared:
    """`prepare_document` 的结果。"""

    document: Document
    created: bool
    note: str = ""


async def prepare_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    data: bytes,
    replace: bool = False,
) -> Prepared:
    """判重 + 建行（`status=PENDING`）。**不切分、不嵌入。**

    ⚠️ `tenant_id` 是**必传的位置性参数**，调用方只能从服务端解析出的身份里拿
        （路由里是 `current_admin.tenant_id`）—— 绝不能来自请求体。

    ⚠️ **失败过的文档一律可以被顶掉**（不管按名字还是按 hash 撞上）：
        否则用户改完文件重传、或者纯粹想重试，都会被一句"已入库过"挡住 ——
        而它其实**根本没进库**，那条记录唯一的作用就是说明"上次为什么没成"。
        **`PENDING` / `PROCESSING` 不顶**：那是真的在处理中，再排一遍等于白烧一次 embedding。
    """
    content_hash = sha256_hex(data)

    # ① 同名文档 —— 这是"内容改过了要重灌"的那条路
    existing = await session.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.filename == filename)
    )
    if existing is not None and existing.status is DocumentStatus.FAILED:
        # ⚠️ **失败过的同名文件不挡路**（见 docstring）：它其实根本没进库，
        #    用户重传就是在重试。挡掉等于"你永远重试不了"。
        await _forget(session, existing)
        existing = None
    if existing is not None:
        if not replace:
            return Prepared(existing, created=False, note="同名文档已入库过（要重来需显式替换）")
        await _forget(session, existing)

    # ② 同内容文档 —— 与表的唯一约束同口径，避免撞成 IntegrityError（500）
    duplicate = await session.scalar(
        select(Document).where(
            Document.tenant_id == tenant_id, Document.content_hash == content_hash
        )
    )
    if duplicate is not None:
        if duplicate.status is not DocumentStatus.FAILED:
            return Prepared(duplicate, created=False, note="同一份内容已经在库里（可能叫别的名字）")
        # "重传即重试"——见 docstring
        await _forget(session, duplicate)

    document = Document(
        tenant_id=tenant_id,
        filename=filename,
        content_type=guess_content_type(filename),
        size_bytes=len(data),
        content_hash=content_hash,
        # ⚠️ 先落 PENDING 并**提交**：这样"文档在库里"这件事在算向量之前就成立，
        #    失败时还能把它标成 FAILED 被列表看见（状态机见 knowledge.py 的 docstring）。
        status=DocumentStatus.PENDING,
    )
    session.add(document)
    await session.commit()
    return Prepared(document, created=True)


async def _forget(session: AsyncSession, document: Document) -> None:
    """删掉一行文档（连带它的块）。

    ⚠️ **必须用 Core 的 delete()，不能用 `session.delete(obj)`。**
        `Document.chunks` 是 `lazy="raise"`（AGENTS §7.7），ORM 级联删除会先去
        加载那个集合 → 直接抛 LazyLoadError。走 Core 语句则由数据库的
        `ON DELETE CASCADE` 负责删 chunks。
    """
    await session.execute(delete(Document).where(Document.id == document.id))
    await session.commit()


# ------------------------------------------------------------
# 第二步：切分 → 嵌入 → 写块
# ------------------------------------------------------------
async def _embed_all(
    embedder: EmbeddingClient, texts: list[str], *, on_progress: ProgressFn | None = None
) -> list[list[float]]:
    """分批嵌入。顺序与输入一致（`EmbeddingClient.embed` 已按 `index` 排序）。"""
    vectors: list[list[float]] = []
    total = len(texts)

    for start in range(0, total, EMBED_BATCH):
        vectors.extend(await embedder.embed(texts[start : start + EMBED_BATCH]))
        if on_progress is not None:
            on_progress(min(start + EMBED_BATCH, total), total)

    if len(vectors) != total:
        # ⚠️ 条数对不上就**必须炸**：少一条会让后续 zip 整体错位，
        #    向量和文本就配错了 —— 那是最难查的一类脏数据。
        raise RuntimeError(f"嵌入条数对不上：期望 {total}，实际 {len(vectors)}")
    return vectors


async def process_document(
    session: AsyncSession,
    *,
    document: Document,
    data: bytes,
    embedder: EmbeddingClient | None,
    settings: Settings,
    on_progress: ProgressFn | None = None,
) -> int:
    """切分 → 嵌入 → 写 `chunks` → 把状态改成 `READY`。返回块数。

    ⚠️ 任何失败都会把 `status` 落成 `FAILED` ＋ `error_message` **然后才抛出**。
        静默失败在这个功能里是最坏的结果：用户传了文件、界面显示成功、
        而知识库里什么都没有 —— 之后问什么都拒答，还找不到原因。
    """
    if document.status is DocumentStatus.READY:
        # 重复消息（比如队列被重放）不该把已经好的文档再算一遍
        logger.info("文档已是 READY，跳过：%s", document.id)
        return 0

    document.status = DocumentStatus.PROCESSING
    document.error_message = None
    await session.commit()

    pieces = chunk_bytes(data, size=settings.chunk_size, overlap=settings.chunk_overlap)
    if not pieces:
        return await _fail(session, document, NothingToIngest("清洗 + 切分之后没有可入库的内容"))
    if len(pieces) > settings.max_chunks_per_document:
        return await _fail(
            session,
            document,
            TooManyChunks(
                f"切出 {len(pieces)} 块，超过上限 {settings.max_chunks_per_document}；请拆分后再传"
            ),
        )

    if embedder is None:
        # ⚠️ 上传路径**不接受"没有向量"**（离线 CLI 的 `--no-embed` 是另一回事）：
        #    用户以为传进知识库了，实际只进了全文检索路 —— 这是**静默降级**。
        return await _fail(
            session, document, IngestError("没有配置 EMBEDDING_API_KEY，无法生成向量")
        )

    try:
        vectors = list(await _embed_all(embedder, pieces, on_progress=on_progress))
    except Exception as exc:
        return await _fail(session, document, exc)

    # ⚠️ 先清旧块再写：同一条消息被处理两次时，`(document_id, ordinal)` 的唯一约束
    #    会直接报 IntegrityError —— 而"重跑 = 覆盖"才是我们要的语义。
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))

    session.add_all(
        [
            Chunk(
                tenant_id=document.tenant_id,
                document_id=document.id,
                ordinal=ordinal,
                content=piece,
                embedding=vector,
                # 元数据锁的两半，和向量一起写（AGENTS §7.1 约定 3）。
                embedding_model=embedder.model,
                embedding_dim=embedder.dim,
            )
            for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
        ]
    )
    document.status = DocumentStatus.READY
    document.error_message = None
    await session.commit()
    return len(pieces)


async def _fail(session: AsyncSession, document: Document, exc: Exception) -> int:
    """把失败**落库**再抛。见 `process_document` 的说明。"""
    message = f"{type(exc).__name__}: {exc}"[:1000]
    document.status = DocumentStatus.FAILED
    document.error_message = message
    await session.commit()
    logger.warning("入库失败 doc=%s：%s", document.id, message)
    if isinstance(exc, IngestError):
        raise exc
    raise IngestError(message) from exc


# ------------------------------------------------------------
# CLI 的便利入口：准备 + 处理
# ------------------------------------------------------------
@dataclass
class IngestResult:
    """一个文件的入库结果。**给命令行看的**（worker 侧靠异常，不靠返回值）。"""

    filename: str
    status: str  # ok / skip / fail
    chunks: int = 0
    note: str = ""


async def ingest_bytes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    data: bytes,
    embedder: EmbeddingClient | None,
    settings: Settings,
    replace: bool = False,
    on_progress: ProgressFn | None = None,
) -> IngestResult:
    """判重 → 切分 → 嵌入 → 写库。CLI 每个文件调一次。"""
    prepared = await prepare_document(
        session, tenant_id=tenant_id, filename=filename, data=data, replace=replace
    )
    if not prepared.created:
        return IngestResult(filename, "skip", note=prepared.note)

    try:
        count = await process_document(
            session,
            document=prepared.document,
            data=data,
            embedder=embedder,
            settings=settings,
            on_progress=on_progress,
        )
    except IngestError as exc:
        return IngestResult(filename, "fail", note=str(exc)[:150])
    return IngestResult(filename, "ok", chunks=count)
