/** 鉴权接口（对应 `app/api/auth.py`）。 */

import { apiJson } from './client'
import type { TokenOut, UserOut } from '@/types/api'

/**
 * 登录，换 JWT。
 *
 * ⚠️ **这个接口吃 `application/x-www-form-urlencoded`，不是 JSON。**
 *     后端用的是 FastAPI 的 `OAuth2PasswordRequestForm` —— 好处是 Swagger UI 右上角
 *     的 Authorize 按钮直接可用（演示时省一步手工拼 header）。
 *     代价是前端得拼表单，而且**字段名固定是 `username` / `password`**
 *     （OAuth2 规范把这个字段叫 username，里面装的其实是邮箱）。
 *     写成 `email=...` 后端会收不到，报 422「字段缺失」。
 */
export async function login(email: string, password: string): Promise<TokenOut> {
  const body = new URLSearchParams({ username: email, password })
  return apiJson<TokenOut>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
}

/**
 * 当前登录用户。
 *
 * ⚠️ 它同时是**鉴权探针**：页面加载时调一次，401 说明 token 已经不作数了
 *     （过期 / 用户被停用 / 租户被停用 / 角色被改），直接去登录页。
 */
export function fetchMe(): Promise<UserOut> {
  return apiJson<UserOut>('/auth/me')
}
