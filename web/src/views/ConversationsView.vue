<script setup lang="ts">
/**
 * 会话列表。
 *
 * ⚠️ "新建会话"**不在这里调后端** —— 它只是跳到 `/conversations/new`。
 *    会话在用户**发出第一条提问**时才真正创建（见 `ChatView` 的 `send`），
 *    这样列表里显示的就是提问本身，而不是一排"新会话"。
 *    顺带好处：点进去又退出来不会留下一堆空会话。
 *
 * ⚠️ **按渠道分组是必需的，不是装饰**（走查 P1-⑥）：
 *    群聊按 `open_id` 分桶后，**群里 10 个人问过就有 10 条会话**。
 *    不分组的话，列表很快就被"飞书用户 · 3a9c1f"淹没，坐席找不到自己要的那条。
 */

import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { deleteConversation, listConversations } from '@/api/conversations'
import type { Channel, ConversationOut } from '@/types/api'

const router = useRouter()

const items = ref<ConversationOut[]>([])
const loading = ref(true)
const error = ref('')

/** 渠道 → 三色标签。⚠️ **同时带文字**：只靠颜色的话，色弱用户和打印稿都读不出来。 */
const CHANNEL_LABEL: Record<Channel, string> = {
  web: '内部',
  feishu_p2p: '私聊',
  feishu_group: '群聊',
}

interface Group {
  key: string
  kind: Channel
  /** 分组标题。群聊是「群名（N 人）」。 */
  label: string
  items: ConversationOut[]
}

/**
 * 三级分组：**内部（Web）／飞书私聊／飞书群聊 · 群名（N 人）**。
 *
 * ⚠️ 群聊的分组键是 `external_chat_id`（同一个群），**不是** `external_title`：
 *    群名是缓存的、可能重名，而 chat_id 不会。标题只用来显示。
 */
const groups = computed<Group[]>(() => {
  const out: Group[] = []
  const web = filtered.value.filter((item) => item.channel === 'web')
  const p2p = filtered.value.filter((item) => item.channel === 'feishu_p2p')
  if (web.length) out.push({ key: 'web', kind: 'web', label: '内部（Web）', items: web })
  if (p2p.length) out.push({ key: 'p2p', kind: 'feishu_p2p', label: '飞书私聊', items: p2p })

  const byChat = new Map<string, ConversationOut[]>()
  for (const item of filtered.value.filter((one) => one.channel === 'feishu_group')) {
    const key = item.external_chat_id ?? 'unknown'
    const bucket = byChat.get(key)
    if (bucket) bucket.push(item)
    else byChat.set(key, [item])
  }
  /*
   * ⚠️ **群名可能重名**（2026-10-25）：两个群都叫「客服测试群」时，两段长得一模一样，
   *    坐席会点错。**重名的才**加 `#` ＋ `chat_id` 后 4 位 —— 不重名就不加，
   *    别给每一段都挂一串噪声。所以要先把名字数一遍，再拼标题。
   */
  const nameCount = new Map<string, number>()
  for (const [chatId, list] of byChat) {
    const name = groupName(chatId, list)
    nameCount.set(name, (nameCount.get(name) ?? 0) + 1)
  }

  for (const [chatId, list] of byChat) {
    const name = groupName(chatId, list)
    const suffix = (nameCount.get(name) ?? 0) > 1 ? ` #${chatId.slice(-4)}` : ''
    out.push({
      key: `grp:${chatId}`,
      kind: 'feishu_group',
      // ⚠️「N 位顾客」不是「N 人」：这个数字是**问过的人数**，不是群成员数
      //    （多群之后尤其容易混 —— 用户 2026-10-25 就误解过）
      label: `${name}${suffix}（${list.length} 位顾客）`,
      items: list,
    })
  }
  return out
})

/** 群名（拿不到就退成 `chat_id` 后 6 位）。同一个群的所有会话该值是同一个。 */
function groupName(chatId: string, list: ConversationOut[]): string {
  return list[0]?.external_title ?? `飞书群 · ${chatId.slice(-6)}`
}

/** 相对时间。列表按"最近更新"排序，所以相对时间比绝对时间好读。 */
function relative(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diffSec = Math.round((Date.now() - then) / 1000)
  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

/**
 * 会话变多之后的三件事（2026-10-25 走查）：
 *  ① **筛选**（全部 / 内部 / 飞书）—— 群聊按人分桶后，飞书会把列表淹没
 *  ② **群段可收起** —— 10 个群 × 每个群几个人，平铺就是几十行
 *  ③ **删除** —— 以前只能传不能删
 */
const filter = ref<'all' | 'web' | 'feishu'>('all')
const collapsed = ref<Set<string>>(new Set())

/** 筛选项。⚠️ 文案带数字没意义（要动态算），所以只给名字。 */
const FILTERS = [
  { value: 'all' as const, label: '全部' },
  { value: 'web' as const, label: '内部' },
  { value: 'feishu' as const, label: '飞书' },
]

/** 每页步长。⚠️ 没有 `total`（后端返回的是纯数组），所以用"返回条数 == limit"判断还有没有更多。 */
const PAGE = 50
const limit = ref(PAGE)

const filtered = computed(() => {
  if (filter.value === 'web') return items.value.filter((item) => item.channel === 'web')
  if (filter.value === 'feishu') return items.value.filter((item) => item.channel !== 'web')
  return items.value
})

const hasMore = computed(() => items.value.length >= limit.value && limit.value < 200)

function setFilter(next: 'all' | 'web' | 'feishu'): void {
  filter.value = next
}

function toggleGroup(key: string): void {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}

function isCollapsed(key: string): boolean {
  return collapsed.value.has(key)
}

async function loadMore(): Promise<void> {
  limit.value = Math.min(limit.value + PAGE, 200)
  await load()
}

/** 删除会话（连带消息，**不可恢复**）。确认框写清"几条消息会没"。 */
async function remove(item: ConversationOut): Promise<void> {
  const ok = window.confirm(`确定删除「${item.title}」吗？\n\n这个会话里的消息会一起删除，且**无法恢复**。`)
  if (!ok) return
  error.value = ''
  try {
    await deleteConversation(item.id)
    // 本地先摘掉再重拉，避免"点了删除但列表没变"那种最难受的反馈
    items.value = items.value.filter((one) => one.id !== item.id)
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `删除失败：${(err as Error).message}`
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    items.value = await listConversations(limit.value)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

function open(item: ConversationOut): void {
  void router.push({ name: 'chat', params: { id: item.id } })
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <AppHeader title="会话" hint="内部坐席工作台 · Web 与飞书的问答都在这里" tone="blue">
      <button class="btn btn--sm" type="button" @click="router.push({ name: 'chat-new' })">
        ＋ 新建会话
      </button>
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <!--
        加载态是**骨架屏**，不是一句"加载中…"。
        理由：白屏那几百毫秒里，用户分不清"没内容"和"还没来"；
        占位块的形状和真实会话行一致，内容出现时不会跳一下。
      -->
      <div v-if="loading" class="sk-stack" aria-busy="true" aria-label="加载中">
        <div v-for="n in 3" :key="n" class="sk sk--block"></div>
      </div>

      <p v-else-if="items.length === 0" class="empty">
        还没有会话。点右上角「＋ 新建会话」开始提问。
      </p>

      <template v-else>
        <!--
          筛选（2026-10-25）：群聊按人分桶之后，飞书会把内部会话淹掉。
          ⚠️ 筛选是**纯前端**的（数据已经在手上），所以切换是瞬时的、也不多发请求。
        -->
        <div class="filters" role="group" aria-label="按渠道筛选">
          <button
            v-for="option in FILTERS"
            :key="option.value"
            class="filters__btn"
            :class="{ 'filters__btn--on': filter === option.value }"
            type="button"
            :aria-pressed="filter === option.value"
            @click="setFilter(option.value)"
          >
            {{ option.label }}
          </button>
          <span class="filters__count">
            已加载 {{ items.length }} 条{{ hasMore ? '（还有更多）' : '' }}
          </span>
        </div>

        <p v-if="filtered.length === 0" class="empty">这个筛选下没有会话。</p>

        <section v-for="group in groups" :key="group.key" class="group">
          <h3 class="group__head">
            <button
              class="group__toggle"
              type="button"
              :aria-expanded="!isCollapsed(group.key)"
              :title="isCollapsed(group.key) ? '展开' : '收起'"
              @click="toggleGroup(group.key)"
            >
              <span class="group__arrow" aria-hidden="true">{{
                isCollapsed(group.key) ? '▸' : '▾'
              }}</span>
              <span class="chan" :class="`chan--${group.kind}`">{{ CHANNEL_LABEL[group.kind] }}</span>
              <span class="group__name">{{ group.label }}</span>
              <span class="group__count">{{ group.items.length }} 条</span>
            </button>
          </h3>

          <ul v-if="!isCollapsed(group.key)" class="list">
            <li v-for="item in group.items" :key="item.id" class="item">
              <button class="row" type="button" @click="open(item)">
                <span class="row__title">{{ item.title }}</span>
                <span class="row__time">{{ relative(item.updated_at) }}</span>
              </button>
              <!-- 删除：不可恢复，所以按钮低调、且一定要确认 -->
              <button
                class="row__del"
                type="button"
                :title="`删除「${item.title}」`"
                aria-label="删除会话"
                @click="remove(item)"
              >
                ✕
              </button>
            </li>
          </ul>
        </section>

        <button v-if="hasMore" class="more" type="button" :disabled="loading" @click="loadMore">
          加载更多（当前 {{ items.length }} 条）
        </button>
      </template>
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

.empty {
  padding: 40px 0;
  color: var(--text-faint);
  text-align: center;
}

/* ---------- 筛选 ---------- */
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 16px;
}

.filters__btn {
  padding: 4px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.filters__btn--on {
  border-color: var(--primary);
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 500;
}

.filters__count {
  margin-left: auto;
  color: var(--text-faint);
  font-size: 0.72rem;
}

/* ---------- 群段 / 会话行 ---------- */
.group {
  margin-bottom: 22px;
}

.group__head {
  margin: 0 0 8px;
}

.group__toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 0.85rem;
  font-weight: 500;
  text-align: left;
}

.group__arrow {
  color: var(--text-faint);
  font-size: 0.75rem;
}

.group__count {
  flex-shrink: 0;
  color: var(--text-faint);
  font-size: 0.72rem;
}

.item {
  display: flex;
  align-items: stretch;
  gap: 6px;
}

.item > .row {
  flex: 1 1 auto;
}

/* 删除：低调但不藏（hover 才出现的话，触屏用户永远看不到） */
.row__del {
  flex: 0 0 auto;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-faint);
  font-size: 0.8rem;
}

.row__del:hover {
  border-color: var(--danger, #b42318);
  color: var(--danger, #b42318);
}

.more {
  display: block;
  width: 100%;
  margin: 4px 0 0;
  padding: 10px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.more:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}

.group__name {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 三色渠道标签。⚠️ **颜色之外一定带文字** —— 只靠颜色的标签对色弱用户等于没有。 */
.chan {
  flex-shrink: 0;
  padding: 1px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 0.72rem;
  font-family: var(--mono);
  line-height: 1.7;
}

.chan--web {
  border-color: color-mix(in srgb, #2563eb 35%, white);
  background: color-mix(in srgb, #2563eb 10%, white);
  color: #2563eb;
}

.chan--feishu_p2p {
  border-color: color-mix(in srgb, #0e9384 35%, white);
  background: color-mix(in srgb, #0e9384 10%, white);
  color: #0e9384;
}

.chan--feishu_group {
  border-color: color-mix(in srgb, #7a5af8 35%, white);
  background: color-mix(in srgb, #7a5af8 10%, white);
  color: #7a5af8;
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
  width: 100%;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  text-align: left;
  transition:
    border-color 0.15s,
    box-shadow 0.15s;
}

.row:hover {
  border-color: var(--primary);
  box-shadow: var(--shadow-sm);
}

.row__title {
  overflow: hidden;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row__time {
  flex-shrink: 0;
  color: var(--text-faint);
  font-size: 0.8rem;
}
</style>
