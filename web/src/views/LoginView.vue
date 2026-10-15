<script setup lang="ts">
/**
 * 登录页。
 *
 * ⚠️ 后端有两个登录入口形态：`OAuth2PasswordRequestForm`（form-urlencoded）
 *    和 `LoginRequest`（JSON schema，**但没有对应的路由**）。
 *    这里走 **form** 那条 —— 那是唯一真实存在的端点，
 *    也顺带保住了 Swagger UI 的 Authorize 按钮可用。
 */

import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { signIn } from '@/stores/auth'

const router = useRouter()
const route = useRoute()

// `app/db/seed.py` 建的管理员账号（`admin_email` 默认就是它）。
// ⚠️ **只预填邮箱，不预填密码** —— 密码在 `.env` 的 `ADMIN_PASSWORD` 里，
//    默认值是 `change-me`，但那属于"配置"，写死在前端源码里就成了硬编码凭据。
const email = ref('admin@example.com')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  busy.value = true
  try {
    await signIn(email.value.trim(), password.value)
    // ⚠️ 登录成功后回**原来想去的页面**（守卫写进 query 的）。
    //    深链接（别人发来的 /conversations/xxx）才不会在登录后被丢掉。
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null
    await router.replace(redirect ?? { name: 'conversations' })
  } catch (err) {
    // 后端对"用户不存在 / 密码错 / 已停用"返回**同一句话**，所以这里直接显示它就行，
    // 不要自作聪明地分类提示 —— 那会把后端的防枚举设计又漏回去。
    error.value = err instanceof ApiError ? err.message : `登录失败：${(err as Error).message}`
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="page">
    <form class="card" @submit.prevent="submit">
      <h1>grounded<span>·</span>qa</h1>
      <p class="tagline">多租户知识库问答平台 · 每条回答都带引用来源</p>

      <label class="label" for="email">邮箱</label>
      <input
        id="email"
        v-model="email"
        class="field"
        type="email"
        autocomplete="username"
        required
        :disabled="busy"
      />

      <label class="label" for="password">密码</label>
      <input
        id="password"
        v-model="password"
        class="field"
        type="password"
        autocomplete="current-password"
        required
        :disabled="busy"
      />

      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <button class="btn submit" type="submit" :disabled="busy || !email || !password">
        {{ busy ? '登录中…' : '登录' }}
      </button>

      <p class="hint">
        开发账号由 <code>python -m app.db.seed</code> 创建；密码见 <code>.env</code> 的
        <code>ADMIN_PASSWORD</code>。
      </p>
    </form>
  </main>
</template>

<style scoped>
.page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 24px;
}

.card {
  width: 100%;
  max-width: 380px;
  padding: 28px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  box-shadow: var(--shadow);
}

h1 {
  margin: 0;
  font-size: 1.5rem;
}

h1 span {
  color: var(--primary);
}

.tagline {
  margin: 6px 0 24px;
  color: var(--text-muted);
  font-size: 0.875rem;
}

.label {
  display: block;
  margin-bottom: 5px;
  color: var(--text-muted);
  font-size: 0.8rem;
}

.field {
  margin-bottom: 16px;
}

.submit {
  width: 100%;
  justify-content: center;
  margin-top: 4px;
}

.hint {
  margin: 18px 0 0;
  color: var(--text-faint);
  font-size: 0.75rem;
  line-height: 1.6;
}

code {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--surface-sunken);
  font-family: var(--mono);
  font-size: 0.9em;
}
</style>
