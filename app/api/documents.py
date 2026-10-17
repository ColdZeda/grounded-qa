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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app.api.deps import AdminUser, LimitsRedisDep, SessionDep
from app.core.config import get_settings
from app.core.limits import LimitGuard, ensure
from app.db.models.knowledge import Document, DocumentStatus
from app.db.repository import TenantScopedRepository
from app.kb.ingest import is_supported, prepare_document
from app.kb.text import TEXT_SUFFIXES
from app.kb.uploads import remove_upload, write_upload
from app.schemas.document import DocumentListOut, DocumentOut, DocumentUploadOut
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
    current_admin: AdminUser,
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
    items = [
        DocumentOut.model_validate(item) for item in await repo.list(limit=limit, offset=offset)
    ]
    return DocumentListOut(total=total, items=items)
