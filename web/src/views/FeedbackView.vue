<script setup lang="ts">
/**
 * 反馈与采纳：**badcase 的出口**。
 *
 * 一条纠错反馈在这里走三步（D11）：
 *   ① 采纳 → 生成一份**草稿文档**（`content_status=draft`，**不进检索**）
 *   ② 审阅 → 看草稿全文（它搜不到，只能从这里看）
 *   ③ 发布 → 服务端先拿它和全库算一次**冲突体检**，有冲突默认拒绝发布
 *
 * ⚠️ **为什么不让"采纳"直接入库**：一条反馈只说明"这里错了"，不说明"对的是什么"。
 *    直接写库 = 把模型当时的错答换个地方存一遍，而且没人看过。
 *
 * ⚠️ 演示账号是**只读管理员**：这一页看得见（它属于治理清单），
 *    但采纳/发布的按钮不摆出来 —— 后端也拦（`AdminWriter`），摆出来只会让人吃 403。
 */

import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import {
  adoptFeedback,
  deleteFeedback,
  listFeedback,
  publishFeedback,
  readDraft,
  type ConflictItem,
  type DraftOut,
  type FeedbackAdminOut,
} from '@/api/feedback'
import { currentUser } from '@/stores/auth'

const loading = ref(true)
const error = ref('')
const notice = ref('')
const busyId = ref('')

/**
 * 三组反馈（2026-10-28 D2）：**按"哪里不对"分开**，各自翻页。
 *
 * ⚠️ 为什么分：三类的处理动作根本不同 —— 答案错要去改内容、引用错要去看串号、
 *    其他先得读一遍。混成一条时间线之后，管理员每次都在自己心里分拣。
 * ⚠️ 为什么各自 `limit`（**按组翻页**，六条硬规矩 ⑥）：一个组不能把配额吃光；
 *    也不做"拉全量在前端切"—— 那样一旦反馈多了，首屏就会慢得莫名其妙。
 */
type Verdict = FeedbackAdminOut['verdict']

interface Group {
  verdict: Verdict
  label: string
  hint: string
  items: FeedbackAdminOut[]
  limit: number
  loading: boolean
}

const PAGE_SIZE = 5

const groups = ref<Group[]>([
  {
    verdict: 'wrong_answer',
    label: '答案错',
    hint: '事实 / 结论不对 —— 采纳后要改的是内容',
    items: [],
    limit: PAGE_SIZE,
    loading: false,
  },
  {
    verdict: 'wrong_citation',
    label: '引用错',
    hint: '答案对、出处引错了 —— 多半是串号',
    items: [],
    limit: PAGE_SIZE,
    loading: false,
  },
  {
    verdict: 'other',
    label: '其他',
    hint: '说不清是哪一类，先读一遍',
    items: [],
    limit: PAGE_SIZE,
    loading: false,
  },
])

/** 扁平的视图（`loading` / `items.length` 这些判空逻辑只关心"有没有东西"）。 */
const items = computed(() => groups.value.flatMap((group) => group.items))

/** 正在编辑草稿的那条（`null` = 没有在编辑）。 */
const editing = ref<{ id: string; title: string; content: string } | null>(null)
/** 已经读出来的草稿正文（按反馈 id 存）。 */
const drafts = ref<Record<string, DraftOut>>({})
/** 发布被体检拦下来时，把冲突原文留在这一条上（不弹全局错误）。 */
const conflicts = ref<Record<string, ConflictItem[]>>({})

const VERDICT_LABEL: Record<FeedbackAdminOut['verdict'], string> = {
  wrong_answer: '答案错',
  wrong_citation: '引用错',
  other: '其他',
}

/** ⚠️ 函数调用形式（`AGENTS §10`：裸标识符当整个 `v-if` 条件会被编译期折叠）。 */
function canWrite(): boolean {
  return currentUser.value?.role === 'admin' && currentUser.value?.is_demo !== true
}

function statusOf(item: FeedbackAdminOut): string {
  if (item.published_at) return '已发布'
  if (item.adopted_document_id) return '待发布'
  return '未采纳'
}

function statusTone(item: FeedbackAdminOut): string {
  if (item.published_at) return 'ok'
  if (item.adopted_document_id) return 'warn'
  return 'idle'
}

const pending = computed(() => items.value.filter((item) => !item.published_at).length)

/** 拉某一组（`group.limit` 条）。刷新时三组一起重拉，各自的首屏都回到 5 条。 */
async function loadGroup(group: Group): Promise<void> {
  group.loading = true
  try {
    group.items = await listFeedback(group.limit, group.verdict)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    group.loading = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  for (const group of groups.value) group.limit = PAGE_SIZE
  await Promise.all(groups.value.map(loadGroup))
  loading.value = false
}

onMounted(load)

/** 「加载更多」：**只加这一组**的条数，其他组不动。 */
async function loadMore(group: Group): Promise<void> {
  group.limit += PAGE_SIZE
  await loadGroup(group)
}

/**
 * 删掉一条反馈（硬删、不可恢复）。
 *
 * ⚠️ **为什么需要它**：上报的不一定对 —— 误点、看错、事后发现是自己理解错。
 *    假阳性一直堆在治理清单里，比没有清单更糟（没人再愿意逐条看）。
 * ⚠️ 删反馈**不删**已经采纳出来的草稿文档 —— 提示里写清楚，别让人以为一起没了。
 */
async function removeFeedback(item: FeedbackAdminOut): Promise<void> {
  const extra = item.adopted_document_id
    ? '\n\n⚠️ 这条已经采纳过：删掉反馈**不会**删掉知识库里那份草稿文档（要删请去知识库页）。'
    : ''
  const ok = window.confirm(
    `确定删掉这条「${VERDICT_LABEL[item.verdict]}」反馈吗？\n\n无法恢复。${extra}`,
  )
  if (!ok) return
  busyId.value = item.id
  try {
    await deleteFeedback(item.id)
    notice.value = '已删掉这条反馈。'
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `删除失败：${(err as Error).message}`
  } finally {
    busyId.value = ''
  }
}

/** 开始采纳：**预填原答案**，让管理员在上面改，而不是从空白页开始写。 */
function startAdopt(item: FeedbackAdminOut): void {
  editing.value = { id: item.id, title: '', content: item.answer_snapshot }
  conflicts.value[item.id] = []
}

function cancelAdopt(): void {
  editing.value = null
}

async function confirmAdopt(): Promise<void> {
  const form = editing.value
  if (form === null) return
  busyId.value = form.id
  try {
    const out = await adoptFeedback(form.id, {
      title: form.title.trim() || null,
      content: form.content,
    })
    notice.value = `已生成草稿：${out.document_name}（${out.chunks} 块，${out.content_status}）${
      out.note ? ` —— ${out.note}` : ''
    }`
    editing.value = null
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `采纳失败：${(err as Error).message}`
  } finally {
    busyId.value = ''
  }
}

async function toggleDraft(item: FeedbackAdminOut): Promise<void> {
  if (drafts.value[item.id]) {
    delete drafts.value[item.id]
    return
  }
  busyId.value = item.id
  try {
    drafts.value[item.id] = await readDraft(item.id)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `读取草稿失败：${(err as Error).message}`
  } finally {
    busyId.value = ''
  }
}

async function doPublish(item: FeedbackAdminOut, force = false): Promise<void> {
  busyId.value = item.id
  try {
    const out = await publishFeedback(item.id, force)
    conflicts.value[item.id] = []
    notice.value = `已发布：${out.document_name}${out.forced ? '（体检报了冲突，你选择了强行发布）' : ''}`
    delete drafts.value[item.id]
    await load()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      // 409 是**预期的业务答案**（有冲突 / 已经发布过），不是故障：
      // 把冲突原文摊开来让人判断，别弹一句红字了事。
      const detail = err.detail as { conflicts?: ConflictItem[] } | null
      if (detail?.conflicts?.length) {
        conflicts.value[item.id] = detail.conflicts
        notice.value = err.message
      } else {
        error.value = err.message
      }
    } else {
      error.value = err instanceof ApiError ? err.message : `发布失败：${(err as Error).message}`
    }
  } finally {
    busyId.value = ''
  }
}

function relative(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const minutes = Math.round((Date.now() - then) / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时前`
  return `${Math.round(minutes / 1440)} 天前`
}
</script>

<template>
  <div class="shell">
    <AppHeader
      title="反馈与采纳"
      :hint="`治理清单 · 待处理 ${pending} 条`"
      tone="amber"
    >
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p class="hint">
        坐席点「⚑ 上报错误」后，记录会出现在这里。<strong>采纳</strong>只是生成一份草稿
        —— 草稿<strong>不进检索</strong>；点<strong>发布</strong>才会进知识库，发布前会先做一次冲突体检。
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <div v-if="loading && items.length === 0" class="sk-stack" aria-busy="true" aria-label="加载中">
        <div v-for="n in 3" :key="n" class="sk sk--block"></div>
      </div>

      <p v-else-if="items.length === 0" class="empty">
        还没有反馈。在会话里对某条回答点「⚑ 上报错误」，它就会出现在这里。
      </p>

      <div v-else class="groups">
        <section v-for="group in groups" :key="group.verdict" class="group">
          <div class="group__head">
            <h3 class="group__title">{{ group.label }}</h3>
            <span class="group__hint">{{ group.hint }}</span>
            <span class="group__count">{{ group.items.length }} 条</span>
          </div>
          <p v-if="group.items.length === 0" class="group__empty">这一组还没有反馈。</p>

          <ul v-else class="list">
            <li v-for="item in group.items" :key="item.id" class="card">
          <div class="card__head">
            <span class="badge" :class="`badge--${item.verdict === 'other' ? 'muted' : 'warn'}`">
              {{ VERDICT_LABEL[item.verdict] }}
            </span>
            <span class="badge" :class="`badge--${statusTone(item)}`">{{ statusOf(item) }}</span>
            <span class="card__time">{{ relative(item.created_at) }}</span>
            <span class="card__cites">引用 {{ item.citations_snapshot.length }} 条</span>
          </div>

          <p v-if="item.note" class="card__note">“{{ item.note }}”</p>

          <!--
            快照 = **客户问 + AI 答**（2026-10-28 D3）。
            ⚠️ 只给答案是不够判的：很多"答错了"其实是问得不清楚、或者上一轮的上下文没带上。
            ⚠️ 迁移前的老记录没有提问 ⟹ 明说"没存到"，别显示成空白（那看起来像客户没问）。
          -->
          <details class="card__snap">
            <summary>当时的问答（快照）</summary>
            <p class="snap__label">客户问</p>
            <p class="snap__text">
              <span v-if="item.question_snapshot">{{ item.question_snapshot }}</span>
              <span v-else class="snap__missing">（这条反馈收集时还没存提问）</span>
            </p>
            <p class="snap__label">AI 答</p>
            <pre class="card__pre">{{ item.answer_snapshot }}</pre>
          </details>

          <!-- 顺着去看上下文（现在只能跳到会话，不是精确定位到某一条） -->
          <RouterLink
            v-if="item.conversation_id"
            class="card__open"
            :to="{ name: 'chat', params: { id: item.conversation_id } }"
          >
            打开原会话 →
          </RouterLink>

          <!-- 采纳表单：预填原答案，管理员在上面直接改 -->
          <div v-if="editing && editing.id === item.id" class="form">
            <label class="form__label">
              标题（留空则自动取答案首句）
              <input v-model="editing.title" class="form__input" type="text" maxlength="120" />
            </label>
            <label class="form__label">
              更正后的说法（这段会进知识库，请写成对外口径，别写"我原来答错了"）
              <textarea v-model="editing.content" class="form__area" rows="8"></textarea>
            </label>
            <div class="form__ops">
              <button
                class="btn btn--primary btn--sm"
                type="button"
                :disabled="busyId === item.id"
                @click="confirmAdopt()"
              >
                生成草稿
              </button>
              <button class="btn btn--ghost btn--sm" type="button" @click="cancelAdopt()">
                取消
              </button>
            </div>
          </div>

          <!-- 草稿正文（草稿搜不到，只能从这里看） -->
          <div v-if="drafts[item.id]" class="draft">
            <div class="draft__head">
              草稿 {{ drafts[item.id].document_name }} · {{ drafts[item.id].chunks }} 块 ·
              {{ drafts[item.id].content_status }}
            </div>
            <pre class="card__pre">{{ drafts[item.id].content }}</pre>
          </div>

          <!-- 冲突体检结果 -->
          <div v-if="conflicts[item.id]?.length" class="conf">
            <p class="conf__title">
              体检发现 {{ conflicts[item.id].length }} 处口径冲突 —— 看过原文再决定要不要发：
            </p>
            <div v-for="(conflict, index) in conflicts[item.id]" :key="index" class="conf__item">
              <div class="conf__head">{{ conflict.topic }}（{{ conflict.scope }}）</div>
              <div class="conf__side">A {{ conflict.left.doc }}：{{ conflict.left.sentence }}</div>
              <div class="conf__side">B {{ conflict.right.doc }}：{{ conflict.right.sentence }}</div>
            </div>
            <button
              class="btn btn--danger btn--sm"
              type="button"
              :disabled="busyId === item.id"
              @click="doPublish(item, true)"
            >
              我知道了，仍然发布
            </button>
          </div>

          <!--
            ⚠️ 正在编辑草稿时**不显示**这一排：否则下面同时挂着「生成草稿 / 取消」
               和「采纳为草稿」，看起来像两个功能（走查里就点懵过一次）。
          -->
          <div
            v-if="canWrite() && !(editing && editing.id === item.id)"
            class="card__ops"
          >
            <button
              v-if="!item.adopted_document_id"
              class="btn btn--primary btn--sm"
              type="button"
              :disabled="busyId === item.id"
              @click="startAdopt(item)"
            >
              采纳为草稿
            </button>
            <template v-else>
              <button
                class="btn btn--ghost btn--sm"
                type="button"
                :disabled="busyId === item.id"
                @click="toggleDraft(item)"
              >
                {{ drafts[item.id] ? '收起草稿' : '审阅草稿' }}
              </button>
              <button
                v-if="!item.published_at"
                class="btn btn--primary btn--sm"
                type="button"
                :disabled="busyId === item.id"
                @click="doPublish(item)"
              >
                发布
              </button>
            </template>
            <!-- 上报的不一定对：假阳性要能清掉，否则清单没人看 -->
            <button
              class="btn btn--ghost btn--sm btn--danger-text"
              type="button"
              title="删掉这条反馈（不可恢复；不会删掉已采纳的草稿文档）"
              :disabled="busyId === item.id"
              @click="removeFeedback(item)"
            >
              删除
            </button>
          </div>

          <p class="card__trail">
            <span v-if="item.adopted_by_email">
              采纳 {{ item.adopted_by_email }} · {{ item.adopted_at ? relative(item.adopted_at) : '—' }}
            </span>
            <span v-if="item.published_by_email">
              ｜ 发布 {{ item.published_by_email }} ·
              {{ item.published_at ? relative(item.published_at) : '—' }}
            </span>
          </p>
        </li>
          </ul>

          <button
            v-if="group.items.length > 0 && group.items.length >= group.limit"
            class="btn btn--ghost btn--sm group__more"
            type="button"
            :disabled="group.loading"
            @click="loadMore(group)"
          >
            {{ group.loading ? '加载中…' : '加载更多' }}
          </button>
        </section>
      </div>
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

/* 三组：每小题一行组头（标题 + 一句说明 + 条数），组内各自翻页 */
.groups {
  display: flex;
  flex-direction: column;
  gap: 26px;
}

.group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.group__head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--line);
}

.group__title {
  margin: 0;
  font-size: 0.95rem;
}

.group__hint {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.group__count {
  margin-left: auto;
  color: var(--text-muted);
  font-size: 0.78rem;
}

.group__empty {
  margin: 0;
  color: var(--text-faint);
  font-size: 0.85rem;
}

.group__more {
  align-self: flex-start;
}

/* 快照的两段（客户问 / AI 答）：**标签一行、内容一行**，两边长得一样 */
.snap__label {
  margin: 8px 0 2px;
  color: var(--text-muted);
  font-size: 0.75rem;
}

.snap__text {
  margin: 0 0 6px;
  white-space: pre-wrap;
}

.snap__missing {
  color: var(--text-faint);
}

.card__open {
  display: inline-block;
  margin-top: 8px;
  font-size: 0.82rem;
  color: var(--primary);
  text-decoration: none;
}

.card__open:hover {
  text-decoration: underline;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  padding: 14px 16px;
}

.card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.card__time,
.card__cites {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.card__note {
  margin: 10px 0 0;
  font-size: 0.9rem;
  line-height: 1.7;
}

.card__snap {
  margin-top: 10px;
  font-size: 0.82rem;
  color: var(--text-muted);
}

.card__snap summary {
  cursor: pointer;
}

.card__pre {
  margin: 8px 0 0;
  padding: 10px 12px;
  max-height: 260px;
  overflow: auto;
  background: var(--surface-sunken);
  border-radius: 8px;
  font-size: 0.8rem;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.card__ops {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

/* 「删除」：不抢眼但认得出（红字、无底色）—— 它是破坏性动作，不是主按钮 */
.btn--danger-text {
  color: var(--danger);
}

.btn--danger-text:hover {
  border-color: var(--danger);
}

.card__trail {
  margin: 10px 0 0;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.form {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.form__label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.form__input,
.form__area {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 0.85rem;
  font-family: inherit;
  background: var(--surface);
  color: var(--text);
}

.form__area {
  resize: vertical;
  line-height: 1.7;
}

.form__ops {
  display: flex;
  gap: 8px;
}

.draft {
  margin-top: 12px;
}

.draft__head {
  font-size: 0.78rem;
  color: var(--text-muted);
}

.conf {
  margin-top: 12px;
  border-left: 3px solid var(--warn);
  padding: 8px 0 8px 12px;
}

.conf__title {
  margin: 0 0 8px;
  font-size: 0.85rem;
}

.conf__item {
  margin-bottom: 8px;
  font-size: 0.8rem;
  line-height: 1.65;
}

.conf__head {
  font-weight: 600;
}

.conf__side {
  color: var(--text-muted);
}
</style>
