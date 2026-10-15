import { createApp } from 'vue'

import { onUnauthorized } from '@/api/client'
import { signOut } from '@/stores/auth'
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

createApp(App).use(router).mount('#app')
