/**
 * SSE（Server-Sent Events）帧的解析。
 *
 * ⚠️ **这里没有用 `EventSource`，因为 `EventSource` 用不了。**
 *
 *     `EventSource` 只能发 **GET**，参数只能塞在 URL 里 —— 而我们的接口是
 *     `POST /conversations/{id}/messages` 带一个 JSON body。
 *     把提问塞进 query string 有三个问题：长度受限、会进 nginx access log、
 *     而且不符合"写操作的语义"。
 *
 *     所以只能拿 `fetch` 把响应当**裸字节流**读，然后自己按 SSE 规范切帧。
 *     下面这个模块就是那件事。**零依赖、不碰 DOM**，所以能单独测。
 *
 * ⚠️ **自己切帧时最容易错的三件事**（下面逐个处理了）：
 *     1. **帧会被网络切开** —— 一次 `read()` 可能只拿到半帧，也可能一次拿到三帧。
 *        所以必须"攒缓冲区 → 切完整帧 → 剩下的留着"。
 *     2. **多字节字符也会被切开** —— 一个汉字 3 字节，切在中间就解出 `�`。
 *        `TextDecoder` 必须带 `{ stream: true }`。
 *     3. **换行是字段分隔符，不是内容** —— 所以是"空行"分隔帧，
 *        而不是"每行一帧"。（后端把 JSON 里的换行转义掉，也是同一个原因。）
 */

/** 一帧：事件名 + 数据（数据是**单行 JSON 字符串**，还没 parse）。 */
export interface SseFrame {
  event: string
  data: string
}

/**
 * 帧分隔符：**空行**。
 *
 * ⚠️ `\r\n\r\n` 必须排在 `\n\n` 前面 —— 正则的备选分支是从左到右试的，
 *     顺序反了会在 `\r\n\r\n` 上先匹配到中间的 `\n\r`... 实际上 `\n\n` 匹配不到
 *     `\r\n\r\n`（中间隔着 `\r`），但保持"长的在前"这个习惯更不容易出错。
 */
const FRAME_SEPARATOR = /\r\n\r\n|\n\n|\r\r/

/**
 * 解析**单帧**的原始文本。
 *
 * @returns 没有 `data:` 字段时返回 `null`（例如只有一个注释行的心跳帧）。
 */
export function parseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const dataLines: string[] = []

  // 行分隔符允许 \r\n / \n / \r 三种（规范如此）
  for (const line of raw.split(/\r\n|\n|\r/)) {
    // 空行：帧内不该出现（它已经在切帧时被吃掉了），防御性跳过
    if (line === '') continue
    // 以冒号开头的是**注释**。SSE 用它做心跳保活 —— 必须忽略，不能当事件发出去
    if (line.startsWith(':')) continue

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)

    // ⚠️ 规范规定：冒号后**最多去掉一个**前导空格。
    //    `data:  x` 的值是 `" x"`（带一个空格），不是 `"x"`。
    //    用 `trimStart()` 会把数据里的空格吃掉 —— 对 JSON 无害，但那是错的。
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') {
      event = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
    // `id:` / `retry:` 忽略：那是**断线重连**用的（浏览器会自动补发 `Last-Event-ID`）。
    // 我们是 POST 流，重连也无从"续传"，所以后端根本不发这两个字段。
  }

  if (dataLines.length === 0) return null
  // 规范：多个 data 行用 \n 连接成一个值
  return { event, data: dataLines.join('\n') }
}

/**
 * 从缓冲区里切出**所有完整帧**，把剩下的半截还回去。
 *
 * 纯函数 —— 不碰网络、不碰 DOM，所以可以直接拿字符串喂它验证边界情况。
 */
export function splitFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = []
  let rest = buffer

  for (;;) {
    const match = FRAME_SEPARATOR.exec(rest)
    if (!match) break
    const raw = rest.slice(0, match.index)
    rest = rest.slice(match.index + match[0].length)
    const frame = parseFrame(raw)
    if (frame) frames.push(frame)
  }

  return { frames, rest }
}

/**
 * 把整个响应体读完，逐个派发完整帧。
 *
 * ⚠️ 结束时**故意丢弃**缓冲区里的残余：SSE 规范规定，流在最后一个空行之前结束的话，
 *     那个不完整的帧**不予派发**。后端每帧都以 `\n\n` 收尾，所以正常情况这里本来就是空的。
 *     —— "看起来更宽容"地把它也解析掉，反而会把半截 JSON 当成一帧，报一个看不懂的解析错。
 */
export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  const reader = body.getReader()
  // ⚠️ `{ stream: true }` 不是可选项：见文件头第 2 条。
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const { frames, rest } = splitFrames(buffer)
    buffer = rest
    for (const frame of frames) onFrame(frame)
  }
}
