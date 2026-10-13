"""冒烟测试：应用能构造、两个运维端点可用。

⚠️ AGENTS.md §7.1 约定 9：**测试里不许真调 LLM API**（烧钱 + 不稳定 + 评测失真）。
    本文件只测不依赖任何外部服务的部分。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_ok() -> None:
    client = TestClient(create_app())

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_metrics_exposed() -> None:
    client = TestClient(create_app())

    resp = client.get("/metrics")

    assert resp.status_code == 200
    # prometheus_client 默认会注册 process / python 采集器
    assert "python_info" in resp.text


def test_settings_defaults_are_config_driven() -> None:
    """配置项必须能覆盖，不许硬编码（AGENTS §7.1 约定 11）。"""
    from app.core.config import Settings

    custom = Settings(embedding_model="some-other-model", chunk_size=1234)

    assert custom.embedding_model == "some-other-model"
    assert custom.chunk_size == 1234
