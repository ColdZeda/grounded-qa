"""配置层的安全闸门 ＋ 自洽性闸门。

⚠️ 这类测试的价值不在于"函数返回对不对"，而在于**保证那道闸不会被悄悄拆掉**。
    "生产不许用默认密钥启动"是一条**安全不变量** —— 拆掉它不会有任何功能测试变红，
    只会在某天被人扫到 `dev-only-...` 的 JWT 密钥为止。

⚠️ 后半部分测的是**另一类不变量**：**"默认值必须是能用的那一套"**。
    默认值腐烂不会有任何功能测试变红 —— 本机有 `.env` 顶着，**只有新环境才会踩到**。
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import (
    CALIBRATED_RERANK_MODELS,
    DEFAULT_ADMIN_PASSWORD,
    DEV_JWT_SECRET,
    Settings,
    warn_if_rerank_calibration_missing,
)

STRONG_SECRET = "s" * 48
STRONG_PASSWORD = "a-real-password"


#: ⚠️ 一律用 `_env_file=None` **绕开 `.env`**，测的是**代码默认值**。
#:    否则 CI（没有 `.env`）和本机（有 `.env`）会测到两样东西 ——
#:    那种测试看着绿，其实什么都没锁住。
def _defaults(**overrides: object) -> Settings:
    return Settings(_env_file=None, app_env="dev", **overrides)


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


def test_production_does_not_refuse_to_start_on_default_admin_password(caplog) -> None:
    """**默认 ADMIN_PASSWORD 不再拦启动**，只告警（2026-10-25 挪到 seed 侧硬失败）。

    ⚠️ 它跑流量时一次都不读。以前它挂在启动校验上，代价实测过：
        `.env` 留空 → api crash-loop → `depends_on: service_healthy` 把 nginx 一起拖住
        → **整站连接被拒**，而且**回滚救不了**（回滚换镜像，`.env` 不在版本控制里）。
    ⚠️ 但**不能就这么放过去**：启动必须留一条告警，建库时必须硬失败
        （`app/db/seed.py::_guard_admin_password`，见 `test_seed_guard.py`）。
    """
    with caplog.at_level(logging.WARNING):
        settings = Settings(
            app_env="prod", jwt_secret=STRONG_SECRET, admin_password=DEFAULT_ADMIN_PASSWORD
        )

    assert settings.admin_password == DEFAULT_ADMIN_PASSWORD
    assert "首次建库会失败" in caplog.text


def test_production_accepts_properly_configured_secrets() -> None:
    settings = Settings(app_env="prod", jwt_secret=STRONG_SECRET, admin_password=STRONG_PASSWORD)

    assert settings.jwt_secret == STRONG_SECRET


#: ⚠️ 这条锁的是 2026-10-17 抓到的真实漏洞 + 2026-10-25 的事故，两件事要**同时**成立：
#:    ① `.env` 写成 `ADMIN_PASSWORD=` 读出来是**空字符串**，不等于 `change-me` ——
#:       只比较"是不是默认值"会把它放过去，于是 seed 建出一个**空密码的管理员**；
#:    ② 但**拦在启动上是错的位置** —— 一个建库变量把整站打掉、回滚还救不了。
#:    ⟹ 现在的分工：**启动只告警（这里是空的，照样能起）＋ 建库时硬失败**（seed 侧）。
#:    📌 这和本项目一贯的病同源：**闸只覆盖了"想到的那种错"，没覆盖"什么都没填"**。
def test_production_tolerates_EMPTY_admin_password_at_startup(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        settings = Settings(app_env="prod", jwt_secret=STRONG_SECRET, admin_password="")

    assert settings.admin_password == ""
    assert "ADMIN_PASSWORD 是空的" in caplog.text


def test_production_rejects_EMPTY_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET 是空的"):
        Settings(app_env="prod", jwt_secret="", admin_password=STRONG_PASSWORD)


def test_default_jwt_secret_meets_hs256_minimum_length() -> None:
    """默认密钥长度必须 ≥ 32 字节，否则 PyJWT 会对**每一次**签发发警告。

    ⚠️ 这条测试锁的是一个具体的坑：原先默认值是 24 字节，
        测试输出里刷了一屏 `InsecureKeyLengthWarning` —— 而警告刷多了就没人看了。
    """
    assert len(DEV_JWT_SECRET.encode("utf-8")) >= 32


# 默认值必须"开箱能用"（2026-10-16）


def test_default_model_choices_match_the_agreed_selection() -> None:
    """**默认组合必须与已定选型一致。**

    ⚠️ 这条锁的是 2026-10-16 拆掉的一颗雷：`config.py` 的代码默认值当时还是
        `text-embedding-v3` / `gte-rerank-v2` / `0.35`。本机没事（`.env` 覆盖了），
        但**新环境里 `.env` 漏写一行就会静默落到那套组合** ——
        而它按实测（`plan\\PROJECT-PLAN.md` §4.2⑤）**会把所有问题都拒答**。

    这是本项目**第二次**遇到同一类病（第一次是 `LLM_BASE_URL` 指着智谱）。
    """
    settings = _defaults()

    assert settings.embedding_model == "qwen3.7-text-embedding-flash"
    assert settings.embedding_dim == 1024
    assert settings.rerank_model == "qwen3.7-text-rerank"
    # ⚠️ 这个数字是**实测标定**出来的（20 条样本），改它是一次决策、不是改配置 ——
    #    所以这里故意写死，逼改动的人先想清楚。
    assert settings.answer_min_score == 0.60


def test_default_rerank_pair_is_self_consistent() -> None:
    """默认的 rerank 模型必须在**已校准表**里 —— 否则应用开箱就告警。"""
    assert _defaults().rerank_model in CALIBRATED_RERANK_MODELS


def test_self_check_is_silent_for_a_calibrated_pair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """配套正确时**一个字都不许说** —— 否则告警会变成噪音，然后被无视。"""
    with caplog.at_level("WARNING"):
        warn_if_rerank_calibration_missing(_defaults())

    assert caplog.records == []


def test_self_check_warns_on_uncalibrated_rerank_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """换了模型却没重标阈值 → **必须喊出来**。

    ⚠️ 为什么非喊不可：这个错误**没有别的暴露方式**。接口照样 200，
        只是所有问题都被拒答（或全都不拒）—— 看起来像"知识库里确实没有"。
    """
    with caplog.at_level("WARNING"):
        warn_if_rerank_calibration_missing(_defaults(rerank_model="gte-rerank-v2"))

    assert len(caplog.records) == 1
    assert "gte-rerank-v2" in caplog.text
    assert "校准" in caplog.text


def test_self_check_warns_when_rerank_is_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """关掉 rerank 后，阈值比的对象变成**向量余弦** —— 那条路从没标定过。"""
    with caplog.at_level("WARNING"):
        warn_if_rerank_calibration_missing(_defaults(rerank_enabled=False))

    assert len(caplog.records) == 1
    assert "RERANK_ENABLED" in caplog.text
