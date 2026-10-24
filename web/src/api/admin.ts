/**
 * 管理端（对应 `app/api/admin.py`）。**仅管理员**可调。
 *
 * ⚠️ 这里**没有**"注册"接口：本系统是内部 B 端，账号由管理员建
 * （`createUser`），没有开放注册。理由见 `..\plan\PROJECT-PLAN.md` §12.1。
 */

import { apiJson } from './client'
import type { UserRole } from '@/types/api'

/** 管理端用户列表项（**不含**任何密码字段 —— 后端 `UserSummary` 就没给）。 */
export interface UserSummary {
  id: string
  email: string
  role: UserRole
  is_active: boolean
}

export interface CreateUserPayload {
  email: string
  password: string
  role: UserRole
}

export function listUsers(): Promise<UserSummary[]> {
  return apiJson<UserSummary[]>('/admin/users')
}

export function createUser(payload: CreateUserPayload): Promise<UserSummary> {
  return apiJson<UserSummary>('/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}
