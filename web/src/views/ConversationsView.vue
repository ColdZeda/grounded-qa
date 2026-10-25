<script setup lang="ts">
/**
 * 会话列表。
 *
 * ⚠️ "新建会话"**不在这里调后端** —— 它只是跳到 `/conversations/new`。
 *    会话在用户**发出第一条提问**时才真正创建（见 `ChatView` 的 `send`），
 *    这样列表里显示的就是提问本身，而不是一排"新会话"。
 *
 * ## 为什么是「按组翻页」（2026-10-25 改）
 *
 * 以前是**全局** `limit=50` 再在前端分组。那样有一个致命缺陷：
 * **内部会话多的时候，一个组会把配额吃光** —— 飞书组一条都不出现，
 * 分组做了等于白做。
 *
 * 现在：
 *   · `GET /conversations/summary` 一次拿到**每组一共多少条**；
 *   · 每组**各自**拉第一页（10 条），组头写「共 N 条」，组内「加载更多」只加这一组。
 *   · 于是 5000 条会话的首屏也只有 ~30 条请求量。
 *
 * ⚠️ 群聊按 `open_id` 分桶，群里 10 个人问过就有 10 条 ⟹ 群数 × 人数会涨得很快。
 *    所以**折叠**是必需的（不是美化）。
 */

import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import {
  deleteConversation,
  getConversationSummary,
  listConversations,
} from '@/api/conversations'
import type { Channel, ConversationOut, ConversationSummary } from '@/types/api'

const router = useRouter()

/** 每组第一页拉几条；点一次「加载更多」再加这么多。 */
const PAGE = 10

/**
 * 首屏**最多自动加载**几个组。
 * ⚠️ 群多的时候（20 个群 = 20 个请求）不能全发；超出的组先只显示组头，
 *    点开才加载 —— 这条是"按组翻页"的必要配套，否则请求数反而爆了。
 */
const AUTO_LOAD_GROUPS = 8

interface GroupState {
  /** `web` / `p2p` / `grp:{chat_id}` */
  key: string
  channel: Channel
  chatId: string | null
  /** 组头显示的名字（重名时会带 `#abcd`） */
  label: string
  /** 一共多少条（来自 summary） */
  total: number
  items: ConversationOut[]
  loading: boolean
  /** 用户是否展开（折叠的组不发请求） */
  open: boolean
  /** 是否已经拉过第一页（配合 AUTO_LOAD_GROUPS） */
  loaded: boolean
}

const groups = ref<GroupState[]>([])
const loading = ref(true)
const error = ref('')
const filter = ref<'all' | 'web' | 'feishu'>('all')

/** 渠道 → 三色标签。⚠️ **同时带文字**：只靠颜色的话，色弱用户和打印稿都读不出来。 */
const CHANNEL_LABEL: Record<Channel, string> = {
  web: '内部',
  feishu_p2p: '私聊',
  feishu_group: '群聊',
}

const FILTERS = [
  { value: 'all' as const, label: '全部' },
  { value: 'web' as const, label: '内部' },
  { value: 'feishu' as const, label: '飞书' },
]

const visibleGroups = computed(() => {
  if (filter.value === 'web') return groups.value.filter((item) => item.channel === 'web')
  if (filter.value === 'feishu') return groups.value.filter((item) => item.channel !== 'web')
  return groups.value
})

const totalCount = computed(() => groups.value.reduce((sum, item) => sum + item.total, 0))

const loadedCount = computed(() =>
  visibleGroups.value.reduce((sum, item) => sum + item.items.length, 0),
)

function setFilter(next: 'all' | 'web' | 'feishu'): void {
  filter.value = next
}

function groupLabel(key: string, channel: Channel, title: string | null, dup: boolean): string {
  if (channel === 'web') return '内部（Web）'
  if (channel === 'feishu_p2p') return '飞书私聊'
  // 群聊：拿不到群名就退成 id 后 6 位；重名的加 `#` + id 后 4 位
  const chatId = key.replace(/^grp:/, '')
  const name = title ?? `飞书群 · ${chatId.slice(-6)}`
  return dup ? `${name} #${chatId.slice(-4)}` : name
}

/** 从 summary 建出组头（**不发列表请求**）；重名群在这里判断。 */
function buildGroups(summary: ConversationSummary): GroupState[] {
  const nameCount = new Map<string, number>()
  for (const item of summary.groups) {
    if (item.channel !== 'feishu_group') continue
    const name = item.external_title ?? `飞书群 · ${(item.external_chat_id ?? '').slice(-6)}`
    nameCount.set(name, (nameCount.get(name) ?? 0) + 1)
  }

  return summary.groups.map((item) => {
    const chatId = item.external_chat_id
    const name = item.external_title ?? `飞书群 · ${(chatId ?? '').slice(-6)}`
    const dup = item.channel === 'feishu_group' && (nameCount.get(name) ?? 0) > 1
    return {
      key: item.key,
      channel: item.channel,
      chatId: item.channel === 'feishu_group' ? chatId : null,
      label: groupLabel(item.key, item.channel, item.external_title, dup),
      total: item.count,
      items: [],
      loading: false,
      open: true,
      loaded: false,
    }
  })
}

/** 拉某个组的第一页（或"加载更多"后的前 N 条）。 */
async function loadGroup(group: GroupState, limit = PAGE): Promise<void> {
  group.loading = true
  try {
    group.items = await listConversations({
      limit,
      channel: group.channel,
      chatId: group.chatId ?? undefined,
    })
    group.loaded = true
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    group.loading = false
  }
}

async function loadMore(group: GroupState): Promise<void> {
  await loadGroup(group, group.items.length + PAGE)
}

/** 展开/收起。**第一次展开才请求**（见 `AUTO_LOAD_GROUPS`）。 */
function toggleGroup(group: GroupState): void {
  group.open = !group.open
  if (group.open && !group.loaded) void loadGroup(group)
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const summary = await getConversationSummary()
    groups.value = buildGroups(summary)
    // 前若干个组自动加载；其余等用户点开
    const eager = groups.value.slice(0, AUTO_LOAD_GROUPS)
    await Promise.all(eager.map((group) => loadGroup(group)))
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

/** 删除会话（连带消息，**不可恢复**）。确认框写清后果。 */
async function remove(group: GroupState, item: ConversationOut): Promise<void> {
  const ok = window.confirm(
    `确定删除「${item.title}」吗？\n\n这个会话里的消息会一起删除，且**无法恢复**。`,
  )
  if (!ok) return
  error.value = ''
  try {
    await deleteConversation(item.id)
    // 本地先摘掉（"点了删除但列表没变"是最难受的反馈），再对齐真实总数
    group.items = group.items.filter((one) => one.id !== item.id)
    group.total = Math.max(0, group.total - 1)
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `删除失败：${(err as Error).message}`
  }
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

      <p v-else-if="groups.length === 0" class="empty">
        还没有会话。点右上角「＋ 新建会话」开始提问。
      </p>

      <template v-else>
        <!-- 筛选：纯前端（数据按组拿着），切换是瞬时的、不多发请求 -->
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
            共 {{ totalCount }} 条 · 已显示 {{ loadedCount }} 条
          </span>
        </div>

        <p v-if="visibleGroups.length === 0" class="empty">这个筛选下没有会话。</p>

        <section v-for="group in visibleGroups" :key="group.key" class="group">
          <h3 class="group__head">
            <button
              class="group__toggle"
              type="button"
              :aria-expanded="group.open"
              :title="group.open ? '收起' : '展开（展开才加载）'"
              @click="toggleGroup(group)"
            >
              <span class="group__arrow" aria-hidden="true">{{ group.open ? '▾' : '▸' }}</span>
              <span class="chan" :class="`chan--${group.channel}`">
                {{ CHANNEL_LABEL[group.channel] }}
              </span>
              <span class="group__name">{{ group.label }}</span>
              <span class="group__count">
                共 {{ group.total }} 条<template v-if="group.channel === 'feishu_group'">
                  · {{ group.total }} 位顾客</template
                >
              </span>
            </button>
          </h3>

          <template v-if="group.open">
            <p v-if="group.loading && group.items.length === 0" class="group__hint">加载中…</p>

            <ul v-else-if="group.items.length" class="list">
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
                  @click="remove(group, item)"
                >
                  ✕
                </button>
              </li>
            </ul>

            <!-- 组内分页：只加这一组，别的组不动 -->
            <button
              v-if="!group.loading && group.items.length < group.total"
              class="more"
              type="button"
              @click="loadMore(group)"
            >
              加载更多（还有 {{ group.total - group.items.length }} 条）
            </button>
          </template>
          <p v-else class="group__hint">
            已收起 · 共 {{ group.total }} 条{{ group.loaded ? '' : '（展开时才加载）' }}
          </p>
        </section>
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

.group__hint {
  margin: 0 0 2px;
  color: var(--text-faint);
  font-size: 0.76rem;
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

/* 三色渠道标签。⚠️ **颜色之外一定带文字** —— 只靠颜色的标签对色弱用户等于没有。 */
.chan {
  flex-shrink: 0;
  padding: 1px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-family: var(--mono);
  font-size: 0.72rem;
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
  font-size: 0.78rem;
}
</style>
