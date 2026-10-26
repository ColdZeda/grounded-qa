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
import type { AgentStep, CitationAudit, CitationRepair } from './api'

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

  /**
   * 降级原因（非空 = 这次是回落 pipeline 答的）。
   * ⚠️ 它不是“错误”，也不影响答案质量 —— 只是告诉坐席“这条不是 agent 答的”。
   *    所以用**灰色角标**，不用红色报错样式。
   */
  degraded?: string | null
  /** agent 走了几步 / 自愈几次。 */
  agentSteps?: number | null
  agentRetries?: number | null
  /** 全轨迹（点开能回放“它当时为什么这么做”）。 */
  agentTrace?: AgentStep[] | null
  /**
   * 引用可核对性（`done.citation_audit` / `meta.citation_audit`）。
   *
   * ⚠️ **只在内部侧显示**：它说的是"我们没核对上 N 处"，顾客看不懂也用不上
   *    （`architecture.md §9.53 ①`：顾客侧只给结论和"能不能办"）。
   * ⚠️ 2026-10-27 之前它只随 SSE 帧走一次 ⟹ 刷新即消失、事后也统计不到，
   *    现在两种模式都落进 `messages.meta`。
   */
  citationAudit?: CitationAudit | null
  /**
   * 修法 D 自动改正过的编号（改前 / 改后 / 命中块）。
   *
   * ⚠️ 必须显示：这是"系统动过证据"的唯一凭据，不显示就等于**悄悄改**。
   */
  citationRepairs?: CitationRepair[] | null
}
