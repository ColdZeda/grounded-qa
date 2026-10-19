/** 配额与成本接口（对应 `app/api/usage.py`）。**只读**，不新开计量。 */

import { apiJson } from './client'
import type { TenantUsageOut, UsageOut } from '@/types/api'

export function getMyUsage(): Promise<UsageOut> {
  return apiJson<UsageOut>('/usage/me')
}

/** 管理员才能调（后端 `require_admin`）。 */
export function getTenantUsage(): Promise<TenantUsageOut> {
  return apiJson<TenantUsageOut>('/usage/tenant')
}
