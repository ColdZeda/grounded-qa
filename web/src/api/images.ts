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
 * 大小上限，**必须和后端 `settings.image_max_bytes` 一致**（现在是 8 MB）。
 * ⚠️ 前端先拦一次不是为了安全（后端才是权威），是为了**省一次白跑的上传**：
 *     用户等到进度条跑完才被告知"超了"，体验很差。
 * ⚠️ 后端那个值还必须**小于 nginx 的 `client_max_body_size`**（现在是 12m）——
 *     否则超限的请求会被 nginx 用**它自己的 HTML 错误页**挡回来，前端解析不了，
 *     症状是"本地传得上去、部署后传不上"。见 `deploy/nginx/grounded-qa.conf`。
 */
export const IMAGE_MAX_BYTES = 8 * 1024 * 1024

/** 给 `<input accept>` 用。⚠️ 和 `app/images.py` 的 `ALLOWED_FORMATS` 对齐。 */
export const IMAGE_ACCEPT = 'image/png,image/jpeg,image/webp'

const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

/**
 * 苹果相机的默认格式，浏览器基本解不开、后端也不收。
 * 单独给一句能听懂的话 —— 只说"只支持三种格式"，iPhone 用户不知道该干什么。
 */
const HEIC_TYPES = new Set(['image/heic', 'image/heif'])

/**
 * 本地预检。返回**给人看的一句话**，没问题返回 `null`。
 *
 * ⚠️ `file.type` 是**浏览器按扩展名猜的**，可以撒谎 —— 但真正的把关在后端
 *     （`app/images.py::sniff` 是看文件头字节）。这里只做 UX。
 * ⚠️ **别在这里替用户决定"太大就别传"**：真正的判断在 `squeezeForUpload()`
 *     （长得再大，压一下往往就够了）。
 */
export function describeImageProblem(file: File): string | null {
  if (HEIC_TYPES.has(file.type)) {
    return 'HEIC 是苹果相机的默认格式，浏览器里压不动、后端也不收 —— 先导出成 JPG 再发'
  }
  if (!ALLOWED_TYPES.has(file.type)) {
    return '只支持 PNG / JPEG / WEBP 三种格式'
  }
  if (file.size > IMAGE_MAX_BYTES) {
    return `这张图 ${formatBytes(file.size)}，超过 ${IMAGE_MAX_BYTES / 1024 / 1024} MB —— 先裁一下再发`
  }
  if (file.size === 0) {
    return '这个文件是空的'
  }
  return null
}

// ---------------------------------------------------------------- 上传前压缩

/** 压缩目标：长边 ≤ 1600，JPEG q0.85。和后端"给读图模型的那份副本"是同一组数。 */
export const COMPRESS_MAX_EDGE = 1600
export const COMPRESS_QUALITY = 0.85

/**
 * "不用压"的第二道条件（第一道是长边 ≤1600）。
 *
 * ⚠️ 这个 1 MB 不是随手定的：它**正好是后端 `images.to_model_copy()` 原样透传的条件**。
 *    于是"前端判断不用压"⟹"送模型的那份就是原图"，不会出现**压两遍**（每编一次都会掉质量）。
 */
const PASSTHROUGH_BYTES = 1024 * 1024

/** `squeezeForUpload()` 的结果。`note` 是给界面的**一行说明**，不是给机器看的。 */
export interface SqueezedImage {
  file: File
  note: string
}

/**
 * 上传前把图压到"够用就行"，返回 `null` 表示**不用压、直接传原图**。
 *
 * 为什么在**前端**压（后端本来也会缩一份给模型）：
 * 后端那份只喂给模型，**落盘的始终是原图**，取回时也是原图 —— 一张 6 MB 的截图
 * 会变成「上传 3 秒 ＋ 存 6 MB ＋ 每次刷新页面再下 6 MB」。前端压一次，这三笔全省掉。
 *
 * ⚠️ **已经够小就一个字节都不动**（长边 ≤1600 且 ≤1 MB）：重编码只有损失没有收益，
 *    而且截图上的**细笔画文字**在 JPEG 里会起振铃（"1/7""8/B" 容易糊）。
 *    日常 Ctrl+V 贴上来的截图绝大多数落在这条路径上。
 * ⚠️ **压完反而更大就退回原图**：描边 / 纯色截图重编码成 JPEG 完全可能变大。
 * ⚠️ **透明区填白底**：JPEG 没有 alpha，canvas 初始是**透明黑**，不铺白底的话
 *    带透明的截图会变成一块黑框。
 * ⚠️ **EXIF 方向要烤进像素**（`imageOrientation: 'from-image'`）：手机照片是
 *    "横着存 + 一个标记说请转 90°"。不转正，界面上就是一张躺倒的图，而且不报错。
 * ⚠️ **解不开就 throw**，由调用方决定怎么办（现在是"照原图传"，让后端给权威报错）。
 */
export async function squeezeForUpload(file: File): Promise<SqueezedImage | null> {
  const source = await decode(file)
  try {
    const longEdge = Math.max(source.width, source.height)
    if (longEdge <= COMPRESS_MAX_EDGE && file.size <= PASSTHROUGH_BYTES) return null

    const scale = longEdge > COMPRESS_MAX_EDGE ? COMPRESS_MAX_EDGE / longEdge : 1
    const width = Math.max(1, Math.round(source.width * scale))
    const height = Math.max(1, Math.round(source.height * scale))

    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('浏览器不支持 canvas，压不了图')

    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, width, height)
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(source.image, 0, 0, width, height)

    const blob = await toJpeg(canvas)
    if (blob.size >= file.size) return null
    return {
      file: new File([blob], renameToJpg(file.name), { type: 'image/jpeg' }),
      note: `${formatBytes(file.size)} → ${formatBytes(blob.size)}`,
    }
  } finally {
    source.release()
  }
}

interface Decoded {
  image: CanvasImageSource
  width: number
  height: number
  /** 释放底层资源（`ImageBitmap.close()` / `revokeObjectURL()`）。 */
  release: () => void
}

/**
 * 把 `File` 解成能画进 canvas 的东西。
 *
 * ⚠️ 首选 `createImageBitmap`：它的 `imageOrientation: 'from-image'` 是**明确要求**
 *    浏览器把 EXIF 方向应用掉。老浏览器（或不认这个选项的）退到 `<img>` ——
 *    现代浏览器的 `<img>` 默认也按 EXIF 摆正，所以两条路的结果一致。
 */
async function decode(file: File): Promise<Decoded> {
  if (typeof createImageBitmap === 'function') {
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
      return {
        image: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        release: () => bitmap.close(),
      }
    } catch {
      // 这个格式它解不开、或者不认上面的选项 → 退到 <img> 再试一次
    }
  }

  const url = URL.createObjectURL(file)
  const img = new Image()
  img.src = url
  try {
    await img.decode()
  } catch (err) {
    URL.revokeObjectURL(url)
    throw err
  }
  return {
    image: img,
    width: img.naturalWidth,
    height: img.naturalHeight,
    release: () => URL.revokeObjectURL(url),
  }
}

function toJpeg(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('压缩后的图片生成失败'))),
      'image/jpeg',
      COMPRESS_QUALITY,
    )
  })
}

/** `截图.png` → `截图.jpg`。后端按内容认格式，这里只是让文件名别撒谎。 */
function renameToJpg(name: string): string {
  const base = name.replace(/\.[^./\\]+$/, '')
  return `${base || 'image'}.jpg`
}

/** 给人和界面看的体积。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ---------------------------------------------------------------- 上传 / 取图

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
 * 这样刚发出去的那条消息不用再下载一遍自己刚上传的图 —— 尤其是手机拍的几 MB 图，
 * 在弱网下这一趟要等好几秒，而图明明就在用户眼前。
 */
export function primeImageUrl(id: string, url: string): void {
  urlCache.set(id, Promise.resolve(url))
}
