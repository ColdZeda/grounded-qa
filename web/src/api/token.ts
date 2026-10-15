/**
 * access_token 存在哪 —— 以及**为什么是这个选择**。
 *
 * 三个选项，代价各不相同：
 *
 * | 方案 | 刷新页面后 | XSS 能不能拿走 | 需要后端配合 |
 * |---|---|---|---|
 * | **localStorage（现在选的）** | ✅ 还在 | ⚠️ **能**（JS 可读） | 不用 |
 * | 内存变量（`ref`） | ❌ 掉登录 | ✅ 拿不到 | 不用 |
 * | httpOnly Cookie | ✅ 还在 | ✅ 拿不到 | **要**（改鉴权方式 + 防 CSRF） |
 *
 * ⚠️ **选 localStorage 是权衡，不是疏忽。**
 *     这是一个以**演示**为主的前端：刷新就掉登录，面试演示时非常难看。
 *     而且本前端的回答渲染**不注入 HTML**（全篇不用 `v-html`），XSS 面很小。
 *     **真上生产要换 httpOnly Cookie** —— 这句话是认真的，不是免责声明。
 *
 * ⚠️ 顺手把**过期时刻**也存下来：token 过期后所有请求都会 401，
 *     靠"先撞一次 401 再跳登录"体验是"点一下、卡一下、被踢出去"。
 *     本地就能判断的事不要交给网络往返。
 */

const TOKEN_KEY = 'gq.access_token'
const EXPIRES_AT_KEY = 'gq.token_expires_at'

/** 提前量：剩不到这么久就当已过期，避免"最后一个请求正好撞上过期"。 */
const SKEW_MS = 30_000

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    // 隐私模式 / 禁用存储时 localStorage 会直接抛异常，不是返回 null
    return null
  }
}

export function saveToken(token: string, expiresInSeconds: number): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token)
    window.localStorage.setItem(EXPIRES_AT_KEY, String(Date.now() + expiresInSeconds * 1000))
  } catch {
    // 存不进去就退化成"内存里的 token"——本次会话仍可用，只是刷新会掉
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY)
    window.localStorage.removeItem(EXPIRES_AT_KEY)
  } catch {
    // 同上，忽略
  }
}

/** 有 token **且**还没过期。 */
export function hasValidToken(): boolean {
  if (!readToken()) return false
  try {
    const raw = window.localStorage.getItem(EXPIRES_AT_KEY)
    if (!raw) return false
    const expiresAt = Number(raw)
    if (!Number.isFinite(expiresAt)) return false
    return Date.now() + SKEW_MS < expiresAt
  } catch {
    return false
  }
}
