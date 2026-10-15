/** 界面层的类型（**不是**后端契约 —— 契约在 `api.ts`）。 */

import type { Citation } from './api'

/**
 * 一条要渲染的消息。
 *
 * ⚠️ 它**不等于**后端的 `MessageOut`：
 *     · 流式回答在生成过程中就存在了，那时后端还没落库、也还没有 id
 *     · `streaming` / `error` / `cancelled` 是纯界面状态
 *     硬要把两者合成一个类型，就会被迫给 `id` 加 `?`，然后每个用到的地方都要判空。
 */
export interface UiMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  refused: boolean
  latencyMs: number | null
  confidence: number | null
  createdAt: string

  /** 还在接收 `delta`。 */
  streaming?: boolean
  /** 流中途出错，或 HTTP 层就失败了。存的是给人看的文案。 */
  error?: string | null
  /** 用户主动点了"停止"。 */
  cancelled?: boolean
}
