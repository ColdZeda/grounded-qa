/** 知识库文档接口（对应 `app/api/documents.py`）。 */

import { apiJson } from './client'
import type { DocumentChunksOut, DocumentListOut, DocumentUploadOut } from '@/types/api'

/**
 * 上传一个文件。
 *
 * ⚠️ **不要自己写 `Content-Type`**：`FormData` 提交时边界串（boundary）由浏览器生成，
 *     手写 `multipart/form-data` 会把 boundary 弄丢，后端的 multipart 解析直接 400。
 *     所以这里**只**传 `FormData`，头交给 fetch 自己填。
 *
 * ⚠️ 返回 **202** = 已收下、还没做完。状态要靠轮询 `listDocuments` 看（`pending → ready`）。
 */
export function uploadDocument(file: File): Promise<DocumentUploadOut> {
  const form = new FormData()
  form.append('file', file)
  return apiJson<DocumentUploadOut>('/documents', { method: 'POST', body: form })
}

/** 文档列表（按创建时间倒序）。界面靠它轮询入库状态。 */
export function listDocuments(limit = 100): Promise<DocumentListOut> {
  return apiJson<DocumentListOut>(`/documents?limit=${limit}`)
}

/**
 * 「看原文」：某一块 ＋ 前后各 `window` 块（**永远不是整篇**）。
 *
 * ⚠️ **点了才请求**：会话默认视图一个字都不变，不点就零流量。
 */
export function getChunkContext(
  documentId: string,
  around: number,
  window = 1,
): Promise<DocumentChunksOut> {
  return apiJson<DocumentChunksOut>(
    `/documents/${documentId}/chunks?around=${around}&window=${window}`,
  )
}
