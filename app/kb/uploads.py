"""上传文件的**停放目录**（api 写、worker 读）。

⚠️ **这里不是存储，是传送带。**
    真正的长期数据是切好的块（`chunks` 表）。原始文件在 worker 处理完之后就删掉。

## 为什么字节要走磁盘，而不是塞进队列消息

上传接口必须**立刻返回**（不能让用户等 embedding），所以「文件字节」和「处理任务」
被分开了：任务走 Redis，字节留在磁盘上。

    一个 1 MB 的文件 base64 之后是 1.3 MB。塞进 Redis 有两个问题：
      · 要占 Redis 的内存（这台机器只给了 96 MB）；
      · 消息一旦过大，`maxmemory-policy` 会在**没有任何报错**的情况下把它挤掉 ——
        表现是"上传成功、永远 pending"。

## 为什么文件名是 UUID，而不是用户上传的名字

    · **路径穿越直接不存在**：`../../etc/passwd` 之类的名字根本进不了文件系统，
      因为落盘用的名字是我们自己生成的 UUID；
    · 不用处理重名、不用处理奇怪字符、不用处理大小写不敏感的文件系统。

⚠️ 原始文件名**仍然存库**（`documents.filename`）—— 它是给人看的，不是给文件系统看的。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.core.config import get_settings

#: 落盘后缀。故意统一：真实后缀在 `documents.filename` 里，文件系统不需要知道。
UPLOAD_SUFFIX = ".bin"


def upload_dir() -> Path:
    """停放目录（相对路径按进程 cwd 解析：容器里是 `/app/uploads`）。"""
    return Path(get_settings().upload_dir).expanduser()


def upload_path(document_id: uuid.UUID) -> Path:
    """某个文档的字节落在哪。"""
    return upload_dir() / f"{document_id}{UPLOAD_SUFFIX}"


async def write_upload(document_id: uuid.UUID, data: bytes) -> Path:
    """落盘。

    ⚠️ 走 `to_thread`：`mkdir` / `write_bytes` 都是**阻塞 I/O**，
        直接在协程里跑会卡住事件循环（ruff `ASYNC240` 拦的就是这个）。
    """

    def _write() -> Path:
        path = upload_path(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    return await asyncio.to_thread(_write)


async def read_upload(document_id: uuid.UUID) -> bytes:
    """读回字节。文件不在就抛 `FileNotFoundError`（调用方要如实落 `FAILED`）。"""
    return await asyncio.to_thread(upload_path(document_id).read_bytes)


async def remove_upload(document_id: uuid.UUID) -> None:
    """删掉。

    ⚠️ `missing_ok=True`：重复消息会走到这里两次，第二次文件已经不在了 ——
        这不是错误，不该把一条已经成功的任务变成失败。
    """
    await asyncio.to_thread(upload_path(document_id).unlink, True)
