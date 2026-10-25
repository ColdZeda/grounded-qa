/** 会话与消息接口（对应 `app/api/conversations.py` 与 `app/api/chat.py` 的读接口）。 */

import { apiFetch, apiJson } from './client'
import type { ConversationOut, MessageOut } from '@/types/api'

/** 按 `updated_at` 倒序。⚠️ 后端没有 `total`，"还有没有更多"只能靠"返回条数 == limit"判断。 */
export function listConversations(limit = 50): Promise<ConversationOut[]> {
  return apiJson<ConversationOut[]>(`/conversations?limit=${limit}`)
}

/**
 * 新建会话。
 *
 * ⚠️ 请求体里**没有** `tenant_id` / `user_id` —— 归属由服务端从 token 推导。
 *     前端**不要**试图传这两个字段，后端也不接受（客户端能指定归属 = 隔离形同虚设）。
 */
export function createConversation(title = '新会话'): Promise<ConversationOut> {
  return apiJson<ConversationOut>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

/** 会话内的消息，按时间正序。刷新页面后用它恢复上下文。 */
export function listMessages(conversationId: string, limit = 100): Promise<MessageOut[]> {
  return apiJson<MessageOut[]>(`/conversations/${conversationId}/messages?limit=${limit}`)
}

/** 会话详情。用于直接拿 URL 进来时确认这个会话**存在且可见**（越权一律 404）。 */
export function getConversation(conversationId: string): Promise<ConversationOut> {
  return apiJson<ConversationOut>(`/conversations/${conversationId}`)
}

/**
 * 删除会话（**连同里面的消息，不可恢复**）。
 *
 * ⚠️ 后端权限与"能不能看见"完全一致：自己的会话，或本租户管理员看得到的那些；
 *    越权一律 404（不给 403，否则可以靠状态码差别枚举 id）。
 */
export function deleteConversation(conversationId: string): Promise<void> {
  return apiFetch(`/conversations/${conversationId}`, { method: 'DELETE' }).then(() => undefined)
}
