/**
 * 纠错反馈（`app/api/feedback.py`）。
 *
 * ⚠️ **只传 message_id + 类型 + 一句话** —— 答案与引用由服务端取快照。
 *    前端传的话，改一行请求就能伪造一条"当时答案是 xxx"的记录。
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
