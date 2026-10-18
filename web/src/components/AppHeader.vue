<script setup lang="ts">
/** 顶栏：品牌 + 当前用户 + 退出。中间留一个插槽给页面放自己的东西。 */

import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { currentUser, signOut } from '@/stores/auth'
import { detailLevel, setDetailLevel } from '@/stores/prefs'

const router = useRouter()
const route = useRoute()

/**
 * 「会话」这一格在**聊天页也要亮**。
 *
 * ⚠️ 只靠 `router-link-active` 做不到：`/conversations/:id` 在路由表里是一条
 * **独立的记录**（`chat`），不是 `conversations` 的子路由 —— vue-router 的
 * active class 按**记录嵌套**算，不按 URL 前缀算。所以这里自己判。
 *
 * ⚠️ 写成**普通函数**而不是 `computed`：模板里要用 `inChat()` 这种**调用形式**
 * （AGENTS §10 那个坑：裸标识符当整个条件时会被编译器常量折叠）。
 * `route` 本身是响应式的，函数式写法照样跟着路由变。
 */
function inChat(): boolean {
  return ['conversations', 'chat', 'chat-new'].includes(String(route.name ?? ''))
}

/**
 * 详细程度档位（设计理由见 `architecture.md` §9.19）。
 *
 * ⚠️ 别把"简洁"理解成"给客户看所以藏来源" —— **藏的是分数，不是来源**。
 *    「这条来自《退款政策》」是建立信任的东西；「精排分 0.466」才是实现细节。
 */
const LEVELS = [
  { value: 'detailed', label: '详细', hint: '显示分数与口径 —— 给开发、评测、面试演示' },
  { value: 'concise', label: '简洁', hint: '隐藏分数，只留来源 —— 给终端用户' },
] as const

const email = computed(() => currentUser.value?.email ?? '')
const roleLabel = computed(() => (currentUser.value?.role === 'admin' ? '管理员' : '成员'))

/**
 * 运维看板**只给管理员**（它暴露流量、成本、错误分布，属于运维面）。
 *
 * ⚠️ 写成函数调用 `isAdmin()` 而不是裸标识符 —— AGENTS §10 那个坑：
 *    裸标识符当整个条件时会被编译器常量折叠，`v-if` 整段从产物里消失。
 */
function isAdmin(): boolean {
  return currentUser.value?.role === 'admin'
}

function logout(): void {
  // ⚠️ 不调后端 —— JWT 是无状态的，服务端没有会话可注销。
  //    真要做"立即失效"得维护一张吊销表，那是另一个话题（不在 v1.0 范围）。
  signOut()
  void router.push({ name: 'login' })
}
</script>

<template>
  <header class="bar">
    <RouterLink class="brand" :to="{ name: 'conversations' }">
      grounded<span>·</span>qa
    </RouterLink>

    <nav class="nav">
      <RouterLink :to="{ name: 'conversations' }" :class="{ 'is-active': inChat() }">
        会话
      </RouterLink>
      <RouterLink :to="{ name: 'documents' }">知识库</RouterLink>
      <RouterLink :to="{ name: 'reports' }">评测报告</RouterLink>
      <RouterLink v-if="isAdmin()" :to="{ name: 'monitoring' }">运维</RouterLink>
    </nav>

    <div class="mid">
      <slot />
    </div>

    <div class="detail" role="group" aria-label="详细程度">
      <button
        v-for="level in LEVELS"
        :key="level.value"
        type="button"
        class="detail__btn"
        :class="{ 'detail__btn--on': detailLevel === level.value }"
        :title="level.hint"
        :aria-pressed="detailLevel === level.value"
        @click="setDetailLevel(level.value)"
      >
        {{ level.label }}
      </button>
    </div>

    <div class="who">
      <span class="email" :title="email">{{ email }}</span>
      <span class="role">{{ roleLabel }}</span>
      <button class="btn btn--ghost btn--sm" type="button" @click="logout">退出</button>
    </div>
  </header>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 56px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.brand {
  color: var(--text);
  font-size: 1rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  text-decoration: none;
}

.brand span {
  color: var(--primary);
}

/* 主导航。`router-link-active` 由 vue-router 自动加 —— 不用自己算当前页 */
.nav {
  display: flex;
  flex-shrink: 0;
  gap: 2px;
}

.nav a {
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 0.85rem;
  text-decoration: none;
}

.nav a:hover {
  color: var(--text);
}

.nav a.router-link-active,
.nav a.is-active {
  background: var(--primary-soft);
  color: var(--primary);
}

.mid {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

/* 详细程度：两格分段开关。刻意做得不显眼 —— 它是设置，不是主操作 */
.detail {
  display: flex;
  flex-shrink: 0;
  overflow: hidden;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
}

.detail__btn {
  padding: 4px 10px;
  border: none;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.detail__btn--on {
  background: var(--primary);
  color: #fff;
}

.who {
  display: flex;
  align-items: center;
  gap: 10px;
}

.email {
  max-width: 220px;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.875rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.role {
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 0.75rem;
}
</style>
