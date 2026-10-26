/**
 * 纠错反馈（`app/api/feedback.py`）。
 *
 * ⚠️ **只传 message_id + 类型 + 一句话** —— 答案与引用由服务端取快照。
 *    前端传的话，改一行请求就能伪造一条"当时答案是 xxx"的记录。
 *
 * ⚠️ **「采纳」与「发布」是两个动作，中间隔着人**（D11）：
 *    采纳只生成一份**草稿**（不进检索），发布才让它出现在知识库里，
 *    而且发布前服务端会先拿它和全库算一次冲突。
 */

import { apiJson } from './client'

export type FeedbackVerdict = 'wrong_answer' | 'wrong_citation' | 'other'

export interface FeedbackOut {
  id: string
  message_id: string
  verdict: FeedbackVerdict
  note: string | null
  answer_snapshot: string
  citations_snapshot: Record<string, unknown>[]
  created_at: string
}

/** 管理员清单里的一条：多出"谁做的"和"草稿什么状态"。 */
export interface FeedbackAdminOut extends FeedbackOut {
  adopted_document_id: string | null
  adopted_at: string | null
  published_at: string | null
  adopted_by_email: string | null
  published_by_email: string | null
  /** `draft` = 待发布，`active` = 已发布，`null` = 还没采纳 */
  adopted_document_status: string | null
  adopted_document_name: string | null
}

export interface DraftOut {
  document_id: string
  document_name: string
  content_status: string
  title: string
  content: string
  adopted_at: string | null
  published_at: string | null
  chunks: number
}

/** 冲突体检报出来的一条（结构见 `app/kb/conflicts.py::build_report`）。 */
export interface ConflictItem {
  kind: string
  topic: string
  scope: string
  left: { doc: string; sentence: string; value: number; status: string; docs?: string[] }
  right: { doc: string; sentence: string; value: number; status: string; docs?: string[] }
}

export interface AdoptOut {
  document_id: string
  document_name: string
  content_status: string
  chunks: number
  note: string | null
}

export interface PublishOut {
  document_id: string
  document_name: string
  content_status: string
  published_at: string
  forced: boolean
  conflicts: ConflictItem[]
}

export async function submitFeedback(payload: {
  message_id: string
  verdict: FeedbackVerdict
  note?: string | null
}): Promise<FeedbackOut> {
  return apiJson<FeedbackOut>('/feedback', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function listFeedback(limit = 50): Promise<FeedbackAdminOut[]> {
  return apiJson<FeedbackAdminOut[]>(`/feedback?limit=${limit}`)
}

export async function adoptFeedback(
  id: string,
  payload: { title?: string | null; content?: string | null },
): Promise<AdoptOut> {
  return apiJson<AdoptOut>(`/feedback/${id}/adopt`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function readDraft(id: string): Promise<DraftOut> {
  return apiJson<DraftOut>(`/feedback/${id}/draft`)
}

export async function publishFeedback(id: string, force = false): Promise<PublishOut> {
  return apiJson<PublishOut>(`/feedback/${id}/publish`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}
