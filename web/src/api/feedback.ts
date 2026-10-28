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

import { apiFetch, apiJson } from './client'
import { listMessages } from './conversations'

export type FeedbackVerdict = 'wrong_answer' | 'wrong_citation' | 'other'

export interface FeedbackOut {
  id: string
  message_id: string
  verdict: FeedbackVerdict
  note: string | null
  answer_snapshot: string
  citations_snapshot: Record<string, unknown>[]
  created_at: string
  /** 客户问的那一句。⚠️ 迁移前的老记录是 `null` —— 要明说"没存到"，别当成空问题。 */
  question_snapshot: string | null
  /** 这条回答属于哪个会话（「打开原会话」用）。会话被删后为 `null`。 */
  conversation_id: string | null
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

/** UUID 的形状。`local-1` 这种临时 id 就是靠它认出来的。 */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * 把"界面上那条消息的 id"换成**服务端真 id**（2026-10-28 走查 D1）。
 *
 * ## 为什么需要这一步
 *
 * 本页**刚生成**的回答在界面上是乐观消息，id 是 `local-1` / `local-2`（`ChatView.nextLocalId`）；
 * 它要等这一轮跑完、落库之后才有真 UUID。以前直接把这个临时 id 发给服务端 ⟹
 * **422「Input should be a valid UUID」**（界面上还显示成英文）。
 * 症状很刁：**刷新过的旧回答能提交，当场生成的必失败**。
 *
 * ## 做法
 *
 * id 不像 UUID ⟹ 拉一次消息列表，用**内容相同的那条 assistant** 对出真 id；
 * 对不上就等 1.2 秒再试（最多 3 次，覆盖"答案刚落库"的那一两秒）。
 * 仍然对不上才报错 —— 那句话是给人看的，不是英文原文。
 */
async function resolveMessageId(payload: {
  message_id: string
  conversation_id?: string | null
  answer?: string
}): Promise<string> {
  if (UUID_RE.test(payload.message_id)) return payload.message_id
  const conversationId = payload.conversation_id
  if (!conversationId) {
    throw new Error('这条回答还没落库，请稍等一两秒再点一次')
  }
  const wanted = (payload.answer ?? '').trim()
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const history = await listMessages(conversationId)
    // 从后往前找：同一段话只可能出现在最后一条（重复提问也是后者才是刚生成的这条）
    for (let i = history.length - 1; i >= 0; i -= 1) {
      const item = history[i]
      if (item.role !== 'user' && item.content.trim() === wanted) return item.id
    }
    await new Promise((resolve) => setTimeout(resolve, 1200))
  }
  throw new Error('这条回答还没落库，请稍等一两秒再点一次')
}

export async function submitFeedback(payload: {
  message_id: string
  verdict: FeedbackVerdict
  note?: string | null
  /** 会话 id：只在需要"临时 id → 真 id"时用得上。 */
  conversation_id?: string | null
  /** 当时的答案正文：用来在列表里认出"是哪一条"。 */
  answer?: string
}): Promise<FeedbackOut> {
  const messageId = await resolveMessageId(payload)
  const body = {
    message_id: messageId,
    verdict: payload.verdict,
    note: payload.note ?? null,
  }
  return apiJson<FeedbackOut>('/feedback', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function listFeedback(
  limit = 50,
  verdict?: FeedbackVerdict,
): Promise<FeedbackAdminOut[]> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (verdict) query.set('verdict', verdict)
  return apiJson<FeedbackAdminOut[]>(`/feedback?${query.toString()}`)
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

/**
 * 删掉一条反馈（**硬删，没有回收站**）。
 *
 * ⚠️ 上报的不一定对 —— 误点、看错、事后发现是自己理解错。假阳性堆在治理清单里，
 *    比没有清单更糟：没人再有耐心逐条看。
 * ⚠️ 删反馈**不删**已经采纳出来的草稿文档：那是知识库里的另一份东西，
 *    一个"删掉这条上报"的动作不该把它带走（要删去知识库页删）。
 */
export async function deleteFeedback(id: string): Promise<void> {
  // 后端回 204 无 body ⟹ 用 `apiFetch`（`apiJson` 会去 `resp.json()`，然后炸）
  await apiFetch(`/feedback/${id}`, { method: 'DELETE' })
}

export async function publishFeedback(id: string, force = false): Promise<PublishOut> {
  return apiJson<PublishOut>(`/feedback/${id}/publish`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}
