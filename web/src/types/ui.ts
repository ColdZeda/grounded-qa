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

  /**
   * 这条消息带的那张图（**只有用户消息**）。
   * ⚠️ 存的是 `image_id` 而不是字节：图留在服务端，界面按 id 取。
   */
  imageId?: string | null
  /**
   * 助手消息：读图读到了什么（`sources.image.note`）。
   * ⚠️ 只在**详细**档显示 —— 它是"我们怎么看到的"，不是回答本身。
   */
  imageNote?: string | null
  /**
   * 助手消息：读图**失败**了（`sources.image.error`）。
   * ⚠️ 这个**任何档位都要显示**：不说的话，用户会以为我们看过这张图了。
   */
  imageError?: string | null
  /**
   * 用户消息：这一问的**检索**并进了上一轮的哪句问句（`sources.followup_prev` /
   * `meta.followup_prev`）。空 = 没合并。
   * ⚠️ 只在**详细**档显示 —— 它是"我们内部怎么检索的"，终端用户看到只会困惑
   *    （他明明只打了一句「德国呢」）。
   */
  followupPrev?: string | null
}
