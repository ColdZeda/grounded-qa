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
  /** 一键演示账号（**只读管理员**）：页面都能看，写操作一律被后端 403。 */
  is_demo?: boolean
  /** 界面偏好（目前只有提问默认模式）。 */
  preferences?: ChatPreferences | null
  /**
   * agent 模式是否可用（= 服务端总闸）。
   * ⚠️ 为 false 时必须把模式开关**置灰** —— 否则用户以为自己切到了 agent。
   */
  agent_available?: boolean
}

/** 用户界面偏好（`users.preferences`）。 */
export interface ChatPreferences {
  /** 下次提问默认用哪条路。 */
  chat_mode?: 'fast' | 'agent'
}

/** 提问时的模式：`fast` = pipeline（快）；`agent` = 模型自己选工具（准，慢 2~4 倍）。 */
export type ChatMode = 'fast' | 'agent'

// ------------------------------------------------------------
// 会话与消息（app/schemas/conversation.py / chat.py）
// ------------------------------------------------------------

/** `app/db/models/conversation.py` 的 `MessageRole`。`system` 目前不落库。 */
export type MessageRole = 'user' | 'assistant' | 'system'

/** 会话从哪来（`app/db/models/conversation.py::Channel`）。 */
export type Channel = 'web' | 'feishu_p2p' | 'feishu_group'

export interface ConversationOut {
  id: string
  title: string
  user_id: string
  /** 渠道 —— 列表按它分组，并上三色标签（**同时带文字**，不只靠颜色）。 */
  channel: Channel
  /** 飞书侧的原生 id 与真名。`web` 的三列都是 `null`。 */
  external_chat_id: string | null
  external_user_id: string | null
  /** 群聊 = 群名；私聊 = 那个人的名字。 */
  external_title: string | null
  created_at: string
  updated_at: string
}

/**
 * 会话列表的一"段"（内部 / 私聊 / 某个群）的规模信息（`GET /conversations/summary`）。
 *
 * ⚠️ 为什么需要它：列表是**按组翻页**的，手里只有"已加载的那几条"，
 *    数不出"一共多少"。没有总数就画不出组头，也说不出"还有 N 条"。
 */
export interface ConversationGroup {
  /** `web` / `p2p` / `grp:{chat_id}` —— 前端直接当 key */
  key: string
  channel: Channel
  external_chat_id: string | null
  external_title: string | null
  /** 这一组**一共**多少条（不是已加载多少条） */
  count: number
  latest_at: string
}

export interface ConversationSummary {
  total: number
  groups: ConversationGroup[]
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
  /**
   * 这块在文档里的序号（「看原文」靠它定位）。
   * ⚠️ 可选：2026-10-25 之前的回答没有这个字段 ⟹ 那些卡片没有「看原文」按钮（不是 bug）。
   */
  ordinal?: number
}

/**
 * 一条订单的**事实卡片**（`app/orders.py::order_snapshot`）。
 *
 * ⚠️ 全是可核对的**事实**，没有"所以能退"这类推论 —— 该不该退是知识库条款的事。
 * ⚠️ 界面暂时**不渲染**它们：这些是给模型看的上下文，摆到用户面前容易让人以为
 *     我们在后台改订单状态。类型写出来是为了 `meta` 不变成 `any`。
 */
export interface OrderSnapshot {
  order_no: string
  status: string
  placed_at: string
  delivered_at: string | null
  days_since_delivery: number | null
  items: { name?: string; qty?: number }[]
  amount: string
  currency: string
}

/**
 * `messages.meta` —— **我们补的读数**，不是用户原话（`app/api/chat.py` 写入）。
 *
 * ⚠️ 它和 `content` **刻意分开**：`content` 永远是用户打的字。混进机器生成的文字，
 *     排障和评测都会失真（"这句话到底是用户说的还是模型读出来的？"）。
 * ⚠️ 只有**用户消息**可能带 `meta`。
 */
export interface MessageMeta {
  /** 这条消息带的那张图。刷新页面后靠它把缩略图找回来。 */
  image_id?: string | null
  /** 进历史的**文字记忆**（模型看不到图，追问全靠这一句）。 */
  image_note?: string | null
  /** 读图失败时的一句话。**非空 = 我们其实没看图**，界面必须说出来。 */
  image_error?: string | null
  /**
   * 这次提问的**检索**并进了上一轮的哪句问句（短追问改写，`app/rag/followup.py`）。
   * ⚠️ 只影响检索，`content` 永远是用户原话。
   */
  followup_prev?: string | null
  orders?: OrderSnapshot[]
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
  /** 机器附加的读数（读图、订单）。**只有用户消息**可能有。 */
  meta: MessageMeta | null
  created_at: string
}

// ------------------------------------------------------------
// 知识库文档（app/schemas/document.py）
// ------------------------------------------------------------

/** `app/db/models/knowledge.py` 的 `DocumentStatus`。 */
export type DocumentStatus = 'pending' | 'processing' | 'ready' | 'failed'

export interface DocumentOut {
  id: string
  /** 入库时的名字，形如 `upload/退货政策.md` 或 `senyu/10-退换货/10-退换货总则.md`。 */
  filename: string
  content_type: string | null
  size_bytes: number | null
  status: DocumentStatus
  /** ⚠️ **只有 `failed` 时才有值** —— 界面靠它解释"为什么没进库"。 */
  error_message: string | null
  /** 被切成几块。上传接口返回时是 0（还没切），列表接口每次都是真数。 */
  chunk_count: number
  created_at: string
  updated_at: string
}

export interface DocumentUploadOut extends DocumentOut {  /** 这次请求是不是真的新建了一份。`false` = 判重命中（**不是失败**）。 */
  created: boolean
  note: string
}

export interface DocumentListOut {
  total: number
  items: DocumentOut[]
}

/** 切片的一块（「看上下文」用）。 */
export interface ChunkOut {
  ordinal: number
  content: string
  /** 是不是用户点开的那一块（前后相邻的几块是 `false`）。 */
  hit: boolean
}

/** `GET /documents/{id}/chunks?around=` —— **只给相邻几块**，永远不是整篇。 */
export interface DocumentChunksOut {
  document_id: string
  filename: string
  /** 请求的那一块（越界已被后端夹到合法区间）。 */
  ordinal: number
  total: number
  window: number
  items: ChunkOut[]
}

// ------------------------------------------------------------
// 评测报告（app/schemas/report.py）
// ------------------------------------------------------------

export interface ReportMetric {
  name: string
  /** ⚠️ **已经格式化好的字符串**（如 `0.920（23/25）`）—— 分子分母必须一起显示。 */
  value: string
  hint: string
}

export interface ReportSnapshot {
  file: string
  kind: string
  generated_at: string
  generated_at_display: string
  questions: number
  metrics: ReportMetric[]
  config: Record<string, string>
}

export interface ReportListOut {
  snapshots: ReportSnapshot[]
  note: string
}

// ------------------------------------------------------------
// 知识库冲突体检（app/kb/conflicts.py，`GET /reports/conflicts`）
// ------------------------------------------------------------

/** 一条"期限事实"：某篇文档说，在某个适用范围下，某主题是多少。 */
export interface ConflictFact {
  /** 文档文件名（相对知识库根）—— ⚠️ 只给文件名，不给正文 */
  doc: string
  /** `active`（现行政策）/ `internal`（内部材料）/ … */
  status: string
  topic: string
  /** 适用范围（`德国站` / `全局`） */
  scope: string
  value: number
  /**
   * 单位族：`day` / `month` / `year` / `hour` —— ⚠️ 比较时**天与小时会换算**
   * （`ConflictItem.unit_family` 此时是 `time`），但**展示一律用各自的单位**。
   */
  unit_family: string
  /** 支撑这条数值的**原句**（后端截到 120 字）。**没有原句的告警等于"你自己去找"** */
  sentence: string
  /** ⚠️ 只有 `right` 带上：同一个错值可能出现在好几篇文档里，合并显示 */
  docs?: string[]
}

/** 一对数字对不上的事实。 */
export interface ConflictItem {
  /** `internal_vs_policy`（内部材料 ≠ 现行政策）或 `active_vs_active`（两份现行互斥） */
  kind: string
  topic: string
  scope: string
  /** 用来比较的族：同族时就是那一族；天/小时换算后比较时是 `time` */
  unit_family: string
  left: ConflictFact
  right: ConflictFact
}

/**
 * `GET /reports/conflicts` —— **按需现算**的体检（不是评测快照）。
 *
 * ⚠️ 只报不一致，**不判谁对**：运维动作取决于人（改内部稿 / 走产品决策）。
 */
export interface ConflictReport {
  /** 参与**配对**的文档数 */
  documents: number
  /** 被跳过的（讲解稿 / 辟谣稿：说的是**别人的**规则） */
  skipped_documents: number
  facts: number
  conflicts: number
  items: ConflictItem[]
}

// ------------------------------------------------------------
// 运维看板（app/schemas/monitoring.py）
// ------------------------------------------------------------

export interface MonitorItem {
  /** 序列标签（如 `outcome=answered`）。单值序列是空串。 */
  label: string
  /** ⚠️ `null` = **没有采到数据**，要显示 `—`，**不是 0**（0 和「没有」是两件事）。 */
  value: number | null
  /** 样本时间（epoch 秒）。`null` = 没有样本。 */
  ts: number | null
}

export interface MonitorPanel {
  key: string
  group: string
  title: string
  /** `ratio` / `pct` / `s` / `req_s` / `mb` / `gb` / `count` / `bool` / `chunks` */
  unit: string
  /** 口径说明 —— 后端强制要求，页面上必须显示（「不许裸数字」）。 */
  hint: string
  items: MonitorItem[]
}

export interface MonitorSnapshot {
  available: boolean
  generated_at_display: string
  prometheus_url: string
  /** 样本比这个秒数还旧就标黄（后端算的 = 3 个抓取周期）。 */
  stale_after_s: number
  panels: MonitorPanel[]
  error: string | null
}

// ------------------------------------------------------------
// 配额与成本（app/schemas/usage.py）
// ------------------------------------------------------------

/** 某个渠道今天花了多少（**同一笔总量**的分项，不参与限流判定）。 */
export interface ChannelUsage {
  /** 机器值：`web` / `feishu_p2p` / `feishu_group` */
  channel: string
  /** 给人看的名字（「内部 Web」/「飞书私聊」/「飞书群聊」） */
  label: string
  /** `null` = 取不到 */
  tokens_used: number | null
}

export interface UsageOut {
  date: string
  /** ⚠️ `null` = **取不到**（Redis 不可用），要显示 `—`，不是 0。 */
  tokens_used: number | null
  quota_limit: number
  tokens_remaining: number
  requests_this_minute: number | null
  rate_limit_per_minute: number
  reset_at_display: string
  /** 没配单价时为 `null`（**不要假装 0 元**）。 */
  cost_estimate_cny: number | null
  price_per_million_tokens: number | null
  generated_at_display: string
  note: string
  /** 按渠道拆（顺序固定：Web / 飞书私聊 / 飞书群聊）。 */
  channels: ChannelUsage[]
}

export interface MemberUsage {
  email: string
  role: string
  tokens_used: number | null
  quota_limit: number
  /** 这一行是**飞书渠道账号**（私聊＋所有群聊都记在它名下）。 */
  is_channel_account: boolean
}

export interface TenantUsageOut {
  date: string
  members: number
  /** 一个都取不到时是 `null` —— 合计不能装作准。 */
  tokens_used_total: number | null
  items: MemberUsage[]
  generated_at_display: string
  note: string
}

// ------------------------------------------------------------
// 图片（app/schemas/image.py）
// ------------------------------------------------------------

/** 上传结果。
 *  ⚠️ 后端**刻意不回传 `path` / `sha256`**（前者是实现细节，后者是内部判重口径）。 */
export interface ImageOut {
  id: string
  content_type: string
  bytes: number
  width: number
  height: number
  created_at: string
}

// ------------------------------------------------------------
// SSE 事件载荷（app/api/chat.py 的 `_sse` 调用处）
// ------------------------------------------------------------

/** `sources` 里附带的那张图的读数（只在**这条消息带图**时出现）。 */
export interface ImageInfo {
  /** 真的读成功了。⚠️ `false` **不一定**是错 —— 见 `error`。 */
  analyzed: boolean
  /** 进历史的那句文字记忆（"用户发来一张图：……"）。 */
  note: string | null
  /** 读图失败的一句话。**非空 = 这次我们没看图**，界面必须明说，不许静默。 */
  error: string | null
}

/** `event: sources` —— 检索完成、开始生成**之前**发的第一个事件。 */
export interface SourcesEvent {
  citations: Citation[]
  confidence: number | null
  refused: boolean
  /** ⚠️ **只在带了图时出现**。没带图的请求没有这个字段（不是 `null`）。 */
  image?: ImageInfo
  /**
   * 只在**做了短追问改写**时出现：这次检索并进了上一轮的那句问句。
   * ⚠️ 它要显示在**用户消息**上（chip「已合并上一轮检索」），不是助手消息。
   */
  followup_prev?: string
}

export interface DeltaEvent {
  text: string
}

/**
 * 引用可核对性（`app/rag/citations.py`）：回答里每个 `[n]` 后面那句的**数字**，
 * 是否真的在被引段落里。
 *
 * ⚠️ 它是**比例不是开关** —— `unsupported / checked` 才是串号率，
 * `unsupported === 0` 只说明这次核到的都对上了。
 */
export interface CitationAudit {
  /** 有引用标记、且含可核数字的句子数（分母） */
  checked: number
  /** 其中核对不上的句子数（分子） */
  unsupported: number
  /** `unsupported / checked`，后端已四舍五入到 4 位 */
  rate: number
  /** 最多 3 条明细（后端截断） */
  issues: { markers: number[]; atoms: string[]; sentence: string }[]
}

/** `event: done` —— 收尾。注意**它不代表成功**，出错时 `error` 事件之后照样会来一个 `done`。 */
/**
 * SSE 的 `tool` 帧：agent 正在调一个工具。
 *
 * ⚠️ 它**不是给终用户看的业务信息**，而是"我还活着"的进度信号：
 *    agent 比 pipeline 慢 2~4 倍，中间几秒什么都不给，用户会以为卡死了。
 */
export interface ToolEvent {
  /** 工具名：`search_kb` / `get_order` / `ask_user` / `finish`。 */
  name: string
  /** 参数的**截断预览**（已脱敏）。 */
  args: string
  ok: boolean
  ms: number
  /** 一句话人话，界面直接显示。 */
  summary: string
}

export interface DoneEvent {
  refused: boolean
  confidence: number | null
  latency_ms: number
  /**
   * ⚠️ **只在真的有可核的引用句时才出现**（`checked > 0`）：
   * 拒答 / 闭卷 / 整句全是名词 → 没有这个字段（不是 `null`）。
   */
  citation_audit?: CitationAudit
}

/** `event: error` —— 流开始**之后**出的错，只能这样告诉前端（改不了状态码了）。 */
export interface ErrorEvent {
  detail: string
  stage: string
}
