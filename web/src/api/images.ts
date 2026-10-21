/**
 * 图片接口（对应 `app/api/images.py`）。
 *
 * 用法是**两步**：先 `POST /images` 拿到 `image_id`，提问时把它带上。
 * ⚠️ 图**不进聊天请求**：base64 塞进 JSON 会撑爆请求体，而且会进访问日志。
 *
 * ⚠️⚠️ **取图不能写成 `<img src="/api/v1/images/xxx">`。**
 *     `<img>` 发的是浏览器自己管理的普通 GET，**带不了 `Authorization` 头**，
 *     而我们的接口要 Bearer —— 直接写 src 只会拿到 401，页面上是一个碎图标。
 *     所以只能 `fetch`（走 `apiFetch`，它会加头）→ `blob()` → `URL.createObjectURL`。
 *     这是"图片也走鉴权"的必然代价，不是绕路。
 */

import { apiFetch, apiJson } from './client'
import type { ImageOut } from '@/types/api'

/**
 * 大小上限，**必须和后端 `settings.image_max_bytes` 一致**。
 * ⚠️ 前端先拦一次不是为了安全（后端才是权威），是为了**省一次白跑的 3 MB 上传**：
 *     用户等到进度条跑完才被告知"超了"，体验很差。
 */
export const IMAGE_MAX_BYTES = 3 * 1024 * 1024

/** 给 `<input accept>` 用。⚠️ 和 `app/images.py` 的 `ALLOWED_FORMATS` 对齐。 */
export const IMAGE_ACCEPT = 'image/png,image/jpeg,image/webp'

const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

/**
 * 本地预检。返回**给人看的一句话**，没问题返回 `null`。
 *
 * ⚠️ `file.type` 是**浏览器按扩展名猜的**，可以撒谎 —— 但真正的把关在后端
 *     （`app/images.py::sniff` 是看文件头字节）。这里只做 UX。
 */
export function describeImageProblem(file: File): string | null {
  if (!ALLOWED_TYPES.has(file.type)) {
    return '只支持 PNG / JPEG / WEBP 三种格式'
  }
  if (file.size > IMAGE_MAX_BYTES) {
    const mb = (file.size / 1024 / 1024).toFixed(1)
    return `这张图 ${mb} MB，超过 3 MB，先压缩一下再发`
  }
  if (file.size === 0) {
    return '这个文件是空的'
  }
  return null
}

/**
 * 上传一张图。
 *
 * ⚠️ **不要自己写 `Content-Type`**：`FormData` 的 boundary 由浏览器生成，
 *     手写会把 boundary 弄丢（和 `uploadDocument` 同一条）。
 * ⚠️ 返回 **201**（不是 202）：图是**同步**落盘的，回来就能用 —— 不需要轮询状态。
 */
export function uploadImage(file: File): Promise<ImageOut> {
  const form = new FormData()
  form.append('file', file)
  return apiJson<ImageOut>('/images', { method: 'POST', body: form })
}

/**
 * `image_id` → 可以直接放进 `<img src>` 的 blob URL。
 *
 * 缓存是按 `image_id` 存的：同一张图在"刚发出去的那条消息"和"刷新后重新加载的消息"里
 * 是同一个 id，只该下载一次。
 *
 * ⚠️ **这些 URL 不回收**（`URL.revokeObjectURL` 没调）。判断依据：一个会话里看到的图
 *     是几十张量级，浏览器存的是**压缩后的 Blob**（不是解码后的位图，解码是渲染时才做
 *     且离屏会被丢弃）。为了省这点内存去写引用计数，风险（图突然变空白）比收益大。
 *     📌 如果哪天图片量级上去了，再按 LRU 回收。
 */
const urlCache = new Map<string, Promise<string>>()

async function download(id: string): Promise<string> {
  const resp = await apiFetch(`/images/${id}`, { headers: { Accept: 'image/*' } })
  const blob = await resp.blob()
  return URL.createObjectURL(blob)
}

export function imageUrl(id: string): Promise<string> {
  const cached = urlCache.get(id)
  if (cached) return cached

  const task = download(id)
  urlCache.set(id, task)
  // ⚠️ 失败**不能留在缓存里**：下次重试会直接拿到那个失败的 Promise，
  //    用户永远看到碎图标，刷新页面才好 —— 极难排查。
  task.catch(() => urlCache.delete(id))
  return task
}

/**
 * 把**本地预览**挂到某个 id 上（上传成功后调）。
 *
 * 这样刚发出去的那条消息不用再下载一遍自己刚上传的图 —— 尤其是手机拍的 3 MB 图，
 * 在弱网下这一趟要等好几秒，而图明明就在用户眼前。
 */
export function primeImageUrl(id: string, url: string): void {
  urlCache.set(id, Promise.resolve(url))
}
