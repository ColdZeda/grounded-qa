/**
 * 提问（消费 SSE 流）。
 *
 * 事件顺序是**后端定死的**（`app/api/chat.py` 的 `_stream`）：
 *
 *     sources  →  delta*  →  done
 *                 └─────────────→  error（随时可能插入，之后**照样**会来一个 done）
 *
 * ⚠️ **错误分两类，处理方式完全不同**（这是 SSE 的固有代价：
 *     状态码在流开始**之前**就发出去了，流一旦开始就再也改不了）：
 *
 *     · **流开始之前**出错（401 / 404 / 422）→ 正常 HTTP 错误，由 `apiFetch` 抛出 `ApiError`
 *     · **流开始之后**出错（模型 API 挂了、检索炸了）→ 只能发一个 `error` 事件，
 *       而且**已经吐出来的半截回答不能丢** —— 用户已经看到了
 */

import { ApiError, apiFetch, authHeaders } from './client'
import { consumeSse, type SseFrame } from './sse'
import type {
  CitationAudit,
  DoneEvent,
  ErrorEvent,
  ImageInfo,
  SourcesEvent,
  ToolEvent,
} from '@/types/api'

export interface StreamHandlers {
  /** 检索完成、**开始生成之前**。前端可以先把"参考来源"渲染出来。 */
  onSources?: (event: SourcesEvent) => void
  /** 一个增量片段。把它追加到当前回答上。 */
  onDelta?: (text: string) => void
  /** 收尾。⚠️ 它**不代表成功** —— 出错时 `error` 之后也会来一个 `done`。 */
  onDone?: (event: DoneEvent) => void
  /**
   * agent 在调工具（只有 `ANSWER_MODE=agent` 时才会有这个事件）。
   * ⚠️ 必须显示：agent 慢 2~4 倍，不告诉用户"在干一件事"会被当成卡死。
   */
  onTool?: (event: ToolEvent) => void
  /** 流中途出错。`detail` 是给开发者看的；界面上应当提示"回答不完整"。 */
  onError?: (event: ErrorEvent) => void
}

function parseJson(raw: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(raw)
    return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null
  } catch {
    // 解析不了就整帧丢掉 —— 但**不能让整个流挂掉**，后面的帧可能还是好的
    return null
  }
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * `sources` 里那张图的读数。
 *
 * ⚠️ 没有这个字段 = **这条消息没带图**（不是"带了但没读成"）—— 两者在界面上
 * 是完全不同的提示，所以这里宁可返回 `undefined` 也不返回 `{analyzed:false}`。
 */
function asImageInfo(value: unknown): ImageInfo | undefined {
  if (typeof value !== 'object' || value === null) return undefined
  const raw = value as Record<string, unknown>
  return {
    analyzed: raw.analyzed === true,
    note: typeof raw.note === 'string' ? raw.note : null,
    error: typeof raw.error === 'string' ? raw.error : null,
  }
}

/**
 * `done` 里的引用核对结果。
 *
 * ⚠️ `checked <= 0` 一律当**没有**（返回 `undefined`）—— 后端也只在 `checked > 0`
 * 时才带这个字段，两边口径一致的话，调用方只需要判 `undefined`。
 */
function asCitationAudit(value: unknown): CitationAudit | undefined {
  if (typeof value !== 'object' || value === null) return undefined
  const raw = value as Record<string, unknown>
  const checked = asNumber(raw.checked) ?? 0
  if (checked <= 0) return undefined
  return {
    checked,
    unsupported: asNumber(raw.unsupported) ?? 0,
    rate: asNumber(raw.rate) ?? 0,
    issues: Array.isArray(raw.issues) ? (raw.issues as CitationAudit['issues']) : [],
  }
}

function dispatch(frame: SseFrame, handlers: StreamHandlers): void {
  const payload = parseJson(frame.data)
  if (payload === null) return

  switch (frame.event) {
    case 'sources':
      handlers.onSources?.({
        citations: Array.isArray(payload.citations)
          ? (payload.citations as SourcesEvent['citations'])
          : [],
        confidence: asNumber(payload.confidence),
        refused: payload.refused === true,
        image: asImageInfo(payload.image),
      })
      break
    case 'delta':
      handlers.onDelta?.(asString(payload.text))
      break
    case 'tool':
      handlers.onTool?.({
        name: asString(payload.name),
        args: asString(payload.args),
        ok: payload.ok === true,
        ms: asNumber(payload.ms) ?? 0,
        summary: asString(payload.summary),
      })
      break
    case 'done': {
      const audit = asCitationAudit(payload.citation_audit)
      handlers.onDone?.({
        refused: payload.refused === true,
        confidence: asNumber(payload.confidence),
        latency_ms: asNumber(payload.latency_ms) ?? 0,
        citation_audit: audit,
      })
      break
    }
    case 'error':
      handlers.onError?.({
        detail: asString(payload.detail) || '生成过程中出错',
        stage: asString(payload.stage) || 'unknown',
      })
      break
    default:
      // ⚠️ **未知事件必须静默忽略**，不能抛错。
      //    否则后端将来加一个事件（比如 `title`），老前端会当场崩掉整个问答 ——
      //    而我们明明可以什么都不做。
      break
  }
}

/**
 * 提问并消费流式回答。
 *
 * @param signal 用 `AbortController` 取消（用户点"停止"或离开页面）。
 *   ⚠️ **取消不会撤销任何东西**：后端在生成器的 `finally` 里已经把
 *   **用户提问 + 已生成的那半截回答**落库了（那是刻意的，见 `chat.py` 的注释）。
 *   所以刷新页面会看到它 —— 这是对的行为，"我看到的"和"存下来的"应该一致。
 * @param imageId 可选：**先** `uploadImage()` 拿到的 id。带图时后端会读一次图。
 *   ⚠️ 图只对**这一条消息**生效。后续追问靠历史里的文字记忆（模型看不到图），
 *   所以追问"那多久到账"时它仍然知道刚才有张图 —— 靠的是 `meta.image_note`。
 */
export async function streamAnswer(
  conversationId: string,
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
  imageId?: string | null,
): Promise<void> {
  const response = await apiFetch(`/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // ⚠️ 声明我们接受事件流。后端不靠它判断（它无条件回 SSE），
      //    但网络中间件 / 未来的网关可能靠它决定怎么处理。
      Accept: 'text/event-stream',
      ...authHeaders(),
    },
    // ⚠️ `image_id` 显式给 `null`（而不是省略字段）：这是"我这条消息不带图"的
    //    明确表态，和"忘了传"区分开。
    body: JSON.stringify({ question, image_id: imageId ?? null }),
    signal,
  })

  if (!response.body) {
    throw new ApiError(response.status, '响应没有可读的流（浏览器不支持 ReadableStream？）')
  }

  await consumeSse(response.body, (frame) => dispatch(frame, handlers))
}
