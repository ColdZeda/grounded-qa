/**
 * 运行时环境探针：`GET /healthz` 的 `env` 字段。
 *
 * ⚠️ **为什么需要它**：本地 `localhost:5173` 和生产 `120.24.67.82:8080` 上，
 *    右上角都写着同一个 `admin@example.com`，界面长得一模一样 ——
 *    2026-10-22 用户就在两者之间反复搞混，白排查了半小时"密码为什么错"。
 *    顶栏显示一个 `本地 / 生产` 角标，这类混淆永久消失。
 *
 * ⚠️ **不走 `apiFetch`**：`/healthz` 在 `/api/v1` 之外，而且**不鉴权**
 *    （登录页也要能显示它是哪套环境）。所以这里直接用 `fetch`。
 */

/** 后端 `/healthz` 的 `env` 取值（`app/core/config.py` 的 `app_env`）。 */
export type RuntimeEnv = 'dev' | 'prod' | 'unknown'

export interface HealthOut {
  env: RuntimeEnv
  version: string
}

export async function fetchHealth(): Promise<HealthOut> {
  const resp = await fetch('/healthz', { headers: { Accept: 'application/json' } })
  if (!resp.ok) throw new Error(`healthz ${resp.status}`)

  const body = (await resp.json()) as { env?: unknown; version?: unknown }
  // ⚠️ 只认这两个值。**不认识的取值一律 `unknown`** —— 宁可没有角标，也不要瞎标一个。
  const env: RuntimeEnv = body.env === 'dev' || body.env === 'prod' ? body.env : 'unknown'
  return { env, version: typeof body.version === 'string' ? body.version : '' }
}
