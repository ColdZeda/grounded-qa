"""图片：**上传** ＋ **取回**（仅本人）。

为什么是两步（上传拿 `image_id`，再带 `image_id` 去提问），而不是把图塞进聊天请求：

    ① 聊天是 SSE（POST + JSON），塞图要么 base64 进 JSON（请求体爆掉、还进日志），
       要么改 multipart —— 那 SSE 的响应和 multipart 的请求凑在一起，很难调试；
    ② 图要**长期留着**（刷新页面还得看得见），它天然属于"先存好、再引用"。

⚠️ **取回接口是必须的**：只有上传没有读取，消息里的缩略图刷新后就没了。
    读的是**别人的图**怎么办？→ 和查订单一样，**一律 404**（403 等于承认这张图存在）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status

from app.api.deps import CurrentUser, LimitsRedisDep, SessionDep
from app.core.config import get_settings
from app.core.limits import LimitGuard, ensure
from app.db.models.image import Image
from app.db.repository import TenantScopedRepository
from app.images import InvalidImage, read, store
from app.schemas.image import ImageOut

logger = logging.getLogger("grounded.images")

router = APIRouter(prefix="/images", tags=["images"])


@router.post(
    "",
    response_model=ImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传一张图（提问时用）",
)
async def upload_image(
    file: Annotated[UploadFile, File(description="PNG / JPEG / WEBP，≤ 3 MB")],
    session: SessionDep,
    current_user: CurrentUser,
    redis: LimitsRedisDep,
) -> ImageOut:
    """收图 → 校验 → 落卷 → 落库。

    ⚠️ **限流复用上传那把尺子**（5 次/分钟/用户）：图片和文档一样是"要落盘的东西"，
        再开一个旋钮只会让配置越来越难说清楚。被限流时返回 429 + 剩余秒数。
    ⚠️ **先限流再读字节**：读 3 MB 的 body 也不便宜，被限流的请求不该让我们先把它读完。
    """
    settings = get_settings()
    guard = LimitGuard(redis, settings)
    ensure(await guard.upload_rate(current_user.id))

    raw = await file.read(settings.image_max_bytes + 1)
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有收到文件内容")
    if len(raw) > settings.image_max_bytes:
        limit_mb = settings.image_max_bytes / 1024 / 1024
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"图片超过 {limit_mb:.0f} MB，请先压缩再上传",
        )

    image_id = uuid.uuid4()
    try:
        rel_path, meta = await store(raw, tenant_id=current_user.tenant_id, image_id=image_id)
    except InvalidImage as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Image)
    image = repo.build(
        id=image_id,
        user_id=current_user.id,
        sha256=meta.sha256,
        path=rel_path,
        content_type=meta.content_type,
        bytes=meta.bytes,
        width=meta.width,
        height=meta.height,
    )
    await repo.add(image)
    await session.commit()

    logger.info(
        "图片已入库 id=%s %s %dx%d %.1f KB",
        image.id,
        meta.content_type,
        meta.width,
        meta.height,
        meta.bytes / 1024,
    )
    return ImageOut.model_validate(image)


@router.get(
    "/{image_id}",
    summary="取回图片字节（仅本人）",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
)
async def get_image(
    image_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Response:
    """把原图字节吐回去（前端 `<img src>` 直接用）。

    ⚠️ `Cache-Control: private`：图是**用户私有**的，不能让中间代理缓存后发给别人。
        （我们前面就一个 nginx，但把话写对不花成本，而且换部署形态时不会出错。）
    """
    repo = TenantScopedRepository(session, tenant_id=current_user.tenant_id, model=Image)
    image = await repo.get(image_id)
    if image is None or image.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片不存在")

    try:
        data = await read(image.path)
    except (FileNotFoundError, ValueError):
        # 库里有行、卷上没文件 —— 数据不一致，属于服务端问题，如实 500。
        logger.warning("图片文件丢失 id=%s path=%s", image.id, image.path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="图片文件丢失"
        ) from None

    return Response(
        content=data,
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )
