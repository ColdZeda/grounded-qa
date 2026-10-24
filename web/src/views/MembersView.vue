<script setup lang="ts">
/**
 * 成员管理（走查阶段 P0-①）。
 *
 * ⚠️ **这是本项目唯一的"加人"入口** —— 没有开放注册（内部 B 端）。
 *    为什么不在登录页放注册：见 `..\plan\PROJECT-PLAN.md` §12.1 决策 D5。
 *
 * ⚠️ 建完之后**新成员立刻能登录**（密码当场生效）；管理员**看不到**别的租户的人
 *    （后端 `TenantScopedRepository` 注入了 `WHERE tenant_id = ?`）。
 */

import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { createUser, listUsers, type UserSummary } from '@/api/admin'
import type { UserRole } from '@/types/api'

const items = ref<UserSummary[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')

const creating = ref(false)
const form = ref<{ email: string; password: string; role: UserRole }>({
  email: '',
  password: '',
  role: 'member',
})

/** 密码 <8 位前端先拦一次（后端 pydantic 也会 422，但别让用户白等一轮）。 */
const canSubmit = computed(
  () => form.value.email.includes('@') && form.value.password.length >= 8 && !creating.value,
)

/**
 * 按钮为什么是灰的 —— **必须说出来**。
 *
 * ⚠️ 走查 2026-10-25 用户实测：邮箱只打了 `testmember`（没有 `@`），「创建」是灰的、
 *    点不动，而**界面上一个字都没解释**。用户只会以为"这功能坏了"。
 *    一个点不动的按钮不是"防呆"，是"卡住了还不知道为什么"。
 * ⚠️ 空表单（刚进页面）不提示 —— 那时还没开始填，弹红字是噪音。
 */
const blockedReason = computed(() => {
  const email = form.value.email.trim()
  const password = form.value.password
  if (!email && !password) return ''
  if (email && !email.includes('@')) return '邮箱要写成 name@example.com 这样（少了 @ 后面的域名）'
  if (password && password.length < 8) return `密码至少 8 位（现在 ${password.length} 位）`
  if (!email) return '还没填邮箱'
  if (!password) return '还没填密码'
  return ''
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    items.value = await listUsers()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  creating.value = true
  error.value = ''
  notice.value = ''
  try {
    const created = await createUser({ ...form.value, email: form.value.email.trim() })
    notice.value = `已创建 ${created.email}（${created.role === 'admin' ? '管理员' : '成员'}）—— 现在就可以登录。`
    form.value = { email: '', password: '', role: 'member' }
    await load()
  } catch (err) {
    // ⚠️ 409（邮箱已占用）与 400（密码超 72 字节 / 邮箱格式）都要**原样告诉用户**，
    //    不能统一成"创建失败" —— 否则用户不知道该改哪一项。
    error.value = err instanceof ApiError ? err.message : `创建失败：${(err as Error).message}`
  } finally {
    creating.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <AppHeader>
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body" data-accent="chat">
      <h2 class="title">成员 <span class="count">本租户 {{ items.length }} 人</span></h2>

      <p class="hint">
        企业内部的账号由管理员在这里发放，<strong>没有开放注册</strong> —— 陌生人不该能自己进来问你家政策。
        新建的成员立刻可以登录，默认是「成员」（只能问答，看不到知识库 / 评测 / 运维）。
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <section class="card">
        <h3 class="card__title">新建成员</h3>
        <form class="form" @submit.prevent="submit">
          <label class="form__field">
            <span>邮箱</span>
            <input
              v-model="form.email"
              class="field"
              type="email"
              placeholder="someone@company.com"
              autocomplete="off"
              :disabled="creating"
            />
          </label>
          <label class="form__field">
            <span>初始密码</span>
            <input
              v-model="form.password"
              class="field"
              type="text"
              placeholder="≥8 位（建议 12 位以上）"
              autocomplete="new-password"
              :disabled="creating"
            />
          </label>
          <label class="form__field form__field--role">
            <span>角色</span>
            <select v-model="form.role" class="field" :disabled="creating">
              <option value="member">成员（只能问答）</option>
              <option value="admin">管理员（可管知识库 / 评测 / 运维）</option>
            </select>
          </label>
          <button class="btn" type="submit" :disabled="!canSubmit">
            {{ creating ? '创建中…' : '＋ 创建' }}
          </button>
        </form>
        <!-- 点不动就要说清为什么（见 `blockedReason` 的注释） -->
        <p v-if="blockedReason" class="form__blocked">{{ blockedReason }}</p>
        <p class="card__note">
          密码只存 bcrypt 哈希，<strong>建完就看不到明文</strong>（bcrypt 上限 72 字节，中文一个字符算 3 字节）。
        </p>
      </section>

      <div v-if="loading" class="skeleton-stack" aria-hidden="true">
        <div v-for="n in 3" :key="n" class="skeleton-card">
          <span class="skeleton skeleton--w70"></span>
          <span class="skeleton skeleton--w40"></span>
        </div>
      </div>

      <p v-else-if="items.length === 0" class="empty">还没有成员。</p>

      <table v-else class="table">
        <thead>
          <tr>
            <th>邮箱</th>
            <th>角色</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in items" :key="user.id">
            <td class="table__email">{{ user.email }}</td>
            <td>{{ user.role === 'admin' ? '管理员' : '成员' }}</td>
            <td>{{ user.is_active ? '正常' : '已停用' }}</td>
          </tr>
        </tbody>
      </table>
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
  margin: 0 0 16px;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.7;
}

.card {
  margin: 0 0 20px;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.card__title {
  margin: 0 0 12px;
  font-size: 0.95rem;
}

.card__note {
  margin: 10px 0 0;
  color: var(--text-faint);
  font-size: 0.75rem;
}

/* 「为什么点不动」—— 比普通说明更醒目一点，但不做成红色警告（还没到出错） */
.form__blocked {
  margin: 8px 0 0;
  color: var(--warn);
  font-size: 0.8rem;
}

.form {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}

.form__field {
  display: grid;
  flex: 1 1 180px;
  gap: 4px;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.form__field--role {
  flex: 0 1 220px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;
}

.table th,
.table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}

.table th {
  color: var(--text-muted);
  font-weight: 500;
  font-size: 0.8rem;
}

.table__email {
  font-family: var(--mono);
  font-size: 0.82rem;
}
</style>
