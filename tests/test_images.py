"""图片上传 / 取回 ／ 图预处理。

这里钉四类东西：

    ① **按内容认格式**：后缀和 `content_type` 都是用户说的，不算数
    ② **取回要仅本人**：别人的图一律 404（不是 403 —— 403 等于承认图存在）
    ③ **降采样的三条规则**：小图原样不动 / PNG 不转 JPEG / 照片才转 JPEG
    ④ **EXIF 转正**：手机照片不转正，模型看到的是躺倒的图，而且它不会报错
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path

import pytest
from conftest import TEST_BCRYPT_ROUNDS, TEST_PASSWORD, Account
from httpx import AsyncClient
from PIL import Image as PILImage
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import images as images_mod
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.images import InvalidImage, sniff, to_model_copy

PREFIX = get_settings().api_prefix


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 `upload_dir` 指到临时目录 —— 否则测试会往仓库根的 `uploads/` 里写东西。

    ⚠️ 改的是 `get_settings()` **缓存的那个实例**（配置是进程级单例），
        所以接口和 `app.images` 读到的都是同一个新值。
    """
    target = tmp_path / "uploads"
    monkeypatch.setattr(get_settings(), "upload_dir", str(target))
    return target


def _png(width: int = 64, height: int = 48, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width: int = 3200, height: int = 2400) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), "gray").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _noisy_png(size: int = 1500) -> bytes:
    """一张**压不小**的图：随机噪声连 PNG 都压不动（1500² ≈ 6.7 MB）。

    ⚠️ 必须用噪声：纯色图不管多大，PNG 都能压到几 KB，"3~8 MB 之间能不能过"就测不到。
    """
    raw = os.urandom(size * size * 3)
    buffer = io.BytesIO()
    PILImage.frombytes("RGB", (size, size), raw).save(buffer, format="PNG")
    return buffer.getvalue()


async def _upload(
    client: AsyncClient, account: Account, token: str, *, data: bytes, name: str = "a.png"
) -> object:
    return await client.post(
        f"{PREFIX}/images",
        headers=account.auth(token),
        files={"file": (name, data, "image/png")},
    )


# ---------------------------------------------------------------- 接口


async def test_upload_png_records_meta_and_file(
    client: AsyncClient,
    make_account: object,
    login: object,
    isolated_upload_dir: Path,
) -> None:
    account: Account = await make_account(label="img")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]

    resp = await _upload(client, account, token, data=_png(120, 80))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (120, 80)
    assert body["bytes"] > 0

    # 落盘位置：uploads/images/<tenant>/<image_id>/<sha256>.png
    stored = list((isolated_upload_dir / "images").rglob("*.png"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == _png(120, 80)


async def test_upload_rejects_non_image(
    client: AsyncClient, make_account: object, login: object
) -> None:
    """后缀叫 `.png` 不算数是图片 —— 内容才说了算。"""
    account: Account = await make_account(label="bad")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]

    resp = await _upload(client, account, token, data=b"<html>nope</html>")
    assert resp.status_code == 400, resp.text
    assert "图片" in resp.json()["detail"]


async def test_upload_rejects_unsupported_format(
    client: AsyncClient, make_account: object, login: object
) -> None:
    buffer = io.BytesIO()
    PILImage.new("RGB", (10, 10), "red").save(buffer, format="GIF")
    account: Account = await make_account(label="gif")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]

    resp = await _upload(client, account, token, data=buffer.getvalue(), name="a.gif")
    assert resp.status_code == 400, resp.text
    assert "格式" in resp.json()["detail"]


async def test_upload_enforces_size_limit(
    client: AsyncClient,
    make_account: object,
    login: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "image_max_bytes", 100)
    account: Account = await make_account(label="big")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]

    resp = await _upload(client, account, token, data=_png(200, 200))
    assert resp.status_code == 413, resp.text


def test_image_limit_default_is_8mb() -> None:
    """上限默认 **8 MB**（走查 P1-⑦ 从 3 MB 抬上来）。

    ⚠️ 前端 `web/src/api/images.ts` 的 `IMAGE_MAX_BYTES` 必须和它一致；
        两边都是 8 MB 时，用户看到的第一句话才是准的。
    """
    assert get_settings().image_max_bytes == 8 * 1024 * 1024


async def test_upload_accepts_image_above_old_3mb_limit(
    client: AsyncClient, make_account: object, login: object
) -> None:
    """3~8 MB 的图**现在收得下**（以前会被 413 挡回去）。"""
    raw = _noisy_png()
    assert 3 * 1024 * 1024 < len(raw) <= 8 * 1024 * 1024, (
        f"这张测试图 {len(raw)} 字节不在新旧上限之间，测不到想测的东西"
    )

    account: Account = await make_account(label="six")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]

    resp = await _upload(client, account, token, data=raw)
    assert resp.status_code == 201, resp.text
    assert resp.json()["bytes"] == len(raw)


async def test_get_image_roundtrip(
    client: AsyncClient, make_account: object, login: object
) -> None:
    account: Account = await make_account(label="get")  # type: ignore[operator]
    token = await login(account.email, account.password)  # type: ignore[operator]
    original = _png(40, 30)
    created = await _upload(client, account, token, data=original)
    assert created.status_code == 201
    image_id = created.json()["id"]

    resp = await client.get(f"{PREFIX}/images/{image_id}", headers=account.auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == original
    assert "private" in resp.headers.get("cache-control", "")


async def test_other_user_gets_404(
    client: AsyncClient, session: AsyncSession, login: object
) -> None:
    """⚠️ 同一租户里的**另一个用户**也看不到 —— 图是私有的，不是租户共有的。"""
    tenant = Tenant(slug=f"img-{uuid.uuid4().hex[:8]}", name="图片租户")
    session.add(tenant)
    await session.flush()
    try:

        def _mk(label: str) -> User:
            return User(
                tenant_id=tenant.id,
                email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(TEST_PASSWORD, rounds=TEST_BCRYPT_ROUNDS),
                role=UserRole.ADMIN,
                is_active=True,
            )

        owner, intruder = _mk("owner"), _mk("intruder")
        session.add_all([owner, intruder])
        await session.commit()

        owner_token = await login(owner.email, TEST_PASSWORD)  # type: ignore[operator]
        headers = {"Authorization": f"Bearer {owner_token}"}
        created = await client.post(
            f"{PREFIX}/images",
            headers=headers,
            files={"file": ("a.png", _png(), "image/png")},
        )
        assert created.status_code == 201, created.text
        image_id = created.json()["id"]

        intruder_token = await login(intruder.email, TEST_PASSWORD)  # type: ignore[operator]
        resp = await client.get(
            f"{PREFIX}/images/{image_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert resp.status_code == 404, resp.text
    finally:
        await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()


async def test_images_require_auth(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/images/{uuid.uuid4()}")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------- 纯函数


def test_sniff_reads_format_from_content_not_name() -> None:
    meta = sniff(_png(12, 34))
    assert (meta.content_type, meta.suffix) == ("image/png", ".png")
    assert (meta.width, meta.height) == (12, 34)
    assert len(meta.sha256) == 64


def test_sniff_rejects_garbage() -> None:
    with pytest.raises(InvalidImage):
        sniff(b"definitely not an image")


def test_sniff_rejects_too_many_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images_mod, "MAX_PIXELS", 100)
    with pytest.raises(InvalidImage):
        sniff(_png(20, 20))


def test_model_copy_keeps_small_png_untouched() -> None:
    """小图**一个字节都不该动** —— 重编码只会掉质量，没有任何好处。"""
    raw = _png(200, 150)
    data, mime = to_model_copy(raw)
    assert data == raw
    assert mime == "image/png"


def test_model_copy_downscales_large_photo() -> None:
    raw = _jpeg(3200, 2400)
    data, mime = to_model_copy(raw)
    assert mime == "image/jpeg"
    assert len(data) < len(raw)
    with PILImage.open(io.BytesIO(data)) as out:
        assert max(out.size) <= get_settings().image_model_max_edge


def test_model_copy_keeps_png_as_png_when_downscaling() -> None:
    """⚠️ **PNG 缩完还是 PNG**：截图/单据上的细笔画字在 JPEG 里会糊。"""
    raw = _png(3000, 2000)
    assert len(raw) > 0
    data, mime = to_model_copy(raw)
    assert mime == "image/png"
    with PILImage.open(io.BytesIO(data)) as out:
        assert out.format == "PNG"
        assert max(out.size) <= get_settings().image_model_max_edge


def test_model_copy_applies_exif_orientation() -> None:
    """手机照片常带"请旋转 90°"标记：不转正，模型看到的是躺倒的图。"""
    img = PILImage.new("RGB", (400, 200), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation = 顺时针 90°
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif)
    data, _ = to_model_copy(buffer.getvalue())

    with PILImage.open(io.BytesIO(data)) as out:
        # 400×200 顺时针转 90° 之后应当是 200×400
        assert out.size == (200, 400)


async def test_read_blocks_path_traversal(isolated_upload_dir: Path) -> None:
    (isolated_upload_dir / "images").mkdir(parents=True, exist_ok=True)
    (isolated_upload_dir / "secret.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        await images_mod.read("images/../secret.txt")
