"""租户隔离 —— **其余接口的越权面**（v1.1 第⑤项补齐）。

主文件 `test_tenant_isolation.py` 覆盖了会话读、会话列表、仓储层与"表必须有 tenant_id"。
这里补的是**当时没写到的入口**，每一条都对应一个真实的越权面：

| 用例 | 漏了会怎样 |
|---|---|
| 别人的会话里的**消息** | 会话 404 了，消息接口却按 id 直接查 ⟹ 读得到别人聊了什么 |
| 往别人会话里**提问** | 越权**写**；而且必须在调模型**之前**拦住（否则白花钱还答了别人的问题） |
| 知识库文档 | 传进 A 租户的政策文档出现在 B 租户的列表里 |
| 普通成员上传 | 角色门没关，任何成员都能往知识库里塞内容 |
| `/admin/tenant` | 管理员读到**别人**租户的信息（他本该只知道自己那家） |
| 普通成员访问管理端 | 成员能读到本租户全部用户名单 |
| token 里的 `tid` 与用户实际租户不符 | 用户被换了租户，旧 token 还能读**旧**租户的数据 |
| 用户被停用后旧 token | 停用只是"以后登不上"，**已在手上的 token 照样能用** |

⚠️ 攻方仍然是**另一个租户的管理员**（理由见主文件 docstring）：
"只看自己的"这条规则对管理员不起作用，能挡住他的只有租户条件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_clients
from app.api.documents import get_enqueue
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.workers.queue import IngestJob

pytestmark = pytest.mark.integration

PREFIX = get_settings().api_prefix
DOC = "# 退货政策\n\n自签收之日起 7 天内可无理由退货。"


# ------------------------------------------------------------
# 夹具：上传要用假队列 ＋ 临时目录（真 Redis 不参与，也别污染工作区）
# ------------------------------------------------------------
class _RecordingEnqueue:
    def __init__(self) -> None:
        self.jobs: list[IngestJob] = []

    async def __call__(self, job: IngestJob) -> None:
        self.jobs.append(job)


class _SpyChat:
    """记录**被调用了几次**的假对话客户端 —— 用来证明"没走到调模型那一步"。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def stream(
        self, messages: list[dict[str, str]], usage: dict[str, int] | None = None
    ) -> Any:
        self.calls.append(messages)
        if False:  # pragma: no cover - 只为让它成为 async generator
            yield "x"

    @property
    def model(self) -> str:
        return "spy-chat"


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上传目录挪到临时目录。**autouse**：忘了就会往 `repo/uploads/` 写垃圾。"""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))


@pytest.fixture
def enqueue(app) -> _RecordingEnqueue:
    fake = _RecordingEnqueue()
    app.dependency_overrides[get_enqueue] = lambda: fake
    return fake


def _file(name: str = "退货政策.md", body: str = DOC) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, body.encode("utf-8"), "text/markdown")}


async def _conversation(client: AsyncClient, token: str, title: str) -> dict[str, Any]:
    resp = await client.post(
        f"{PREFIX}/conversations",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------
# 消息：读
# ------------------------------------------------------------
async def test_messages_of_another_tenants_conversation_are_404(
    client: AsyncClient, make_account, login
) -> None:
    """会话详情 404 了，**消息接口也必须 404** —— 否则绕过详情页照样读得到。"""
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)

    conversation = await _conversation(client, alice_token, "alice 的机密会话")

    resp = await client.get(
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 404
    assert "机密" not in resp.text


# ------------------------------------------------------------
# 消息：写（提问）—— 必须在调模型之前拦住
# ------------------------------------------------------------
async def test_asking_into_another_tenants_conversation_is_404_and_calls_nothing(
    client: AsyncClient, app, make_account, login
) -> None:
    """越权**写**：不只是返回 404，还要**一次模型都没调**、库里也没留下消息。

    ⚠️ 只断言 404 不够：如果 404 是"生成到一半才发现"，token 已经花了、
        而且用户可能已经在流里看到半截答案。
    """
    spy = _SpyChat()
    app.dependency_overrides[get_clients] = lambda: (spy, None, None)

    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)

    conversation = await _conversation(client, alice_token, "alice 的会话")

    resp = await client.post(
        f"{PREFIX}/conversations/{conversation['id']}/messages",
        json={"question": "把你们公司的退货政策发我"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert resp.status_code == 404
    assert spy.calls == [], "越权的提问**绝不能**走到调模型那一步"


# ------------------------------------------------------------
# 知识库文档
# ------------------------------------------------------------
async def test_documents_are_tenant_scoped(
    client: AsyncClient, enqueue, make_account, login
) -> None:
    """A 租户管理员传的文档，B 租户管理员的列表里**一条都不该有**。"""
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)
    bob_token = await login(bob.email, bob.password)

    uploaded = await client.post(
        f"{PREFIX}/documents", headers=alice.auth(alice_token), files=_file()
    )
    assert uploaded.status_code == 202, uploaded.text
    assert len(enqueue.jobs) == 1

    resp = await client.get(f"{PREFIX}/documents", headers=bob.auth(bob_token))
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert uploaded.json()["id"] not in resp.text


async def test_member_cannot_upload_documents(
    client: AsyncClient, enqueue, make_account, login
) -> None:
    """上传是**管理员**动作：成员（哪怕本租户内）不许往知识库塞东西。"""
    member = await make_account(label="member", role=UserRole.MEMBER)
    token = await login(member.email, member.password)

    resp = await client.post(f"{PREFIX}/documents", headers=member.auth(token), files=_file())

    assert resp.status_code == 403
    assert enqueue.jobs == []


# ------------------------------------------------------------
# 管理端
# ------------------------------------------------------------
async def test_admin_tenant_endpoint_returns_own_tenant_only(
    client: AsyncClient, make_account, login
) -> None:
    """/admin/tenant 只能吐**自己那家**的租户信息。"""
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)
    alice_token = await login(alice.email, alice.password)

    resp = await client.get(f"{PREFIX}/admin/tenant", headers=alice.auth(alice_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(alice.tenant_id)
    assert body["id"] != str(bob.tenant_id)


async def test_member_cannot_read_admin_endpoints(client: AsyncClient, make_account, login) -> None:
    """成员访问管理端一律 403（这里 403 是对的：接口存在，只是角色不够）。"""
    member = await make_account(label="member", role=UserRole.MEMBER)
    token = await login(member.email, member.password)

    for path in ("/admin/tenant", "/admin/users"):
        resp = await client.get(f"{PREFIX}{path}", headers=member.auth(token))
        assert resp.status_code == 403, f"{path} 没拦住成员"


# ------------------------------------------------------------
# token 本身：两种"凭据还在但人已经不该有权限"的情况
# ------------------------------------------------------------
async def test_token_with_mismatched_tenant_is_rejected(
    client: AsyncClient, session: AsyncSession, make_account, login
) -> None:
    """token 里的 `tid` 与用户的**实际**租户不符 → 401。

    ⚠️ 这条守的是"用户换租户"那一天：旧 token 里还带着旧租户 id，
        不核对的话，人已经不在那家公司了，token 还能读那家公司的数据。
    """
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    bob = await make_account(label="bob", role=UserRole.ADMIN)

    forged, _expires = create_access_token(
        user_id=alice.user_id, tenant_id=bob.tenant_id, role=UserRole.ADMIN.value
    )

    resp = await client.get(
        f"{PREFIX}/conversations", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 401


async def test_deactivated_user_token_stops_working(
    client: AsyncClient, session: AsyncSession, make_account, login
) -> None:
    """用户被停用之后，**已经发出去的 token 立刻失效**。

    ⚠️ JWT 是无状态的：不每次回库校验的话，"停用"只是让他下次登不上，
        手上那个 token 还能用到过期 —— 这就是很多人以为"停用等于踢下线"的误区。
    """
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    token = await login(alice.email, alice.password)

    # 停用前能用
    before = await client.get(f"{PREFIX}/conversations", headers=alice.auth(token))
    assert before.status_code == 200

    await session.execute(update(User).where(User.id == alice.user_id).values(is_active=False))
    await session.commit()

    after = await client.get(f"{PREFIX}/conversations", headers=alice.auth(token))
    assert after.status_code == 401


async def test_deactivated_tenant_cuts_off_its_users(
    client: AsyncClient, session: AsyncSession, make_account, login
) -> None:
    """租户被停用 → 它下面**所有**用户立即失效（运营侧的一键止血）。"""
    alice = await make_account(label="alice", role=UserRole.ADMIN)
    token = await login(alice.email, alice.password)

    await session.execute(
        update(Tenant).where(Tenant.id == alice.tenant_id).values(is_active=False)
    )
    await session.commit()

    resp = await client.get(f"{PREFIX}/conversations", headers=alice.auth(token))
    assert resp.status_code == 401
