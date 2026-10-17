/** 评测报告接口（对应 `app/api/reports.py`）。**只读** —— 不在页面上跑评测。 */

import { apiJson } from './client'
import type { ReportListOut } from '@/types/api'

export function listReports(): Promise<ReportListOut> {
  return apiJson<ReportListOut>('/reports')
}
