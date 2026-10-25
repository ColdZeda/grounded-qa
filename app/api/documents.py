"""知识库文档：上传 + 列表（**仅管理员**）。

⚠️ **知识库是租户共有的，不是某个人的私有数据** —— 所以上传要管理员。
    普通成员能上传的话，等于任何人都能往所有人的上下文里塞东西
    （这是"提示注入"的一个现实入口，不只是权限问题）。

## 一次上传发生了什么

    ① 校验后缀 / 大小（不引新的解析面：只吃文本类）
    ② 算 sha256 → 判重 → 建一行 `Document(status=PENDING)`      ← 快，毫秒级
    ③ 字节落到共享卷（`gq-uploads`）
    ④ `LPUSH` 一条任务进 Redis，**立刻返回 202**
    ⑤ worker 在另一个容器里：切分 → embedding → 写 `chunks` → 状态改 `READY`

⚠️ **为什么请求路径上不做 embedding**：那是一次秒级的外部 API 调用，
    还会失败。放在请求里意味着用户要盯着转圈、nginx 超时、重试还可能重复入库。
    队列把"接受"和"做完"分开了 —— 界面靠**轮询状态**表达进度。

⚠️ 这里**不检查 embedding key 配没配**：谁有能力做这件事谁判断。
    key 缺失由 worker 判失败并把原因写进 `error_message`，
    界面照样看得见。在这里再抄一份前置条件，两处早晚会漂。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, func, select

from app.api.deps import AdminUser, AdminWriter, CurrentUser, LimitsRedisDep, SessionDep
from app.core.config import get_settings
from app.core.limits import LimitGuard, ensure
from app.db.models.knowledge import Chunk, Document, DocumentStatus
from app.db.repository import TenantScopedRepository
from app.kb.ingest import is_supported, prepare_document
from app.kb.text import TEXT_SUFFIXES
from app.kb.uploads import remove_upload, write_upload
from app.schemas.document import (
    ChunkOut,
    DocumentChunksOut,
    DocumentListOut,
    DocumentOut,
    DocumentUploadOut,
)
from app.workers.queue import IngestJob, enqueue_ingest

logger = logging.getLogger("grounded.documents")

router = APIRouter(prefix="/documents", tags=["documents"])

#: 入队的动作 —— **做成可注入的依赖**（AGENTS §7.1 约定 19）：
#: 测试里要能把它换成假实现，否则每个上传用例都得连一个真 Redis。
EnqueueFn = Callable[[IngestJob], Awaitable[None]]


def get_enqueue() -> EnqueueFn:
    return enqueue_ingest


EnqueueDep = Annotated[EnqueueFn, Depends(get_enqueue)]


def _out(document: Document, *, created: bool, note: str = "") -> DocumentUploadOut:
    """ORM → 出参。

    ⚠️ 先走 `DocumentOut` 再拼两个额外字段：直接拿 `document.__dict__` 展开的话，
        会把 `_sa_instance_state` 这类内部属性一起带到 Pydantic 前面，
        而"哪些字段对外可见"这件事就没人把关了。
    """
    return DocumentUploadOut.model_validate(
        {**DocumentOut.model_validate(document).model_dump(), "created": created, "note": note}
    )


@router.post(
    "",
    response_model=DocumentUploadOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（入队异步入库）",
)
async def upload_document(
    session: SessionDep,
    current_admin: AdminWriter,
    enqueue: EnqueueDep,
    redis: LimitsRedisDep,
    file: Annotated[UploadFile, File(description="文本类文件（.md / .markdown / .txt）")],
) -> DocumentUploadOut:
    """收下文件、排进队列，**立刻返回**。

    ⚠️ 返回 **202**（已接受、还没做完），不是 201（已创建）。
        文档现在只是一行元数据，块还没切、向量还没算 —— 说"创建成功"就是在撒谎。

    ⚠️ **限流这一道放在最前面**，连后缀不对、超大的请求也计数：
        一次上传要跑一遍切分 ＋ N 次 embedding，而且是**队列串行**的 ——
        一个人连传 20 个文件能把队列堵住半小时，别人只能干等。
        挡这种人不该等他"传对了内容"才生效。
    """
    settings = get_settings()

    guard = LimitGuard(redis, settings)
    ensure(await guard.upload_rate(current_admin.id))

    # ⚠️ `Path(...).name`：把客户端可能带上的目录部分**就地丢掉**。
    #    落盘用的是 UUID（见 `app/kb/uploads.py`），但这一列还会显示在引用来源里，
    #    带着 `C:\Users\...` 这种东西没有意义。
    original = Path(file.filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件名不能为空")
    if not is_supported(original):
        suffixes = " / ".join(sorted(TEXT_SUFFIXES))
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"只接受文本类文件（{suffixes}）。PDF / docx 解析还没做。",
        )

    # 先看客户端声明的长度：能挡掉的超大请求不要先落进 /tmp 再判。
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"文件超过上限 {settings.upload_max_bytes // 1024} KB",
        )

    # 多读 1 字节：读满上限还多就说明超了。这样**不依赖客户端声明的长度**。
    data = await file.read(settings.upload_max_bytes + 1)
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"文件超过上限 {settings.upload_max_bytes // 1024} KB",
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件是空的")

    prepared = await prepare_document(
        session,
        tenant_id=current_admin.tenant_id,
        filename=f"upload/{original}",
        data=data,
    )
    document = prepared.document

    if not prepared.created:
        # 判重命中 —— **不是错误**。界面上要说清"为什么没重复入库"。
        return _out(document, created=False, note=prepared.note)

    try:
        await write_upload(document.id, data)
        await enqueue(IngestJob(document_id=document.id, tenant_id=current_admin.tenant_id))
    except Exception as exc:
        logger.exception("入队失败 doc=%s", document.id)
        await remove_upload(document.id)
        # ⚠️ 落成 FAILED 而不是"删掉这行假装没发生"：
        #    用户刚看到的文档不能凭空消失。失败也要能看见、能解释。
        document.status = DocumentStatus.FAILED
        document.error_message = f"入队失败：{type(exc).__name__}: {exc}"[:1000]
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="入库队列不可用，这次上传没排上队（列表里能看到这条失败记录）",
        ) from exc

    logger.info("已入队 doc=%s file=%s bytes=%d", document.id, original, len(data))
    return _out(document, created=True)


@router.get("", response_model=DocumentListOut, summary="文档列表（含入库状态）")
async def list_documents(
    session: SessionDep,
    current_admin: AdminUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListOut:
    """列出**本租户**的文档，按创建时间倒序。

    ⚠️ 隔离靠 `TenantScopedRepository`（它把 `WHERE tenant_id = ?` 焊进查询）。
    ⚠️ 前端靠这个接口**轮询状态**（`pending → processing → ready / failed`），
        所以 `status` 与 `error_message` 必须在出参里 —— 少一个，界面就只能转圈。
    """
    repo = TenantScopedRepository(session, tenant_id=current_admin.tenant_id, model=Document)
    total = await repo.count()
    rows = await repo.list(limit=limit, offset=offset)

    # 每篇切了几块：**一次分组查询**，不要逐个文档 count（那会变成 N+1）。
    # ⚠️ 计数也走仓储的 `select()`，租户过滤只有一个归属地。
    chunk_repo = TenantScopedRepository(session, tenant_id=current_admin.tenant_id, model=Chunk)
    counts: dict[uuid.UUID, int] = {}
    if rows:
        counted = await session.execute(
            chunk_repo.select()
            .where(Chunk.document_id.in_([row.id for row in rows]))
            .with_only_columns(Chunk.document_id, func.count())
            .group_by(Chunk.document_id)
        )
        counts = {document_id: int(count) for document_id, count in counted.all()}

    items = []
    for row in rows:
        item = DocumentOut.model_validate(row)
        item.chunk_count = counts.get(row.id, 0)
        items.append(item)
    return DocumentListOut(total=total, items=items)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除文档")
async def delete_document(
    document_id: uuid.UUID,
    session: SessionDep,
    current_admin: AdminWriter,
) -> None:
    """删除一份文档（连同它的切片和 raw 副本）。

    ⚠️ **硬删、不可恢复** —— 内部工作台不做回收站（2026-10-25 定）。
    ⚠️ 越权 / 不存在一律 **404**（不给 403，否则可枚举 id）。
    ⚠️ 顺序是**先删行、再删文件**：行没删掉却把文件删了，库里会留一条"永远修不好"的文档；
       反过来最坏只是留一个孤儿文件（下面 `remove_upload` 自己吞掉异常）。
    """
    repo = TenantScopedRepository(session, tenant_id=current_admin.tenant_id, model=Document)
    document = await repo.get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    # `chunks.document_id` 上是 `ON DELETE CASCADE` ⟹ 切片由数据库一起删（不用 ORM 级联，
    # 那些关系是 `lazy="raise"`，走 ORM 反而会抛"未加载"）。
    await session.execute(
        delete(Document).where(
            Document.id == document_id, Document.tenant_id == current_admin.tenant_id
        )
    )
    await session.commit()
    await remove_upload(document_id)
    logger.info("文档已删除 document=%s file=%s", document_id, document.filename)


@router.get(
    "/{document_id}/chunks",
    response_model=DocumentChunksOut,
    summary="某一块的上下文（引用核对用）",
)
async def chunk_context(
    document_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    around: Annotated[int, Query(ge=0, description="要看的块序号（0 开始）")] = 0,
    window: Annotated[int, Query(ge=0, le=5, description="前后各取几块")] = 1,
) -> DocumentChunksOut:
    """取**某一块 ＋ 前后各 `window` 块**，用于在引用卡片上"看原文"。

    ## 为什么需要它

    「参考来源」只有 160 字摘要（`make_snippet` 截断的）—— 知道"出自哪"，
    但**判断不了"有没有断章取义"**、也看不出表格有没有被切断。
    回答可核对是本项目的深度锚（幻觉治理），所以这一屏必须能点开。

    ## 三条刻意的限制

    ⚠️ ① **只给相邻几块，不给整篇**：流量是一方面，更重要的是"你看到的是片段"
        这件事要一直成立 —— 返回全文会让人以为那就是全部。
    ⚠️ ② **权限是 `CurrentUser` 而不是管理员**：核对引用是**每个提问者**的基本能力，
        只给管理员的话，普通成员点了只会看到 403。文档**列表**仍然只给管理员。
    ⚠️ ③ **越权一律 404**（不走 403）：不能让人用 404/403 的差别去枚举 document_id。
    """
    doc_repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Document)
    document = await doc_repo.get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    chunk_repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Chunk)
    scoped = chunk_repo.select().where(Chunk.document_id == document_id)
    # ⚠️ 用仓储的 `select()` 包一层子查询来数总数，而不是自己手写
    #    `WHERE tenant_id = ...` —— 租户过滤只允许有一个归属地（`TenantScopedRepository`）。
    total = int(await session.scalar(select(func.count()).select_from(scoped.subquery())) or 0)
    if total == 0:
        return DocumentChunksOut(
            document_id=document.id,
            filename=document.filename,
            ordinal=0,
            total=0,
            window=window,
            items=[],
        )

    hit = min(max(around, 0), total - 1)
    low, high = max(0, hit - window), min(total - 1, hit + window)
    rows = (
        await session.scalars(
            scoped.where(Chunk.ordinal.between(low, high)).order_by(Chunk.ordinal)
        )
    ).all()

    return DocumentChunksOut(
        document_id=document.id,
        filename=document.filename,
        ordinal=hit,
        total=total,
        window=window,
        items=[
            ChunkOut(ordinal=row.ordinal, content=row.content, hit=row.ordinal == hit)
            for row in rows
        ],
    )
