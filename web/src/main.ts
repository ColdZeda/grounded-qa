import { createApp } from 'vue'

import { onUnauthorized } from '@/api/client'
import { signOut } from '@/stores/auth'
import { loadRuntimeEnv } from '@/stores/env'
import App from './App.vue'
import router from './router'

import './assets/main.css'

/**
 * 401 的全局出口。
 *
 * ⚠️ 在这里注册（而不是在 `api/client.ts` 里直接 `import` router）是为了
 *    **打断循环依赖**：client 认识 router 的话，
 *    `router → stores/auth → api/auth → api/client → router` 就闭环了。
 *    ESM 遇到循环依赖**不一定报错**，而是让某一方拿到 `undefined` —— 极难排查。
 */
onUnauthorized(() => {
  signOut()
  void router.push({ name: 'login' })
})

/**
 * 探一次"我现在连的是哪套后端"。
 *
 * ⚠️ **不 `await`**：角标是辅助信息，不该拦住首屏。拿不到就不显示（`stores/env.ts` 里
 *    把失败落成 `unknown`）—— 宁可没有，也不猜一个错的（2026-10-22 那个"分不清本地/生产"
 *    的坑，猜错比不显示更坏）。
 */
void loadRuntimeEnv()

createApp(App).use(router).mount('#app')
