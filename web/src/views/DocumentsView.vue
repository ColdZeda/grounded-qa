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
import ConflictsPanel from '@/components/ConflictsPanel.vue'
import { ApiError } from '@/api/client'
import { deleteDocument, listDocuments, uploadDocument } from '@/api/documents'
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

/**
 * 当前轮询间隔。正常是 1.5 秒；被限流（429）时**指数退避**到最多 15 秒。
 *
 * ⚠️ 为什么需要它：不处理 429 的话，页面上会跳一条红色的"请求太频繁"，
 *    而实际上入库**还在正常进行** —— 用户以为坏了，去重传，反而更糟。
 *    这类"其实没事"的错误提示，比不提示更伤。
 */
const pollDelay = ref(POLL_MS)

/**
 * 能不能做**写操作**（上传 / 替换 / 删除）。
 *
 * ⚠️ 演示账号是**只读管理员**：`role` 也是 admin（所以这一页看得见），
 *    但后端 `AdminWriter` 会把这些接口一律 403 ⟹ 前端也别把按钮摆出来，
 *    否则访客点一下吃个 403，看着像坏了。
 * ⚠️ 写成**函数**而不是 `computed`：模板里要用 `canWrite()` 这种**调用形式**
 * （`AGENTS §10`：裸标识符当整个 `v-if` 条件时会被编译器常量折叠，整段消失）。
 */
function canWrite(): boolean {
  return currentUser.value?.role === 'admin' && currentUser.value?.is_demo !== true
}

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

/**
 * **内容状态**的显示口径（2026-10-28 走查）。
 *
 * ⚠️ 为什么要单独一颗徽章：`已入库` 只说"切片和向量都好了"，**没说它能不能被检索引用**。
 *    现场就卡在这 —— 采纳出来的草稿还没发布，页面只写「已入库」，走查的人以为它已经在用了。
 */
const CONTENT_LABEL: Record<DocumentOut['content_status'], string> = {
  active: '现行',
  superseded: '已废止',
  draft: '草稿（不进检索）',
  sibling_brand: '同品牌他站',
  internal: '内部稿',
}

/** 只有这两种内容状态会被检索引用；其余一律当"不生效"标灰。 */
function contentIsLive(item: DocumentOut): boolean {
  return item.content_status === 'active' || item.content_status === 'superseded'
}

async function load(): Promise<void> {
  try {
    const page = await listDocuments()
    items.value = page.items
    total.value = page.total
    error.value = ''
    pollDelay.value = POLL_MS
  } catch (err) {
    if (err instanceof ApiError && err.status === 429) {
      // 限流不是"加载失败"：保持列表不动、安静退避
      pollDelay.value = Math.min(pollDelay.value * 2, 15_000)
    } else {
      error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
    }
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
    }, pollDelay.value)
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

/**
 * 「替换」＝ **先传新的、成功了再删旧的**（2026-10-25 定）。
 *
 * ⚠️ 顺序不能反：先删后传，一旦用户取消选择或上传失败，库里就**没有这份文档**了 ——
 *    那是最糟的结果（本来只是"想换一版"）。
 */
const replaceTarget = ref<DocumentOut | null>(null)

function startReplace(item: DocumentOut): void {
  replaceTarget.value = item
  fileInput.value?.click()
}

/** 删除一份文档（硬删、不可恢复）。⚠️ 一定要确认 —— 切片会一起没。 */
async function removeDoc(item: DocumentOut): Promise<void> {
  const ok = window.confirm(
    `确定删除「${item.filename}」吗？\n\n它的 ${item.chunk_count} 个切片会一起删除，且**无法恢复**。`,
  )
  if (!ok) return
  error.value = ''
  notice.value = ''
  try {
    await deleteDocument(item.id)
    notice.value = `已删除「${item.filename}」（连同 ${item.chunk_count} 个切片）。`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `删除失败：${(err as Error).message}`
  }
}

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) {
    replaceTarget.value = null // 取消了替换：旧文档**原样留着**
    return
  }

  const replacing = replaceTarget.value
  error.value = ''
  notice.value = ''
  uploading.value = true
  try {
    const result = await uploadDocument(file)
    notice.value = result.created
      ? `已收下「${file.name}」，正在后台上线，稍等几秒。`
      : `没有重复入库：${result.note}`
    // 新的真的进去了，才把旧的删掉
    if (replacing !== null && replacing.id !== result.id) {
      await deleteDocument(replacing.id)
      notice.value += ` 旧版「${replacing.filename}」已删除。`
    }
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `上传失败：${(err as Error).message}`
  } finally {
    uploading.value = false
    replaceTarget.value = null
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
    <AppHeader title="知识库" :hint="`共 ${total} 份`" tone="teal">
      <button
        v-if="canWrite()"
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
      <!-- 知识库体检（冲突检查）：2026-10-25 从「评测报告」搬来 —— 它是库的健康，不是评测分数 -->
      <ConflictsPanel />

      <p class="hint">
        只收 <code>.md</code> / <code>.txt</code>。上传后走队列异步入库（解析 → 切分 →
        向量化），下面能看到每一步的状态。
        <template v-if="!canWrite()">上传是管理员动作，你可以查看但不能新增。</template>
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <!-- 骨架屏：3 行，形状对齐下面的 `.row`（文件名 + 元信息 + 状态徽章） -->
      <div v-if="loading && items.length === 0" class="sk-stack" aria-busy="true" aria-label="加载中">
        <div v-for="n in 3" :key="n" class="sk sk--block"></div>
      </div>

      <p v-else-if="items.length === 0" class="empty">
        还没有文档。{{ canWrite() ? '点右上角「＋ 上传文档」开始。' : '' }}
      </p>

      <ul v-else class="list">
        <li v-for="item in items" :key="item.id" class="row">
          <div class="row__main">
            <span class="row__name" :title="item.filename">{{ item.filename }}</span>
            <span class="row__meta">
              {{ humanSize(item.size_bytes) }} ·
              <!-- 切成几块：切分质量是检索质量的第一道门，看不出来就没人会发现切坏了 -->
              <strong class="row__chunks">{{ item.chunk_count }} 块</strong> ·
              {{ relative(item.created_at) }}
            </span>
            <span v-if="item.error_message" class="row__err">{{ item.error_message }}</span>
          </div>
          <span class="badge" :class="`badge--${item.status}`">
            {{ STATUS_LABEL[item.status] }}
          </span>
          <!-- 内容状态：能不能被检索引用，看的是这一颗（不是上面那颗） -->
          <span
            class="badge"
            :class="contentIsLive(item) ? 'badge--ready' : 'badge--muted'"
            :title="
              contentIsLive(item)
                ? '这份文档的内容状态允许它被检索引用'
                : '这份文档不会被检索引用（草稿 / 内部稿 / 他站）'
            "
          >
            {{ CONTENT_LABEL[item.content_status] }}
          </span>
          <!-- 管理动作只给管理员；判断用**函数调用** `canWrite()`（AGENTS §10 编译期折叠坑） -->
          <div v-if="canWrite()" class="row__ops">
            <button
              class="row__op"
              type="button"
              :disabled="uploading"
              title="换一版：先上传新的，成功后再删掉这份旧的"
              @click="startReplace(item)"
            >
              替换
            </button>
            <button
              class="row__op row__op--danger"
              type="button"
              title="删除这份文档（连同切片，不可恢复）"
              @click="removeDoc(item)"
            >
              删除
            </button>
          </div>
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

/* 切块数：不是装饰 —— "这份文档被切成 1 块"通常意味着切分没生效 */
.row__chunks {
  color: var(--text-muted);
  font-weight: 500;
}

/* 管理动作（替换 / 删除）：平时低调，悬停才明显 —— 删除是不可逆的 */
.row__ops {
  display: flex;
  flex: 0 0 auto;
  gap: 6px;
}

.row__op {
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.76rem;
}

.row__op:hover:not(:disabled) {
  border-color: var(--border-strong);
  color: var(--text);
}

.row__op--danger:hover:not(:disabled) {
  border-color: var(--danger, #b42318);
  color: var(--danger, #b42318);
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
