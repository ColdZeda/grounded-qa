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
import { listConversations } from '@/api/conversations'
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
  const web = items.value.filter((item) => item.channel === 'web')
  const p2p = items.value.filter((item) => item.channel === 'feishu_p2p')
  if (web.length) out.push({ key: 'web', kind: 'web', label: '内部（Web）', items: web })
  if (p2p.length) out.push({ key: 'p2p', kind: 'feishu_p2p', label: '飞书私聊', items: p2p })

  const byChat = new Map<string, ConversationOut[]>()
  for (const item of items.value.filter((one) => one.channel === 'feishu_group')) {
    const key = item.external_chat_id ?? 'unknown'
    const bucket = byChat.get(key)
    if (bucket) bucket.push(item)
    else byChat.set(key, [item])
  }
  for (const [chatId, list] of byChat) {
    // 同一群里所有会话的 `external_title` 是同一个群名（解析失败时是 id 后 6 位）
    const name = list[0].external_title ?? `飞书群 · ${chatId.slice(-6)}`
    out.push({
      key: `grp:${chatId}`,
      kind: 'feishu_group',
      label: `${name}（${list.length} 人）`,
      items: list,
    })
  }
  return out
})

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

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    items.value = await listConversations()
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
    <AppHeader>
      <button class="btn btn--sm" type="button" @click="router.push({ name: 'chat-new' })">
        ＋ 新建会话
      </button>
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <h2 class="title">会话</h2>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <p v-if="loading" class="muted">加载中…</p>

      <p v-else-if="items.length === 0" class="empty">
        还没有会话。点右上角「＋ 新建会话」开始提问。
      </p>

      <template v-else>
        <section v-for="group in groups" :key="group.key" class="group">
          <h3 class="group__head">
            <span class="chan" :class="`chan--${group.kind}`">{{ CHANNEL_LABEL[group.kind] }}</span>
            <span class="group__name">{{ group.label }}</span>
          </h3>

          <ul class="list">
            <li v-for="item in group.items" :key="item.id">
              <button class="row" type="button" @click="open(item)">
                <span class="row__title">{{ item.title }}</span>
                <span class="row__time">{{ relative(item.updated_at) }}</span>
              </button>
            </li>
          </ul>
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

.title {
  margin: 0 0 16px;
  font-size: 1.1rem;
}

.muted {
  color: var(--text-muted);
}

.empty {
  padding: 40px 0;
  color: var(--text-faint);
  text-align: center;
}

.group {
  margin-bottom: 22px;
}

.group__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-size: 0.85rem;
  font-weight: 500;
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
