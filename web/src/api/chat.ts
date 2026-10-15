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
import type { DoneEvent, ErrorEvent, SourcesEvent } from '@/types/api'

export interface StreamHandlers {
  /** 检索完成、**开始生成之前**。前端可以先把"参考来源"渲染出来。 */
  onSources?: (event: SourcesEvent) => void
  /** 一个增量片段。把它追加到当前回答上。 */
  onDelta?: (text: string) => void
  /** 收尾。⚠️ 它**不代表成功** —— 出错时 `error` 之后也会来一个 `done`。 */
  onDone?: (event: DoneEvent) => void
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
      })
      break
    case 'delta':
      handlers.onDelta?.(asString(payload.text))
      break
    case 'done':
      handlers.onDone?.({
        refused: payload.refused === true,
        confidence: asNumber(payload.confidence),
        latency_ms: asNumber(payload.latency_ms) ?? 0,
      })
      break
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
 */
export async function streamAnswer(
  conversationId: string,
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
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
    body: JSON.stringify({ question }),
    signal,
  })

  if (!response.body) {
    throw new ApiError(response.status, '响应没有可读的流（浏览器不支持 ReadableStream？）')
  }

  await consumeSse(response.body, (frame) => dispatch(frame, handlers))
}
