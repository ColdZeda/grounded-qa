<script setup lang="ts">
/** 顶栏：品牌 + 当前用户 + 退出。中间留一个插槽给页面放自己的东西。 */

import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { currentUser, signOut } from '@/stores/auth'

const router = useRouter()

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
