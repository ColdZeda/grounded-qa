/**
 * 后端契约的 TypeScript 镜像。
 *
 * ⚠️ **手写，不是从 OpenAPI 代码生成。**
 *     现在只有 6 个端点，引入 codegen 链条（生成器 + 构建步骤 + 版本对齐）
 *     比手抄这 40 行贵得多。
 *
 * ⚠️ 代价是真实的：**后端改了字段这里不会报错**，只会在运行时变成 `undefined`。
 *     所以下面的字段名是一个一个对着 `app/schemas/*.py` 抄的，不凭印象写。
 *     哪天端点数量上去了，再换成生成 —— 那时生成的收益才盖过成本。
 */

// ------------------------------------------------------------
// 鉴权（app/schemas/auth.py）
// ------------------------------------------------------------

/** `app/db/models/user.py` 的 `UserRole`。 */
export type UserRole = 'admin' | 'member'

export interface TokenOut {
  access_token: string
  token_type: string
  /** 秒。前端拿它算过期时刻，不用自己解 JWT。 */
  expires_in: number
}

export interface UserOut {
  id: string
  email: string
  role: UserRole
  tenant_id: string
}

// ------------------------------------------------------------
// 会话与消息（app/schemas/conversation.py / chat.py）
// ------------------------------------------------------------

/** `app/db/models/conversation.py` 的 `MessageRole`。`system` 目前不落库。 */
export type MessageRole = 'user' | 'assistant' | 'system'

export interface ConversationOut {
  id: string
  title: string
  user_id: string
  created_at: string
  updated_at: string
}

/**
 * 这个分数是**什么口径**。
 *
 * ⚠️ 必须原样展示给用户，**不能统一成一个"相关度"** ——
 *     三种分量纲不同：`0.62` 的 rerank 分和 `0.62` 的余弦相似度完全是两回事，
 *     摆在一起会让人以为它们可比。（`app/rag/pipeline.py` 的 `Citation` 有同样注释）
 */
export type ScoreKind = 'rerank' | 'vector_cosine' | 'fulltext_trgm'

export interface Citation {
  /** 与回答正文里的 `[n]` 对应。 */
  index: number
  chunk_id: string
  document_id: string
  score: number
  score_kind: ScoreKind
  snippet: string
}

export interface MessageOut {
  id: string
  role: MessageRole
  content: string
  /**
   * ⚠️ **空数组现在只有一种含义了：用户消息。**
   *    **拒答也会带 `citations`**（2026-10-15 定，见 `architecture.md` §9.19）——
   *    那时它们是"检索到了、但都不够好"的**候选**，而不是"引用"。
   */
  citations: Citation[]
  refused: boolean
  /**
   * 置信度（`rerank` 分数，或退化时的向量余弦）—— 口径跟着 `citations[].score_kind`。
   * 用户消息恒为 `null`；助手消息在"只有全文命中"时也是 `null`。
   */
  confidence: number | null
  latency_ms: number | null
  created_at: string
}

// ------------------------------------------------------------
// SSE 事件载荷（app/api/chat.py 的 `_sse` 调用处）
// ------------------------------------------------------------

/** `event: sources` —— 检索完成、开始生成**之前**发的第一个事件。 */
export interface SourcesEvent {
  citations: Citation[]
  confidence: number | null
  refused: boolean
}

export interface DeltaEvent {
  text: string
}

/** `event: done` —— 收尾。注意**它不代表成功**，出错时 `error` 事件之后照样会来一个 `done`。 */
export interface DoneEvent {
  refused: boolean
  confidence: number | null
  latency_ms: number
}

/** `event: error` —— 流开始**之后**出的错，只能这样告诉前端（改不了状态码了）。 */
export interface ErrorEvent {
  detail: string
  stage: string
}
