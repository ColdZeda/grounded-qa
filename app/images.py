"""图片的**文件那一半**：校验 / 落盘 / 读回 / 给模型的那份副本。

⚠️ 和 `app/kb/uploads.py` 的区别：那里的字节是**传送带**（worker 处理完就删），
   这里的字节是**附件**（跟着消息长期留着，用户刷新页面还要看得见）。

## 三条约束

⚠️ **① 格式按内容认，不看后缀。** `evil.png` 完全可能是个 HTML；
   按 `UploadFile.content_type` 信更是等于信用户。Pillow 打开失败 = 不是图，直接拒。

⚠️ **② 落盘用 UUID，不用原文件名。** 路径穿越（`../../etc/passwd`）就不可能发生 ——
   文件名是我们自己生成的，用户的原名只进数据库展示。

⚠️ **③ 送模型的是"副本"，不是原图。** 原图保持**上传时的字节**（`sha256` 就是它的哈希），
   另算一份长边 ≤1600 的副本给读图模型。理由与安全边界见 `architecture.md` §9.35。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from app.core.config import get_settings

#: 允许的格式 → `(MIME, 后缀)`。**白名单**而不是黑名单：
#: 图库支持的格式很多（BMP / TIFF / HEIC…），每一种都是一个新的解析面。
ALLOWED_FORMATS: dict[str, tuple[str, str]] = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}

#: 解压炸弹防线：一张 2 MB 的 PNG 能解出几亿像素，把 2C2G 的内存直接吃光。
#: ⚠️ 自己定阈值而不是用 Pillow 默认的 89M —— 我们的场景里超过 4000 万像素
#:    （约 8000×5000）一定不是"手机随手拍"。
MAX_PIXELS = 40_000_000


class InvalidImage(ValueError):
    """不是图片 / 格式不在白名单 / 像素过多。**调用方按 400 处理**。"""


@dataclass(frozen=True)
class ImageMeta:
    """一张图的事实（不含任何业务判断）。"""

    sha256: str
    content_type: str
    suffix: str
    width: int
    height: int
    bytes: int


def images_root() -> Path:
    return Path(get_settings().upload_dir).expanduser() / "images"


def sniff(raw: bytes) -> ImageMeta:
    """按**内容**认格式、取尺寸、算哈希。不认识就抛 `InvalidImage`。

    ⚠️ 用 `PILImage.open` 而不是读 magic bytes 自己判：Pillow 会把容器解析一遍，
        比"前 4 个字节像 PNG"可靠得多（而且它还会给出真实尺寸）。
    ⚠️ 不解码像素（不 `.load()`）—— 那一步才是 CPU 大头，这里只需要头部信息。
        `width*height` 的检查防止后面真正解码时被炸弹打中。
    """
    try:
        with PILImage.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            width, height = probe.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImage("文件看起来不是图片（或已损坏）") from exc
    except PILImage.DecompressionBombError as exc:  # 极端大图，Pillow 自己的防线
        raise InvalidImage("图片尺寸过大") from exc

    if fmt not in ALLOWED_FORMATS:
        raise InvalidImage(f"不支持的图片格式：{fmt or '未知'}（只收 PNG / JPEG / WEBP）")
    if width <= 0 or height <= 0:
        raise InvalidImage("图片尺寸不合法")
    if width * height > MAX_PIXELS:
        raise InvalidImage(f"图片像素过多（{width}×{height}），请先压缩再上传")

    content_type, suffix = ALLOWED_FORMATS[fmt]
    return ImageMeta(
        sha256=hashlib.sha256(raw).hexdigest(),
        content_type=content_type,
        suffix=suffix,
        width=width,
        height=height,
        bytes=len(raw),
    )


def _rel_path(tenant_id: uuid.UUID, image_id: uuid.UUID, meta: ImageMeta) -> str:
    """相对路径：`images/<tenant>/<image_id>/<sha256>.png`。

    ⚠️ 目录带 `image_id` 而不是只带哈希：将来"同一张图被两个用户传"时不会互相覆盖
        （哈希相同的两个文件内容一样，但**归属不同**，删一个不能连累另一个）。
    """
    return f"images/{tenant_id}/{image_id}/{meta.sha256}{meta.suffix}"


async def store(raw: bytes, *, tenant_id: uuid.UUID, image_id: uuid.UUID) -> tuple[str, ImageMeta]:
    """校验 + 落盘，返回 `(相对路径, 元信息)`。

    ⚠️ 目录必须 `mkdir -p`：命名卷的属主来自镜像，父目录不存在时 `write_bytes`
        直接抛 `FileNotFoundError` —— 表现是**本地正常、容器 500**（`AGENTS.md` §10）。
    ⚠️ 走 `to_thread`：`mkdir` / `write_bytes` 都是阻塞 I/O（ruff `ASYNC240`）。
    """
    meta = sniff(raw)
    rel = _rel_path(tenant_id, image_id, meta)
    target = images_root() / rel[len("images/") :]

    def _write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    await asyncio.to_thread(_write)
    return rel, meta


def _resolve(rel_path: str) -> Path:
    """把库里的相对路径解析成绝对路径，**并确保它没跑出 images 根目录**。

    ⚠️ 路径来自数据库，理论上可信；但"理论上"这三个字在安全代码里不值得冒险 ——
        一次误写（或将来的迁移脚本）就能变成任意文件读取。多一次 `is_relative_to` 很便宜。
    """
    root = images_root().resolve()
    target = (root / rel_path.removeprefix("images/")).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"图片路径越界：{rel_path}")
    return target


async def read(rel_path: str) -> bytes:
    """读回原始字节。文件不在就抛 `FileNotFoundError`（调用方按 404/500 处理）。"""
    return await asyncio.to_thread(_resolve(rel_path).read_bytes)


def to_model_copy(raw: bytes) -> tuple[bytes, str]:
    """生成**只给读图模型看**的那份副本，返回 `(字节, MIME)`。

    规则（数据来自 `config.py` 的三个旋钮）：

        · 已经不超限（长边 ≤1600 且 ≤1 MB）→ **原样返回**，一次重编码都不做
        · PNG（截图 / 单据）→ 只缩尺寸，**仍存 PNG**（无损。转 JPEG 会糊掉细笔画字）
        · JPEG / WEBP（照片）→ 缩尺寸 + 重编码 JPEG，先 q88，还超 1 MB 再退一档 q76

    ⚠️ **先做 EXIF 转正**：手机照片常常是"横向存储 + 一个 EXIF 标记说请旋转 90°"。
        不转正，模型看到的是**躺倒的图**，所有文字识别都会崩 —— 而且它不会报错，
        只会给你一段看起来像胡说八道的转写。
    """
    settings = get_settings()
    with PILImage.open(io.BytesIO(raw)) as opened:
        fmt = (opened.format or "").upper()
        is_png = fmt == "PNG"
        width, height = opened.size
        long_edge = max(width, height)
        within_edge = long_edge <= settings.image_model_max_edge
        #: 1 / 缺失 = 不需要旋转。其它值（3/6/8…）表示"文件是横着存的"。
        orientation = opened.getexif().get(0x0112, 1)
        if within_edge and len(raw) <= settings.image_model_max_bytes and orientation in (None, 1):
            return raw, ALLOWED_FORMATS.get(fmt, ("image/png", ""))[0]

        img = ImageOps.exif_transpose(opened)
        if not within_edge:
            scale = settings.image_model_max_edge / long_edge
            img = img.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                PILImage.Resampling.LANCZOS,
            )

        buffer = io.BytesIO()
        if is_png:
            img.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"

        # 照片类：转 RGB（JPEG 不支持透明通道与调色板色）
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        quality = settings.image_model_jpeg_quality
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) > settings.image_model_max_bytes:
            # 还超？再退一档质量。**不做循环**：两档足够，再压就伤内容了。
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=max(60, quality - 12), optimize=True)
            data = buffer.getvalue()
        return data, "image/jpeg"


__all__ = [
    "ALLOWED_FORMATS",
    "MAX_PIXELS",
    "ImageMeta",
    "InvalidImage",
    "images_root",
    "read",
    "sniff",
    "store",
    "to_model_copy",
]
