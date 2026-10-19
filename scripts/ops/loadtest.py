"""最小压测脚本 —— **量我们自己写的这条链路**，不是量机器。

⚠️ 它只做一件事：用固定并发持续打**一个**接口，吐出吞吐与分位数。
   不做场景编排、不做阶梯加压（那是 locust/k6 的活）—— 我们要回答的问题只有一个：
   **"瓶颈在哪一跳，改一处能换来几个数"**。

⚠️ **为什么不打 `/api/v1/chat`**：那个接口要调模型、要花钱、而且耗时由上游决定
   （我们量到的是 DeepSeek 的延迟，不是我们的）。压测要压**我们能改的那部分**。

⚠️ **鉴权接口有速率限制**（问答 30/分钟/用户、上传 5/分钟）⟹ 拿这类接口压测
   会先撞上自己的 429。所以默认打的是**没有限流的只读接口**
   （`/api/v1/conversations`：走 JWT 校验 + 一次真 DB 查询 + 序列化，
   能代表"业务请求"里我们自己那部分开销）。

用法：
    # ① 先要一个 token（本地开发账号登录换的，别用生产管理员的）
    #    密码别写在命令行里 —— 用环境变量传
    export GQ_PASSWORD='...'
    python scripts/ops/loadtest.py --base http://127.0.0.1:8000 \
        --email you@example.com --password-env GQ_PASSWORD --path /api/v1/conversations

    # ② 把结果存下来，方便前后对比
    python scripts/ops/loadtest.py ... --json ../ops/loadtest-before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


@dataclass
class Result:
    """一次压测的结果。

    ⚠️ 只报分位数、不报平均值：平均值会被"一小部分很慢的请求"完全掩盖，
        而用户感受到的正是那些慢请求。
    """

    base: str
    path: str
    concurrency: int
    duration_s: float
    requests: int = 0
    errors: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    rps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        return 0.0 if self.requests == 0 else self.errors / self.requests


def _percentile(samples: list[float], pct: float) -> float:
    """最近秩法（nearest-rank）。样本少的时候插值法会给出**根本没出现过**的数字。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[index]


async def _worker(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    deadline: float,
    latencies: list[float],
    statuses: dict[str, int],
    errors: list[str],
) -> None:
    """一个并发片：不停地打到时间到为止。"""
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            response = await client.get(url, headers=headers)
            statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
            if response.status_code >= 400:
                errors.append(f"{response.status_code}: {response.text[:120]}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            statuses["exc"] = statuses.get("exc", 0) + 1
        finally:
            latencies.append((time.perf_counter() - started) * 1000)


async def run(
    *,
    base: str,
    path: str,
    token: str,
    concurrency: int,
    duration: float,
    warmup: float,
    request_timeout: float,
) -> Result:
    url = f"{base.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # ⚠️ 连接池按并发开，且**复用连接**：每次新建 TCP 手会测到"连接开销"，
    #    那既不是我们代码的问题，也不是生产会发生的（浏览器/nginx 都复用）。
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=request_timeout) as client:
        # 预热：把连接建起来、让服务端 JIT / 缓存热起来（不预热会测到启动态）
        if warmup > 0:
            await asyncio.gather(*(client.get(url, headers=headers) for _ in range(concurrency)))

        latencies: list[float] = []
        statuses: dict[str, int] = {}
        errors: list[str] = []
        started = time.perf_counter()
        deadline = started + duration
        await asyncio.gather(
            *(
                _worker(client, url, headers, deadline, latencies, statuses, errors)
                for _ in range(concurrency)
            )
        )
        elapsed = time.perf_counter() - started

    ok = sum(count for status, count in statuses.items() if status.startswith("2"))
    return Result(
        base=base,
        path=path,
        concurrency=concurrency,
        duration_s=round(elapsed, 2),
        requests=len(latencies),
        errors=len(latencies) - ok,
        status_counts=statuses,
        rps=round(len(latencies) / elapsed, 1) if elapsed > 0 else 0.0,
        p50_ms=round(_percentile(latencies, 50), 1),
        p95_ms=round(_percentile(latencies, 95), 1),
        p99_ms=round(_percentile(latencies, 99), 1),
        max_ms=round(max(latencies, default=0.0), 1),
    )


def _login(base: str, email: str, password: str, timeout: float) -> str:
    """换 JWT。**密码只从环境变量来**（不落命令行，也就不会进 shell 历史/进程列表）。"""
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base.rstrip('/')}/api/v1/auth/login",
            data={"username": email, "password": password},
        )
        response.raise_for_status()
        return str(response.json()["access_token"])


def main() -> int:
    parser = argparse.ArgumentParser(description="最小压测：固定并发打一个接口")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--path", default="/api/v1/conversations")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--email", help="登录邮箱（不给就直接用 --token-file）")
    parser.add_argument("--password-env", default="GQ_PASSWORD", help="密码所在的环境变量名")
    parser.add_argument("--token-file", help="从文件读 token（避免 token 出现在命令行里）")
    parser.add_argument("--json", help="把结果写到这个文件（前后对比用）")
    args = parser.parse_args()

    if args.token_file:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    elif args.email:
        password = os.environ.get(args.password_env, "")
        if not password:
            parser.error(f"环境变量 {args.password_env} 是空的")
        token = _login(args.base, args.email, password, args.timeout)
    else:
        # 公开接口（`/healthz` 之类）不需要鉴权 —— 不带 header 也能压
        token = ""

    result = asyncio.run(
        run(
            base=args.base,
            path=args.path,
            token=token,
            concurrency=args.concurrency,
            duration=args.duration,
            warmup=args.warmup,
            request_timeout=args.timeout,
        )
    )

    print()
    print(f"目标      {result.base}{result.path}")
    print(f"并发      {result.concurrency} · 时长 {result.duration_s}s")
    print(f"请求数    {result.requests}（错误 {result.errors}，{result.error_rate:.1%}）")
    print(f"状态码    {result.status_counts}")
    print(f"吞吐      {result.rps} req/s")
    print(
        f"延迟      p50 {result.p50_ms}ms · p95 {result.p95_ms}ms · "
        f"p99 {result.p99_ms}ms · max {result.max_ms}ms"
    )
    if result.error_rate > 0.01:
        print("⚠️ 错误率 > 1%：这组数不能当基线用（先查错再谈性能）")

    if args.json:
        payload: dict[str, Any] = asdict(result)
        payload["error_rate"] = round(result.error_rate, 4)
        payload["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"已写入    {args.json}")

    return 0 if result.error_rate <= 0.01 else 1


if __name__ == "__main__":
    raise SystemExit(main())
