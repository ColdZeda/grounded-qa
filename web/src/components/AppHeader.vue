<script setup lang="ts">
/** 顶栏：品牌 + 当前用户 + 退出。中间留一个插槽给页面放自己的东西。 */

import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { currentUser, signOut } from '@/stores/auth'
import { detailLevel, setDetailLevel } from '@/stores/prefs'

const router = useRouter()

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
