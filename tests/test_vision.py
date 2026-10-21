"""读图客户端：解析 / 降级 / 重试 / 配置回退。

三个断言值得单独写在这里，因为它们各自守着一种**静默的坏**：

    ① **解析失败要降级、不许抛** —— 抛了就是"一条格式问题毁掉整条问答"
    ② **API 失败要抛** —— 不抛就会被上层当成"图里什么都没有"，用户以为我们看过了
    ③ **没有 JSON 时也要判断"值不值得查库"** —— 否则降级路上单号全丢
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.rag.providers import ModelAPIError
from app.rag.vision import (
    ImageAnalysis,
    ImageAnalysisClient,
    build_image_client,
    parse_analysis,
)


def _client(handler: Any) -> ImageAnalysisClient:
    transport = httpx.MockTransport(handler)
    return ImageAnalysisClient(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="deepseek-flash",
        client=httpx.AsyncClient(transport=transport),
    )


def _completion(content: str, *, usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


async def test_analyze_parses_four_fields() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return _completion(
            '{"image_type":"screenshot","scene":"订单页截图","'
            'transcription":"订单号：SF10001","has_exact_fields":true}'
        )

    result = await _client(handler).analyze(b"fakepng", content_type="image/png")

    assert result.image_type == "screenshot"
    assert result.scene == "订单页截图"
    assert result.transcription == "订单号：SF10001"
    assert result.has_exact_fields is True
    assert result.fallback is False
    assert result.usage["total_tokens"] == 15
    assert seen["url"].endswith("/chat/completions")
    # 图必须**内联**（不是给个 URL 让模型去下载）
    image_part = seen["body"]["messages"][0]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


async def test_analyze_falls_back_when_not_json() -> None:
    """模型回了人话而不是 JSON → **整段当 scene**，并记 fallback。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return _completion("这是一张纸箱破损的照片，面单上写着 SF10001。")

    result = await _client(handler).analyze(b"x", content_type="image/jpeg")

    assert result.fallback is True
    assert "纸箱破损" in result.scene
    assert result.image_type == "other"
    assert result.transcription == ""
    # ⚠️ 降级路上也要认出"值得查库"的线索，否则单号就白丢了
    assert result.has_exact_fields is True


async def test_analyze_strips_markdown_fence() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _completion(
            '```json\n{"image_type":"photo","scene":"断腿的木椅",'
            '"transcription":"","has_exact_fields":false}\n```'
        )

    result = await _client(handler).analyze(b"x", content_type="image/jpeg")
    assert result.fallback is False
    assert result.scene == "断腿的木椅"
    assert result.has_exact_fields is False


async def test_unknown_image_type_becomes_other() -> None:
    result = parse_analysis(
        '{"image_type":"hologram","scene":"某个东西","transcription":"","'
        'has_exact_fields":"false"}',
        model="m",
    )
    assert result.image_type == "other"
    # 字符串 "false" 也要读成 False（不同模型习惯不一样）
    assert result.has_exact_fields is False


async def test_api_error_raises() -> None:
    """⚠️ 读图失败**必须抛**：上层要据此明确告诉用户"图片没读成功"。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ModelAPIError):
        await _client(handler).analyze(b"x", content_type="image/png")


async def test_retries_without_response_format_when_rejected() -> None:
    """换一个不认 `response_format` 的模型时，去掉它重试一次就行。"""
    calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(400, text='{"error":"response_format is not supported"}')
        return _completion(
            '{"image_type":"document","scene":"制度页","transcription":"保修期 24 个月",'
            '"has_exact_fields":false}'
        )

    result = await _client(handler).analyze(b"x", content_type="image/png")
    assert len(calls) == 2
    assert "response_format" not in calls[1]
    assert result.scene == "制度页"


async def test_build_image_client_defaults_to_llm_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三处 `image_analysis_*` 留空时**回退到 `llm_*`** —— 生产 .env 不用改。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "image_analysis_enabled", True)
    monkeypatch.setattr(settings, "image_analysis_model", "")
    monkeypatch.setattr(settings, "image_analysis_base_url", "")
    monkeypatch.setattr(settings, "image_analysis_api_key", "")

    client = build_image_client()
    assert client is not None
    assert client.model == settings.llm_model


async def test_build_image_client_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "image_analysis_enabled", False)
    assert build_image_client() is None


def test_memory_line_is_a_fact_summary() -> None:
    """多轮追问靠它恢复上下文；只放读数，不放我们的推断。"""
    analysis = ImageAnalysis(
        image_type="photo",
        scene="纸箱一角压瘪并撕裂",
        transcription="SF10001",
        has_exact_fields=True,
        model="deepseek-flash",
    )
    line = analysis.memory_line
    assert "纸箱" in line
    assert "SF10001" in line
    for forbidden in ("可以退", "建议", "超期"):
        assert forbidden not in line


def test_memory_line_truncates_long_transcription() -> None:
    analysis = ImageAnalysis(
        image_type="document",
        scene="制度页",
        transcription="条款" * 500,
        has_exact_fields=False,
        model="m",
    )
    assert len(analysis.memory_line) < 400
