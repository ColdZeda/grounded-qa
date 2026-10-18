/** 运维看板接口（对应 `app/api/monitoring.py`）。**只读**，不跑任何查询引擎。 */

import { apiJson } from './client'
import type { MonitorSnapshot } from '@/types/api'

export function getMonitorSnapshot(): Promise<MonitorSnapshot> {
  return apiJson<MonitorSnapshot>('/monitoring/snapshot')
}
