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
import { currentUser } from '@/stores/auth'

/**
 * 是不是一键演示账号（**只读管理员**）。
 * ⚠️ 用**函数调用**形式（`isDemoAccount()`），别写成裸标识符 ——
 *    `AGENTS §10`：裸标识符当整个 `v-if` 条件时会被编译器常量折叠，整段消失。
 */
function isDemoAccount(): boolean {
  return currentUser.value?.is_demo === true
}

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
  () => form.value.email.trim().length > 0 && form.value.password.length >= 8 && !creating.value,
)

/**
 * 按钮为什么是灰的 —— **必须说出来**。
 *
 * ⚠️ 走查 2026-10-25 用户实测：邮箱只打了 `testmember`（没有 `@`），「创建」是灰的、
 *    点不动，而**界面上一个字都没解释**。用户只会以为"这功能坏了"。
 *    一个点不动的按钮不是"防呆"，是"卡住了还不知道为什么"。
 * ⚠️ 空表单（刚进页面）不提示 —— 那时还没开始填，弹红字是噪音。
 * ⚠️ **2026-10-25 起不再要求邮箱**（用户定：内部工具不需要真邮箱，`zhangsan` 就该能建）。
 *    唯一性仍然要，登录框填的就是它。
 */
const blockedReason = computed(() => {
  const email = form.value.email.trim()
  const password = form.value.password
  if (!email && !password) return ''
  if (email && /\s/.test(email)) return '账号里不能有空格'
  if (password && password.length < 8) return `密码至少 8 位（现在 ${password.length} 位）`
  if (!email) return '还没填账号'
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
    <AppHeader title="成员" :hint="`本租户 ${items.length} 人`" tone="violet">
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p class="hint">
        企业内部的账号由管理员在这里发放，<strong>没有开放注册</strong> —— 陌生人不该能自己进来问你家政策。
        新建的成员立刻可以登录，默认是「成员」（只能问答，看不到知识库 / 评测 / 运维）。
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="notice" class="alert alert--info">{{ notice }}</p>

      <section class="card">
        <h3 class="card__title">新建成员</h3>
        <!--
          ⚠️ 演示账号是**只读管理员**：这一页看得见（才有演示价值），但不能建成员 ——
             能建成员就等于能给自己开一个永久 admin，把"关掉演示开关"架空（后端 `AdminWriter` 也拦）。
        -->
        <p v-if="isDemoAccount()" class="alert alert--info">
          演示账号是<strong>只读</strong>的：本页可以查看，但不能新建成员。
        </p>
        <form v-else class="form" @submit.prevent="submit">
          <label class="form__field">
            <span>账号</span>
            <input
              v-model="form.email"
              class="field"
              type="text"
              placeholder="zhangsan 或 zhangsan@company.com"
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

.hint {
  margin: 0 0 16px;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.7;
}

/* 边框/圆角/底色/阴影统一走全局 `.card`（`main.css`），这里只说"下面要留空" */
.card {
  margin: 0 0 18px;
}

.card__title {
  margin: 0 0 12px;
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

/* 表格密度统一走全局 `.table`（`main.css`）；这里只留「账号列用等宽」这一处差异 */
.table__email {
  font-family: var(--mono);
  font-size: 0.82rem;
}
</style>
