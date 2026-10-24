<script setup lang="ts">
/**
 * 左侧栏：品牌 ＋ 导航 ＋ 用户区（底部）。
 *
 * 为什么把导航从顶栏挪到侧栏（走查 2026-10-24 的截图）：
 *   顶栏的导航是 `flex-wrap` 的，窗口一窄就折成两行、顶栏忽高忽低，
 *   再窄一点环境角标和退出按钮就挤到看不见的地方。**换行本身就是丑的来源** ——
 *   侧栏只有一列，宽度由断点决定，永远不会换行。
 *
 * 三档（与 `main.css` 的 `--sidebar-w` **必须一致**）：
 *   ≥1440      240px 展开：图标 ＋ 文字
 *   1100–1439   68px 折叠：只留图标，`title` 给全名
 *   <1100       68px ＋ 抽屉：点左上角 ☰，浮出 240px 的面板（带遮罩，Esc 关）
 *
 * ⚠️ 抽屉打开时面板用 `position: fixed`：`.app` 上有 `overflow-x: auto`，
 *    普通绝对定位会被那个滚动容器**裁掉**（浮出来的那一半直接看不见）。
 */

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { currentUser, signOut } from '@/stores/auth'

const router = useRouter()
const route = useRoute()

/**
 * 导航表。**顺序按日常使用频率**，不是字母序 —— 会话是每天开着的那个。
 *
 * ⚠️ 图标是**内联 SVG 路径**，不是图标库：这个前端一直不引组件库
 *    （`main.css` 顶部写了理由），6 个图标引一整个库不划算。
 */
const NAV = [
  {
    name: 'conversations',
    label: '会话',
    paths: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
  },
  {
    name: 'documents',
    label: '知识库',
    paths: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z'],
  },
  {
    name: 'members',
    label: '成员',
    adminOnly: true,
    paths: [
      'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2',
      'M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
      'M22 21v-2a4 4 0 0 0-3-3.87',
      'M16 3.13a4 4 0 0 1 0 7.75',
    ],
  },
  {
    name: 'reports',
    label: '评测报告',
    paths: ['M18 20V10', 'M12 20V4', 'M6 20v-6'],
  },
  {
    name: 'usage',
    label: '配额',
    paths: ['M21.21 15.89A10 10 0 1 1 8 2.83', 'M22 12A10 10 0 0 0 12 2v10z'],
  },
  {
    name: 'monitoring',
    label: '运维',
    adminOnly: true,
    paths: ['M22 12h-4l-3 9L9 3l-3 9H2'],
  },
]

/** 折叠 / 抽屉两个状态由 `matchMedia` 判定 —— 与 `main.css` 的断点是同一组数字。 */
const collapsed = ref(false)
const narrow = ref(false)
const drawerOpen = ref(false)

let mqCollapsed: MediaQueryList | null = null
let mqNarrow: MediaQueryList | null = null

function sync(): void {
  if (mqCollapsed) collapsed.value = mqCollapsed.matches
  if (mqNarrow) narrow.value = mqNarrow.matches
  // 回到宽屏就没有抽屉可言了：不留一个"打开着但看不见"的状态
  if (!narrow.value) drawerOpen.value = false
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') drawerOpen.value = false
}

onMounted(() => {
  mqCollapsed = window.matchMedia('(max-width: 1439.98px)')
  mqNarrow = window.matchMedia('(max-width: 1099.98px)')
  sync()
  mqCollapsed.addEventListener('change', sync)
  mqNarrow.addEventListener('change', sync)
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  mqCollapsed?.removeEventListener('change', sync)
  mqNarrow?.removeEventListener('change', sync)
  window.removeEventListener('keydown', onKey)
})

/**
 * 「会话」这一格在**聊天页也要亮**。
 *
 * ⚠️ 只靠 `router-link-active` 做不到：`/conversations/:id` 在路由表里是一条
 * **独立的记录**（`chat`），不是 `conversations` 的子路由 —— vue-router 的
 * active class 按**记录嵌套**算，不按 URL 前缀算。
 *
 * ⚠️ 写成**普通函数**而不是 `computed`：模板里要用 `isActive(x)` 这种调用形式
 * （`AGENTS §10`：裸标识符当整个条件时会被编译器常量折叠）。`route` 本身是响应式的。
 */
function isActive(name: string): boolean {
  const current = String(route.name ?? '')
  if (name === 'conversations') return ['conversations', 'chat', 'chat-new'].includes(current)
  return current === name
}

/** 窄屏（<1100）：只有这时才出现抽屉开关。⚠️ 函数调用形式，理由同上。 */
function isNarrow(): boolean {
  return narrow.value
}

/** ⚠️ 函数调用形式，理由同上（`isAdmin()` 而不是裸 `isAdmin`）。 */
function isAdmin(): boolean {
  return currentUser.value?.role === 'admin'
}

const visibleNav = computed(() => NAV.filter((item) => !item.adminOnly || isAdmin()))

const account = computed(() => currentUser.value?.email ?? '')
const roleLabel = computed(() => (currentUser.value?.role === 'admin' ? '管理员' : '成员'))
const initial = computed(() => (account.value.trim()[0] ?? '?').toUpperCase())

function logout(): void {
  // ⚠️ 不调后端 —— JWT 是无状态的，服务端没有会话可注销。
  signOut()
  void router.push({ name: 'login' })
}
</script>

<template>
  <aside
    class="side"
    :class="{ 'side--rail': collapsed, 'side--narrow': narrow, 'side--open': drawerOpen }"
  >
    <!-- 抽屉的遮罩：点任意处关掉。只够在窄屏出现，宽屏 `v-if` 直接不渲染 -->
    <div v-if="drawerOpen" class="scrim" @click="drawerOpen = false"></div>

    <div class="panel">
      <div class="top">
        <RouterLink class="brand" :to="{ name: 'conversations' }" title="grounded·qa">
          <span class="brand__mark">g</span>
          <span class="brand__text">grounded<span>·</span>qa</span>
        </RouterLink>

        <!--
          抽屉开关：只在 <1100 出现。
          ⚠️ `aria-expanded` 必须跟着状态走 —— 这个按钮没有文字，读屏用户只能靠它判断展开没展开。
        -->
        <button
          v-if="isNarrow()"
          class="burger"
          type="button"
          :aria-expanded="drawerOpen"
          aria-label="展开导航"
          title="展开导航"
          @click="drawerOpen = !drawerOpen"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 7h16" />
            <path d="M4 12h16" />
            <path d="M4 17h16" />
          </svg>
        </button>
      </div>

      <nav class="nav" aria-label="主导航">
        <RouterLink
          v-for="item in visibleNav"
          :key="item.name"
          class="nav__item"
          :class="{ 'nav__item--on': isActive(item.name) }"
          :to="{ name: item.name }"
          :title="item.label"
        >
          <svg class="nav__icon" viewBox="0 0 24 24" aria-hidden="true">
            <path v-for="d in item.paths" :key="d" :d="d" />
          </svg>
          <span class="nav__label">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="foot">
        <RouterLink class="me" :to="{ name: 'profile' }" :title="`${account} · 个人信息`">
          <span class="me__avatar">{{ initial }}</span>
          <span class="me__meta">
            <span class="me__name">{{ account }}</span>
            <span class="me__role">{{ roleLabel }}</span>
          </span>
        </RouterLink>

        <button class="out" type="button" title="退出登录" aria-label="退出登录" @click="logout">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
            <path d="M16 17l5-5-5-5" />
            <path d="M21 12H9" />
          </svg>
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
/*
 * `.side` 只占位（宽度 = `--sidebar-w`）：真正的面板是**绝对定位**的，
 * 这样抽屉在窄屏浮出来时**不参与 flex 计算** ⟹ 内容区不会跟着跳。
 */
.side {
  position: relative;
  flex: 0 0 var(--sidebar-w);
  width: var(--sidebar-w);
  height: 100%;
}

.panel {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 20;
  display: flex;
  width: var(--sidebar-w);
  flex-direction: column;
  padding: 12px 10px 10px;
  overflow: hidden;
  border-right: 1px solid var(--border);
  background: var(--surface);
  transition: width 0.16s ease;
}

/* 抽屉浮出：`fixed` 才能逃开 `.app` 的横向滚动裁剪（见文件头） */
.side--open .panel {
  position: fixed;
  z-index: 60;
  width: 240px;
  box-shadow: var(--shadow);
  animation: drawer-in 0.16s ease-out;
}

@keyframes drawer-in {
  from {
    transform: translateX(-10px);
    opacity: 0.6;
  }

  to {
    transform: none;
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .side--open .panel {
    animation: none;
  }
}

.scrim {
  position: fixed;
  z-index: 50;
  inset: 0;
  background: rgba(16, 24, 40, 0.34);
}

/* ---------- 品牌 ---------- */

.top {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 2px 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  color: var(--text);
  font-size: 0.95rem;
  font-weight: 600;
  text-decoration: none;
}

.brand__mark {
  display: grid;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1;
}

.brand__text {
  overflow: hidden;
  white-space: nowrap;
}

.brand__text span {
  color: var(--primary);
}

.burger {
  display: grid;
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  margin-left: auto;
  padding: 0;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-muted);
}

.burger:hover {
  border-color: var(--border-strong);
  color: var(--text);
}

.burger svg,
.out svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* ---------- 导航 ---------- */

.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 16px;
}

.nav__item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 38px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 0.88rem;
  white-space: nowrap;
  text-decoration: none;
}

.nav__item:hover {
  background: var(--surface-sunken);
  color: var(--text);
}

.nav__item--on {
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 500;
}

.nav__icon {
  flex: 0 0 20px;
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.nav__label {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---------- 底部用户区 ---------- */

.foot {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}

.me {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  gap: 9px;
  min-width: 0;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  color: var(--text);
  text-decoration: none;
}

.me:hover {
  background: var(--surface-sunken);
}

.me__avatar {
  display: grid;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 0.82rem;
  font-weight: 600;
}

.me__meta {
  display: flex;
  min-width: 0;
  flex-direction: column;
  line-height: 1.3;
}

.me__name {
  overflow: hidden;
  font-size: 0.82rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.me__role {
  color: var(--text-faint);
  font-size: 0.7rem;
}

.out {
  display: grid;
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  padding: 0;
  place-items: center;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-faint);
}

.out:hover {
  border-color: var(--border);
  color: var(--danger);
}

/* ---------- 折叠档（1100–1439）：只留图标 ---------- */

.side--rail:not(.side--open) .brand__text,
.side--rail:not(.side--open) .nav__label,
.side--rail:not(.side--open) .me__meta {
  display: none;
}

.side--rail:not(.side--open) .top {
  justify-content: center;
}

.side--rail:not(.side--open) .brand {
  gap: 0;
}

/* 折叠时图标**居中**：靠左贴边会让整条侧栏看起来"塌了" */
.side--rail:not(.side--open) .nav__item {
  justify-content: center;
  padding: 0;
}

.side--rail:not(.side--open) .foot {
  flex-direction: column;
  gap: 6px;
}

.side--rail:not(.side--open) .me {
  flex: 0 0 auto;
  justify-content: center;
  padding: 5px 0;
}

/*
 * 窄屏（<1100）抽屉关着时，**☰ 顶替品牌标记的位置**：
 * 68px 里塞不下「28px 的品牌 ＋ 30px 的 ☰」（实测会被 `overflow: hidden` 裁掉品牌）。
 * 品牌在抽屉打开时照样看得到 —— 它本来就是"展开态"才该出现的东西。
 */
.side--narrow:not(.side--open) .brand {
  display: none;
}

.side--narrow:not(.side--open) .burger {
  margin: 0 auto;
}
</style>
