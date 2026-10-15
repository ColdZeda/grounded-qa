/**
 * HTTP 客户端：**薄，但把三件事收在一处**。
 *
 *   1. 前缀 `/api/v1`（与后端 `settings.api_prefix` 对应）
 *   2. 自动带 `Authorization` 头
 *   3. **401 的统一处理** —— 这是最要紧的一条：token 过期可能在**任何一个**请求上暴露，
 *      如果每个调用点各写一遍"401 就去登录页"，必然有漏的，表现就是
 *      "偶尔某些页面卡着不动，也没有任何提示"。
 *
 * ⚠️ **不用 axios**：只需要上面这三件事，`fetch` 加 40 行就够了。
 *     axios 会带来拦截器、取消、转换器一整套概念，而这里每一样都要重新学一遍。
 *
 * ⚠️ **不加 CORS 中间件**（后端侧）：开发时 `vite` 的 proxy 把 `/api` 转到后端，
 *     生产时 nginx 反代 —— **两种情况都是同源**。同源就不需要 CORS，
 *     不需要 CORS 就少一类配置错误（和一类允许来源写太宽的风险）。
 */

import { clearToken, readToken } from './token'

/** 与 `app/core/config.py` 的 `api_prefix` 一致。改版本号时两边一起改。 */
export const API_BASE = '/api/v1'

/** 后端返回的错误。`status` 留着是为了让调用方能区分 401 / 404 / 422。 */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 401 的全局钩子。
 *
 * ⚠️ 用**注册**而不是在 client 里 `import` router：那会形成
 *     `router → stores/auth → api/auth → api/client → router` 的循环依赖。
 *     循环依赖在 ESM 里不一定报错，而是**某些模块拿到 undefined** —— 极难排查。
 */
let unauthorizedHandler: (() => void) | null = null

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

export function authHeaders(): Record<string, string> {
  const token = readToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * 把失败响应翻成 `ApiError`。
 *
 * ⚠️ FastAPI 的 `detail` 有**两种形状**，都要认：
 *     · 我们自己 `raise HTTPException(detail="会话不存在")` → `detail` 是**字符串**
 *     · Pydantic 参数校验失败（422）→ `detail` 是**对象数组** `[{loc, msg, type}]`
 *     只处理字符串的话，422 会显示成 `[object Object]`，等于没有报错信息。
 */
async function toApiError(resp: Response): Promise<ApiError> {
  let message = `请求失败（HTTP ${resp.status}）`
  try {
    const body: unknown = await resp.json()
    const detail = (body as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => (item as { msg?: unknown })?.msg)
        .filter((msg): msg is string => typeof msg === 'string')
      if (parts.length > 0) message = parts.join('；')
    }
  } catch {
    // 响应不是 JSON（网关的 502 页面之类）→ 保留状态码兜底文案
  }
  return new ApiError(resp.status, message)
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...authHeaders(),
      ...(init.headers ?? {}),
    },
  })

  if (resp.status === 401) {
    // ⚠️ **先清 token 再通知**：钩子里会跳登录页，跳过去的那一刻
    //    路由守卫要能立刻判断出"没登录了"。顺序反了会跳回原页面再被踢一次。
    clearToken()
    unauthorizedHandler?.()
  }

  if (!resp.ok) throw await toApiError(resp)
  return resp
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await apiFetch(path, init)
  return (await resp.json()) as T
}
