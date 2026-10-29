"""评测报告页 —— 聚合逻辑**纯单元测试**（造 payload，不读真文件、不连库）＋ 末尾一条**权限集成测试**。

⚠️ 这里守的是那句"**不许裸数字**"：
    每条指标都要带分母，每份快照都要带生成时间；缺值要显示成 `—` 而不是 0。
⚠️ 末尾那条权限测试要真库（`pytest.mark.integration`）：连不上库时会 SKIP，不是失败。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.reports import (
    _display_time,
    _num,
    _ratio,
    _summarize_answer_layer,
    _summarize_retrieval,
)
from app.core.config import get_settings
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole

PREFIX = get_settings().api_prefix


def test_display_time_formats_stamp() -> None:
    assert _display_time("20261017T112745Z") == "2026-10-17 11:27:45 UTC"


def test_display_time_does_not_guess_on_unknown_format() -> None:
    """认不出来就原样带出来 —— 猜出来的时间比没有时间更糟。"""
    assert _display_time("昨天下午") == "昨天下午"


def test_ratio_never_invents_a_denominator() -> None:
    assert _ratio(3, 5) == "3/5"
    # 分母为零是"不存在"，不是"零分"
    assert _ratio(0, 0) == "—"
    assert _num(None) == "—"
    assert _num(0.9199) == "0.920"


def test_summarize_retrieval_keeps_config_and_denominator() -> None:
    payload = {
        "kind": "retrieval",
        "generated_at": "20261017T112745Z",
        "top_k": 5,
        "config": {
            "retrieve_top_k": 20,
            "rrf_k": 60,
            "fulltext_min_similarity": 0.2,
            "embedding_model": "qwen3.7-text-embedding-flash",
            "rerank_model": "qwen3.7-text-rerank",
        },
        "meta": {"kb": "senyu", "questions": 25},
        "ablations": [{"name": "rrf+rerank", "hit_at_k": 1.0, "hit_count": 25, "mrr_at_10": 0.98}],
    }

    snapshot = _summarize_retrieval(payload, filename="retrieval-x.json", stamp="20261017T112745Z")

    assert snapshot.kind == "retrieval"
    assert snapshot.questions == 25
    # 分子分母必须一起出现
    assert any("25/25" in metric.value for metric in snapshot.metrics)
    # 那次运行的配置必须带出来 —— 换个 fulltext_min 数字就会变
    assert snapshot.config["fulltext_min_similarity"] == "0.2"
    assert snapshot.config["消融组"] == "rrf+rerank"


def test_summarize_refusal_marks_the_zero_cost_path() -> None:
    payload = {
        "kind": "refusal",
        "generated_at": "20261017T110457Z",
        "kb": "senyu",
        "llm_model": "deepseek-flash",
        "results": [
            {"id": "r1", "passed": True, "refused": True, "llm_calls": 0},
            {"id": "r2", "passed": True, "refused": False, "llm_calls": 1, "must_say_ok": True},
            {"id": "r3", "passed": False, "refused": False, "llm_calls": 1, "must_say_ok": False},
        ],
    }

    snapshot = _summarize_answer_layer(
        payload, filename="refusal-x.json", stamp="20261017T110457Z", kind="refusal"
    )

    assert snapshot.questions == 3
    by_name = {metric.name: metric.value for metric in snapshot.metrics}
    assert by_name["行为正确"] == "2/3"
    assert by_name["真的拒答了"] == "1/3"
    # 拒答是调用前的判断 —— 这一项要能看出"省了几次调用"
    assert by_name["一次模型都没调"] == "1/3"
    assert by_name["内容要点达标"] == "1/2"
    assert snapshot.config["对话模型"] == "deepseek-flash"


def test_summarize_generation_counts_forbidden_phrases() -> None:
    payload = {
        "kind": "generation",
        "generated_at": "20261017T112536Z",
        "kb": "senyu",
        "llm_model": "deepseek-flash",
        "results": [
            {"id": "g1", "passed": True, "cite_ok": True, "forbidden_hit": ""},
            {"id": "g2", "passed": False, "cite_ok": None, "forbidden_hit": "1 年"},
        ],
    }

    snapshot = _summarize_answer_layer(
        payload, filename="generation-x.json", stamp="20261017T112536Z", kind="generation"
    )

    by_name = {metric.name: metric.value for metric in snapshot.metrics}
    assert by_name["全通过"] == "1/2"
    # 只统计"真的检查过"的那些题，没检查的不该被算成错
    assert by_name["引用正确"] == "1/1"
    assert by_name["出现禁语"] == "1/2"


# 权限（集成测试，要真库）—— 2026-10-25 收紧
@pytest.mark.integration
async def test_reports_list_is_admin_only(client, login, make_account, session) -> None:
    """评测快照**只给管理员**。

    ⚠️ 以前只要求"登录"，而登录页有「一键演示账号」（演示账号是 member）⟹
        打开演示登录就等于把"判分口径 / 禁语清单 / 哪几题没通过"公开给任何访客。
        前端导航对成员隐藏这一页，但直接打 API 是拿得到的 —— 所以权限必须在这一层收紧。
    """
    admin = await make_account(role=UserRole.ADMIN, label="reports-admin")
    tenant = await session.scalar(select(Tenant).where(Tenant.id == admin.tenant_id))
    assert tenant is not None
    member = await make_account(role=UserRole.MEMBER, label="reports-member", tenant=tenant)

    admin_token = await login(admin.email, admin.password)
    member_token = await login(member.email, member.password)

    # 管理员：200（目录存不存在都 200 —— 没有结果时返回空列表 + 一句说明）
    ok = await client.get(f"{PREFIX}/reports", headers=admin.auth(admin_token))
    assert ok.status_code == 200
    assert isinstance(ok.json()["snapshots"], list)

    # 普通成员：403（"已登录但角色不够"用 403；404 是留给"越权看别人的东西"的）
    denied = await client.get(f"{PREFIX}/reports", headers=member.auth(member_token))
    assert denied.status_code == 403
