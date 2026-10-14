"""配置层的安全闸门。

⚠️ 这类测试的价值不在于"函数返回对不对"，而在于**保证那道闸不会被悄悄拆掉**。
    "生产不许用默认密钥启动"是一条**安全不变量** —— 拆掉它不会有任何功能测试变红，
    只会在某天被人扫到 `dev-only-...` 的 JWT 密钥为止。
"""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_ADMIN_PASSWORD, DEV_JWT_SECRET, Settings

STRONG_SECRET = "s" * 48
STRONG_PASSWORD = "a-real-password"


def test_dev_environment_tolerates_default_secrets() -> None:
    """本地开发**零摩擦** —— 不许逼开发者也去生成密钥。"""
    settings = Settings(app_env="dev")

    assert settings.jwt_secret == DEV_JWT_SECRET
    assert settings.admin_password == DEFAULT_ADMIN_PASSWORD


def test_production_rejects_development_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(app_env="prod", jwt_secret=DEV_JWT_SECRET, admin_password=STRONG_PASSWORD)


def test_production_rejects_short_jwt_secret() -> None:
    """HS256 的密钥短于 32 字节不行（RFC 7518 §3.2）。"""
    with pytest.raises(ValueError, match="32 字节"):
        Settings(app_env="prod", jwt_secret="too-short", admin_password=STRONG_PASSWORD)


def test_production_rejects_default_admin_password() -> None:
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        Settings(app_env="prod", jwt_secret=STRONG_SECRET, admin_password=DEFAULT_ADMIN_PASSWORD)


def test_production_accepts_properly_configured_secrets() -> None:
    settings = Settings(app_env="prod", jwt_secret=STRONG_SECRET, admin_password=STRONG_PASSWORD)

    assert settings.jwt_secret == STRONG_SECRET


def test_default_jwt_secret_meets_hs256_minimum_length() -> None:
    """默认密钥长度必须 ≥ 32 字节，否则 PyJWT 会对**每一次**签发发警告。

    ⚠️ 这条测试锁的是一个具体的坑：原先默认值是 24 字节，
        测试输出里刷了一屏 `InsecureKeyLengthWarning` —— 而警告刷多了就没人看了。
    """
    assert len(DEV_JWT_SECRET.encode("utf-8")) >= 32
