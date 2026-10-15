/**
 * 路由。
 *
 * 三个页面：登录 → 会话列表 → 聊天。**刻意不做多标签页布局** ——
 * 面试演示时"点进去、看到答案、看到引用来源"这条动线越短越好。
 *
 * 全部用**动态 import**（懒加载）：登录页不该为了渲染一个表单
 * 先把聊天页的代码也下载下来。
 */

import { createRouter, createWebHistory } from 'vue-router'

import { ensureUser, isLoggedIn } from '@/stores/auth'

const router = createRouter({
  // ⚠️ `createWebHistory` = HTML5 history 模式（URL 干净，没有 `#`）。
  //    代价是**服务器必须把所有未知路径都回落到 index.html**，
  //    否则用户直接访问 /conversations/xxx 会拿到 nginx 的 404。
  //    → 这件事由 deploy/nginx 的 `try_files ... /index.html` 负责，
  //      改前端路由时**不要**忘了它（否则只有"刷新才 404"，很难联想）。
  history: createWebHistory(),

  routes: [
    { path: '/', redirect: { name: 'conversations' } },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/conversations',
      name: 'conversations',
      component: () => import('@/views/ConversationsView.vue'),
    },
    {
      // ⚠️ 这条**必须放在 `:id` 前面**。vue-router 会按"静态段优先"排序，
      //    所以顺序其实不影响匹配结果 —— 但写在前面对读代码的人更清楚：
      //    `new` 不是某个会话的 id。
      //
      // 它对应"还没建会话"的状态：会话在**第一次提问时**才创建
      // （为了拿提问当标题，见 `ChatView.vue` 的说明），
      // 所以这时**没有 id 可以放进路径**。
      path: '/conversations/new',
      name: 'chat-new',
      component: () => import('@/views/ChatView.vue'),
    },
    {
      path: '/conversations/:id',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
      // `props: true` → 路由参数作为 props 传给组件，组件里就不用碰 `useRoute()`
      props: true,
    },
    // 兜底：乱七八糟的 URL 回列表页，而不是白屏
    { path: '/:pathMatch(.*)*', redirect: { name: 'conversations' } },
  ],
})

router.beforeEach(async (to) => {
  const authed = isLoggedIn()

  if (!to.meta.public && !authed) {
    // ⚠️ 带上 `redirect`，登录后能回到原来想去的页面。
    //    少了它，用户点开别人发来的深链接会被丢到会话列表，得自己再找一遍。
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.meta.public && authed) {
    // 已登录还去登录页 → 直接送进去
    return { name: 'conversations' }
  }

  if (authed) await ensureUser()
  return true
})

export default router
