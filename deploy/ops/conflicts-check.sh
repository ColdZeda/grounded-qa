#!/usr/bin/env bash
# 知识库冲突体检 —— 由服务器上的 systemd timer（grounded-conflicts.timer）每天调用一次。
#
# ⚠️ 为什么用 `docker exec` 而不是 `docker compose exec`：
#    compose 会先做变量插值，而 systemd 给的是**干净环境**（没有 .env 的上下文）——
#    2026-10-23 实测直接报 `required variable ACR_REGISTRY is missing`，任务当场失败。
#    `docker exec` 只要求容器名，不吃 compose 那一套。
#
# ⚠️ 失败**必须发声**：容器没起 / 数据库连不上时，只留一行 journalctl 等于没有告警 ——
#    那正是本项目最怕的「定时任务静默失效」（`AGENTS §10`）。
#    所以这里先推一条失败告警，再以非零退出让 systemd 记成 failed。
set -euo pipefail

if docker exec gq-api python -m app.kb.conflicts --notify; then
  exit 0
fi

echo "❌ 冲突体检没跑成，正在推一条失败告警" >&2
docker exec gq-api python -c '
import asyncio

from app.core.alerts import push_text

text = "⚠️ 知识库冲突体检没跑成（定时任务）\n请手动执行：docker exec gq-api python -m app.kb.conflicts"
print("failure_alert_sent =", asyncio.run(push_text(text)))
' || true

exit 1
