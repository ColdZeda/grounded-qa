/** 会话与消息接口（对应 `app/api/conversations.py` 与 `app/api/chat.py` 的读接口）。 */

import { apiFetch, apiJson } from './client'
import type { Channel, ConversationOut, ConversationSummary, MessageOut } from '@/types/api'

export interface ListOptions {
  limit?: number
  offset?: number
  /** 只看这个渠道（按组翻页用） */
  channel?: Channel
  /** 只看这个飞书群（群聊组翻页用） */
  chatId?: string
}

/**
 * 按 `updated_at` 倒序。**按组翻页**：每组自己 limit/offset，别一次拉全局。
 *
 * ⚠️ 为什么必须过滤（2026-10-25）：以前一次拉全局 50 条再在前端分组，
 *    内部会话多的时候**一个组会把配额吃光**，飞书组一条都不出现。
 */
export function listConversations(options: ListOptions = {}): Promise<ConversationOut[]> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 20) })
  if (options.offset) query.set('offset', String(options.offset))
  if (options.channel) query.set('channel', options.channel)
  if (options.chatId) query.set('chat_id', options.chatId)
  return apiJson<ConversationOut[]>(`/conversations?${query.toString()}`)
}

/** 每组**一共多少条** —— 首屏靠它画组头、说"还有 N 条"。 */
export function getConversationSummary(): Promise<ConversationSummary> {
  return apiJson<ConversationSummary>('/conversations/summary')
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
