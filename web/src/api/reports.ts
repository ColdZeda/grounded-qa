/** 评测报告接口（对应 `app/api/reports.py`）。**只读** —— 不在页面上跑评测。 */

import { apiJson } from './client'
import type { ConflictReport, ReportListOut } from '@/types/api'

export function listReports(): Promise<ReportListOut> {
  return apiJson<ReportListOut>('/reports')
}

/**
 * 知识库冲突体检。⚠️ 和上面的评测快照**不是一回事**：
 * 那是"读已经跑出来的结果文件"，这个要**当场扫库**（同主题 ＋ 同适用范围 ＋ 同单位族，值不同）。
 *
 * ⚠️ **只管理员能调**（里面会把内部材料和现行政策并排摆出来）——
 * 普通成员会拿到 403，调用方要能区分"没有冲突"和"你看不了"。
 */
export function getConflicts(): Promise<ConflictReport> {
  return apiJson<ConflictReport>('/reports/conflicts')
}
