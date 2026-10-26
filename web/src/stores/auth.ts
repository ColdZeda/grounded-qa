/**
 * 登录状态。
 *
 * ⚠️ **没有用 Pinia** —— 全应用只有"当前用户"这一份全局状态。
 *     引入 Pinia 要多学一套概念（store 定义、`storeToRefs`、插件、devtools 接线），
 *     换来的只是"更规范"。**这个规模下模块级 `ref` 就是最合适的方案**，
 *     而且它天然是单例、天然可 tree-shake。
 *     等真的有第二、第三份跨页面状态时再换，那时才划算。
 */

import { computed, ref } from 'vue'

import * as authApi from '@/api/auth'
import { clearToken, hasValidToken, saveToken } from '@/api/token'
import type { UserOut } from '@/types/api'

const user = ref<UserOut | null>(null)

/**
 * 是否已经问过 `/auth/me`。
 *
 * ⚠️ 必须有这个标志。否则每次路由切换都会再打一次 `/auth/me` ——
 *    用户点几下就是几次多余请求，而且**每次都要等它回来才能渲染页面**。
 */
const userLoaded = ref(false)

export const currentUser = computed(() => user.value)
export const isAdmin = computed(() => user.value?.role === 'admin')

/** 本地判断，**不打网络**。路由守卫用它，所以每次跳转都是零延迟。 */
export function isLoggedIn(): boolean {
  return hasValidToken()
}

/** 登录：换 token → 存起来 → 立刻拉一次用户信息。 */
export async function signIn(email: string, password: string): Promise<void> {
  const token = await authApi.login(email, password)
  saveToken(token.access_token, token.expires_in)
  user.value = await authApi.fetchMe()
  userLoaded.value = true
}

/** 演示登录：走的是**同一条**"换 token → 存起来 → 拉用户信息"路径（走查 P0-③）。 */
export async function signInDemo(): Promise<void> {
  const token = await authApi.demoLogin()
  saveToken(token.access_token, token.expires_in)
  user.value = await authApi.fetchMe()
  userLoaded.value = true
}

/** 退出：清本地状态。**不需要调后端** —— JWT 是无状态的，服务端没有"会话"可注销。 */
export function signOut(): void {
  clearToken()
  user.value = null
  userLoaded.value = true
}

/**
 * 确保拿到了当前用户（路由守卫在跳转前调）。
 *
 * ⚠️ 失败时**不抛错**：401 已经由 `client.ts` 的钩子处理掉了（清 token + 跳登录页）。
 *    这里再抛一次只会得到一个没人 catch 的 Promise 拒绝，在控制台刷红字。
 */
export async function ensureUser(): Promise<void> {
  if (userLoaded.value || !hasValidToken()) {
    userLoaded.value = true
    return
  }
  try {
    user.value = await authApi.fetchMe()
  } catch {
    user.value = null
  } finally {
    userLoaded.value = true
  }
}

/**
 * 用户改了提问模式 → 记到后端并同步本地。
 *
 * ⚠️ 失败**不报错也不回滚界面**：模式本身已经随请求发出去了，
 *    记不住只影响下次的默认值 —— 为这个弹一个错误提示只会让人困惑。
 */
export async function setChatMode(mode: 'fast' | 'agent'): Promise<void> {
  if (user.value) {
    user.value = { ...user.value, preferences: { ...(user.value.preferences ?? {}), chat_mode: mode } }
  }
  try {
    user.value = await authApi.updatePreferences({ chat_mode: mode })
  } catch {
    // 静默失败：下次进来可能还是老值，但本次使用不受影响。
  }
}
