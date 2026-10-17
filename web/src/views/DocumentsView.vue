<script setup lang="ts">
/**
 * 知识库：上传 + 文档列表（含入库状态）。
 *
 * ⚠️ **上传接口立刻返回（202），文档还没进库** —— 状态要**轮询**。
 *    所以这里有一个只在"有文档在跑"时才启动的定时器：全部到位就停，
 *    不能让一个 1.5 秒的请求永久跑在一个打开的页面上。
 *
 * ⚠️ **判重命中不是错误**（`created: false`）：同一份内容已经在库里，
 *    界面要说清"没有重复入库"，而不是弹一个红色的错。
 */

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { listDocuments, uploadDocument } from '@/api/documents'
import { currentUser } from '@/stores/auth'
import type { DocumentOut } from '@/types/api'

/** 轮询间隔。入库是秒级的事（切分 + embedding），1.5 秒足够，也不至于打爆后端。 */
const POLL_MS = 1500

const items = ref<DocumentOut[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

let timer: number | null = null

const isAdmin = computed(() => currentUser.value?.role === 'admin')

/** 还有文档在路上吗？有就继续轮询，没有就停下来。 */
const inFlight = computed(() =>
  items.value.some((item) => item.status === 'pending' || item.status === 'processing'),
)

const STATUS_LABEL: Record<DocumentOut['status'], string> = {
  pending: '排队中',
  processing: '入库中',
  ready: '已入库',
  failed: '失败',
}

async function load(): Promise<void> {
  try {
    const page = await listDocuments()
    items.value = page.items
    total.value = page.total
    error.value = ''
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
  schedule()
}

/** 只在需要时挂定时器；已经在跑就不重复挂（否则越点越多条）。 */
function schedule(): void {
  if (inFlight.value && timer === null) {
    timer = window.setTimeout(() => {
      timer = null
      void load()
    }, POLL_MS)
  }
}

/**
 * ⚠️ 离开页面必须清掉定时器。不清的话，组件已经卸载、请求还在发，
 *    每次跳走再回来就多一条链 —— 表现是"越用越卡"，而且看不出是谁在请求。
 */
onBeforeUnmount(() => {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
})

/** 打开系统文件选择框（真正的控件是隐藏的 input）。 */
function pickFile(): void {
  fileInput.value?.click()
}

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  error.value = ''
  notice.value = ''
  uploading.value = true
  try {
    const result = await uploadDocument(file)
    notice.value = result.created
      ? `已收下「${file.name}」，正在后台上线，稍等几秒。`
      : `没有重复入库：${result.note}`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `上传失败：${(err as Error).message}`
  } finally {
    uploading.value = false
    // 清掉选择，否则再选同一个文件不会触发 change 事件
    if (fileInput.value) fileInput.value.value = ''
  }
}

function humanSize(bytes: number | null): string {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function relative(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diffSec = Math.round((Date.now() - then) / 1000)
  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <AppHeader>
      <button
        v-if="isAdmin"
        class="btn btn--sm"
        type="button"
        :disabled="uploading"
        @click="pickFile"
      >
        {{ uploading ? '上传中…' : '＋ 上传文档' }}
      </button>
      <!--
        ⚠️ 文件选择框**用按钮去 `click()` 它**，而不是把它包在 `<label>` 里。
           两种都常见，但按钮这条路只依赖一件事（元素的 click 事件），
           而 label 那条要依赖"label 激活行为 + 隐藏控件的兼容性"——
           后者在 `display:none` / `pointer-events:none` 的边界上各浏览器历史上不一致。
      -->
      <input
        ref="fileInput"
        class="picker"
        type="file"
        accept=".md,.markdown,.txt"
        :disabled="uploading"
        @change="onPick"
      />
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <h2 class="title">知识库 <span class="count">共 {{ total }} 份</span></h2>

      <p class="hint">
        只收 <code>.md</code> / <code>.txt</code>。上传后走队列异步入库（解析 → 切分 →
        向量化），下面能看到每一步的状态。
        <template v-if="!isAdmin">上传是管理员动作，你可以查看但不能新增。</template>
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <p v-if="loading && items.length === 0" class="muted">加载中…</p>

      <p v-else-if="items.length === 0" class="empty">
        还没有文档。{{ isAdmin ? '点右上角「＋ 上传文档」开始。' : '' }}
      </p>

      <ul v-else class="list">
        <li v-for="item in items" :key="item.id" class="row">
          <div class="row__main">
            <span class="row__name" :title="item.filename">{{ item.filename }}</span>
            <span class="row__meta">{{ humanSize(item.size_bytes) }} · {{ relative(item.created_at) }}</span>
            <span v-if="item.error_message" class="row__err">{{ item.error_message }}</span>
          </div>
          <span class="badge" :class="`badge--${item.status}`">
            {{ STATUS_LABEL[item.status] }}
          </span>
        </li>
      </ul>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.body {
  flex: 1;
  overflow-y: auto;
  width: 100%;
  max-width: 820px;
  margin: 0 auto;
  padding: 28px 20px 60px;
}

.title {
  margin: 0 0 8px;
  font-size: 1.1rem;
}

.count {
  color: var(--text-faint);
  font-size: 0.8rem;
  font-weight: 400;
}

.hint {
  margin: 0 0 18px;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.7;
}

.hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--primary-soft);
  font-size: 0.8rem;
}

/* 文件选择框藏起来（真正的入口是上面那个按钮） */
.picker {
  display: none;
}

.muted {
  color: var(--text-muted);
}

.empty {
  padding: 40px 0;
  color: var(--text-faint);
  text-align: center;
}

.list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.row__main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.row__name {
  overflow: hidden;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row__meta {
  color: var(--text-faint);
  font-size: 0.78rem;
}

/* 失败原因**必须显眼**：不然用户只看到"失败"两个字，不知道该怎么办 */
.row__err {
  color: var(--danger, #c0392b);
  font-size: 0.78rem;
  line-height: 1.5;
}

.badge {
  flex-shrink: 0;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 0.75rem;
  white-space: nowrap;
}

.badge--pending,
.badge--processing {
  background: var(--primary-soft);
  color: var(--primary);
}

.badge--ready {
  background: #e6f4ea;
  color: #1e7e34;
}

.badge--failed {
  background: #fdecea;
  color: #c0392b;
}
</style>
