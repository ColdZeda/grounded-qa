<script setup lang="ts">
/**
 * 个人信息 ＋ 改密码（走查阶段 P0-②）。
 *
 * ⚠️ 这里**只读展示**身份信息（邮箱 / 角色 / 租户）—— 邮箱是全局唯一的账号标识，
 *    改邮箱等于换账号，属于管理员动作，不在这一页做。
 * ⚠️ 改密码**必须验旧密码**：token 泄露时，攻击者不该能把主人锁在门外。
 * ⚠️ 改完**不会踢掉当前会话**（JWT 无状态，要吊销得维护名单）—— 界面上要说清楚，
 *    否则用户会以为"改了密码为什么还登着"是 bug。
 */

import { computed, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { changePassword } from '@/api/auth'
import { currentUser } from '@/stores/auth'

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')

const submitting = ref(false)
const error = ref('')
const notice = ref('')

const roleLabel = computed(() => (currentUser.value?.role === 'admin' ? '管理员' : '成员'))

/** 三条前端校验，任一条不满足就不让提交（后端还会再验一次）。 */
const canSubmit = computed(
  () =>
    oldPassword.value.length > 0 &&
    newPassword.value.length >= 8 &&
    newPassword.value === confirmPassword.value &&
    !submitting.value,
)

const mismatch = computed(
  () => confirmPassword.value.length > 0 && newPassword.value !== confirmPassword.value,
)

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  notice.value = ''
  try {
    await changePassword(oldPassword.value, newPassword.value)
    notice.value = '密码已更新。当前会话仍然有效；下次登录请用新密码。'
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `修改失败：${(err as Error).message}`
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="shell">
    <AppHeader title="个人信息" hint="账号与密码" tone="slate" />

    <main class="body">
      <section class="card">
        <h3 class="card__title">账号</h3>
        <dl class="kv">
          <!-- ⚠️ 标签是「账号」不是「邮箱」：2026-10-25 起建成员可以只填用户名 -->
          <dt>账号</dt>
          <dd class="kv__mono">{{ currentUser?.email }}</dd>
          <dt>角色</dt>
          <dd>{{ roleLabel }}<span class="kv__hint">（管理员能管知识库 / 评测 / 运维；成员只能问答）</span></dd>
          <dt>租户</dt>
          <dd class="kv__mono">{{ currentUser?.tenant_id?.slice(0, 8) }}</dd>
        </dl>
        <p class="card__note">
          账号（邮箱或用户名都行）是全局唯一的登录标识，改它等于换账号 ——
          需要管理员在「成员」里重新建人。
        </p>
      </section>

      <section class="card">
        <h3 class="card__title">修改密码</h3>

        <p v-if="error" class="alert alert--danger">{{ error }}</p>
        <p v-if="notice" class="alert alert--info">{{ notice }}</p>

        <form class="form" @submit.prevent="submit">
          <label class="form__field">
            <span>当前密码</span>
            <input
              v-model="oldPassword"
              class="field"
              type="password"
              autocomplete="current-password"
              :disabled="submitting"
            />
          </label>
          <label class="form__field">
            <span>新密码（≥8 位）</span>
            <input
              v-model="newPassword"
              class="field"
              type="password"
              autocomplete="new-password"
              :disabled="submitting"
            />
          </label>
          <label class="form__field">
            <span>再输一次</span>
            <input
              v-model="confirmPassword"
              class="field"
              type="password"
              autocomplete="new-password"
              :disabled="submitting"
            />
          </label>
          <button class="btn" type="submit" :disabled="!canSubmit">
            {{ submitting ? '提交中…' : '保存新密码' }}
          </button>
        </form>

        <p v-if="mismatch" class="alert alert--warn form__warn">两次输入的新密码不一致。</p>
        <p class="card__note">
          密码只存 bcrypt 哈希；上限是 <strong>72 字节</strong>（中文一个字符算 3 字节），
          超了会被明确拒绝而不是静默截断。改完<strong>不会踢掉当前会话</strong>。
        </p>
      </section>
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
  max-width: 720px;
  margin: 0 auto;
  padding: 28px 20px 60px;
}

/* 边框/圆角/底色统一走全局 `.card`（`main.css`），这里只说"下面要留空" */
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
  line-height: 1.7;
}

.kv {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 8px 12px;
  margin: 0;
  font-size: 0.9rem;
}

.kv dt {
  color: var(--text-muted);
}

.kv dd {
  margin: 0;
}

.kv__mono {
  font-family: var(--mono);
  font-size: 0.85rem;
}

.kv__hint {
  color: var(--text-faint);
  font-size: 0.75rem;
}

.form {
  display: grid;
  gap: 12px;
}

.form__field {
  display: grid;
  gap: 4px;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.form__warn {
  margin-top: 10px;
}
</style>
