"""纯单元测试：密码哈希 + JWT 签发校验。

⚠️ **不碰数据库、不发网络请求** —— 所以 `uv run pytest` 在任何机器上都能跑。
"""

from __future__ import annotations

import uuid

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    BCRYPT_MAX_PASSWORD_BYTES,
    PasswordTooLongError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

# 测试用最低轮数（4）。生产默认 12 —— 这里被测的是逻辑，不是暴力破解成本。
ROUNDS = 4


# 密码哈希


def test_hash_then_verify_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple", rounds=ROUNDS)

    assert hashed != "correct horse battery staple"  # 绝不能存明文
    assert verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_is_rejected() -> None:
    hashed = hash_password("right-password", rounds=ROUNDS)

    assert verify_password("wrong-password", hashed) is False


def test_same_password_hashes_differently_each_time() -> None:
    """加盐的证据：同样输入两次，哈希串必须不同。

    ⚠️ 如果这条挂了，说明盐没生效 —— 那样"两个用户密码相同"会**一眼看出来**，
        而且一张彩虹表就能同时破解所有人。
    """
    first = hash_password("same-password", rounds=ROUNDS)
    second = hash_password("same-password", rounds=ROUNDS)

    assert first != second
    # 但两者都能校验通过（盐存在哈希串里，不需要额外存）
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_password_over_bcrypt_limit_is_rejected_not_truncated() -> None:
    """超过 bcrypt 的 72 字节上限 → **明确报错**，不静默截断。

    ⚠️ 静默截断的实际后果：`"a"*72 + "X"` 和 `"a"*72 + "Y"` 会被当成**同一个密码**。
        用户以为自己设了更长的密码，其实后半截根本没参与。
    """
    too_long = "a" * (BCRYPT_MAX_PASSWORD_BYTES + 1)

    with pytest.raises(PasswordTooLongError):
        hash_password(too_long, rounds=ROUNDS)


def test_verify_returns_false_instead_of_raising_on_extreme_input() -> None:
    """校验侧遇到超长 / 损坏输入 → **返回 False**，绝不抛异常。

    ⚠️ 抛异常会让"输错密码"变成 500，既暴露信息又难看。密码错就是密码错。
    """
    hashed = hash_password("normal", rounds=ROUNDS)

    assert verify_password("a" * 500, hashed) is False
    assert verify_password("normal", "not-a-bcrypt-hash") is False
    assert verify_password("normal", "") is False


# JWT


def test_token_roundtrip_carries_tenant_and_role() -> None:
    """token 里必须带 `tid`（租户 id）—— 数据隔离全靠它。"""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    token, expires_in = create_access_token(user_id=user_id, tenant_id=tenant_id, role="admin")
    payload = decode_access_token(token)

    assert payload.sub == user_id
    assert payload.tid == tenant_id
    assert payload.role == "admin"
    assert expires_in > 0


def test_token_signed_with_another_secret_is_rejected() -> None:
    """换个密钥签的 token 必须验不过 —— 这是"别人伪造不出身份"的根本保证。"""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "role": "admin",
            "exp": 9_999_999_999,
        },
        # 32 字节以上，避免 PyJWT 的 InsecureKeyLengthWarning 淹没真正的警告
        "a-different-secret-of-sufficient-length-32b",
        algorithm=get_settings().jwt_algorithm,
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(forged)


def test_tampered_token_is_rejected() -> None:
    """改一个字符就废 —— 签名保护的直接体现。"""
    token, _ = create_access_token(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    head, payload, signature = token.split(".")

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(f"{head}.{payload}.{signature[:-2]}xx")


def test_expired_token_is_rejected() -> None:
    """过期必须验不过，否则"登出"永远不生效。"""
    token, _ = create_access_token(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member", expires_minutes=-1
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


def test_expiry_honours_configuration() -> None:
    """有效期走配置，不硬编码（AGENTS §7.1 约定 11）。"""
    _, expires_in = create_access_token(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member", expires_minutes=5
    )

    assert expires_in == 5 * 60
