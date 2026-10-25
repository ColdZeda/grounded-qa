"""`seed` 的管理员密码闸（2026-10-25 从 `Settings` 启动校验**挪过来**的）。

⚠️ 为什么闸在这里：`ADMIN_PASSWORD` 只在"库里还没有这个邮箱"时被读一次，
    **跑流量时一次都不读**。挂在启动校验上的代价实测过 —— `.env` 里留空 →
    api crash-loop → `depends_on: service_healthy` 把 nginx 拖住 → 整站连接被拒，
    而**回滚救不了**（回滚换镜像，`.env` 不在版本控制里）。
    ⟹ 站点照常起（只告警），建库这一步**响亮地**失败。文件里守的就是这个"响亮"。

这类测试的价值不在于"函数返回对不对"，而在于**保证那道闸不会被悄悄拆掉**：
删掉它不会有任何功能测试变红，只会在某天生产上多出一个**空密码的管理员**。
"""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_ADMIN_PASSWORD
from app.db.seed import _guard_admin_password


def test_guard_rejects_empty_password() -> None:
    """空字符串和纯空格都算空 —— 用空密码建管理员等于没有密码。"""
    with pytest.raises(SystemExit, match="ADMIN_PASSWORD 是空的"):
        _guard_admin_password("")
    with pytest.raises(SystemExit, match="ADMIN_PASSWORD 是空的"):
        _guard_admin_password("   ")


def test_guard_rejects_default_password() -> None:
    with pytest.raises(SystemExit, match="仍是默认值"):
        _guard_admin_password(DEFAULT_ADMIN_PASSWORD)


def test_guard_accepts_any_real_password() -> None:
    """不判强度（那是运营的事），只判"填了、且不是默认值"。"""
    _guard_admin_password("a-real-one-0123456789")
    _guard_admin_password("x")
