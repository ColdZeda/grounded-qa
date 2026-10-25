<script setup lang="ts">
/**
 * 页面动作条（**不是**全局顶栏）。
 *
 * 2026-10-25 的 P2 之前，这里承担了品牌 / 导航 / 用户 / 退出 —— 那是"应用顶栏"的角色。
 * 现在那些都在 `AppSidebar.vue` 里，这一条只剩**这一页自己的东西**：
 *   左：色块 ＋ 页面标题 ＋ 一句口径（"只读 · 按北京时间自然日"这类）
 *   右：本页的动作按钮（插槽）＋ 环境角标
 *
 * ⚠️ 环境角标（`本地` / `生产`）**故意留在这里、而且永远带文字**：
 *    用户 2026-10-22 在两套环境之间反复搞混，为"密码为什么错"白排查了半小时。
 *    它不是装饰，是"我现在动的是哪套数据"的唯一常驻提示 —— 不能只在悬停里。
 */

import { runtimeEnv } from '@/stores/env'

/**
 * `tone` = 页头分区色。
 * 走查里"整页全白、看不出自己在哪"是条真意见：每个分区一个色，
 * 换页时**只靠颜色也能认出来**，但颜色很淡（4% 混底），不至于变花。
 */
type Tone = 'blue' | 'teal' | 'violet' | 'amber' | 'green' | 'slate'

withDefaults(
  defineProps<{
    title: string
    hint?: string
    tone?: Tone
    /**
     * 返回目标（路由路径）。给了才出现 `←` 按钮。
     *
     * ⚠️ 为什么要有它（2026-10-25 走查）：会话页当时只能靠**左下角侧栏的「会话」**
     *    回列表，而它长得像"分区入口"、不像"返回" —— 外人第一反应是找不到路。
     */
    back?: string
    /** 返回按钮上的字（默认「返回」）。会话页写「会话列表」更明确。 */
    backLabel?: string
  }>(),
  {
    hint: '',
    tone: 'blue',
    back: '',
    backLabel: '返回',
  },
)
</script>

<template>
  <header class="head" :class="`head--${tone}`">
    <RouterLink v-if="back" class="head__back" :to="back" :title="backLabel">
      <span aria-hidden="true">←</span>
      <span class="head__back-text">{{ backLabel }}</span>
    </RouterLink>
    <span class="head__dot" aria-hidden="true"></span>
    <h1 class="head__title" :title="title">{{ title }}</h1>
    <span v-if="hint" class="head__hint">{{ hint }}</span>

    <div class="head__actions">
      <slot />
    </div>

    <!--
      ⚠️ 条件写在**表达式**里（`runtimeEnv === 'dev'`），不是裸标识符 ——
         `AGENTS §10` 的编译期常量折叠：裸标识符当整个条件时 `v-if` 会整段消失。
      ⚠️ `unknown` 时不显示：宁可没有，也不要标错。
    -->
    <span
      v-if="runtimeEnv === 'dev'"
      class="env env--dev"
      title="连的是本地后端（localhost:58000）—— 这里的账号/数据与生产完全独立"
    >
      本地
    </span>
    <span
      v-else-if="runtimeEnv === 'prod'"
      class="env env--prod"
      title="连的是生产后端（120.24.67.82）—— 改动会动到线上数据"
    >
      生产
    </span>
  </header>
</template>

<style scoped>
.head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  /*
   * ⚠️ **不写死 `height`**：窗口窄 / 有浏览器缩放时内容会长高，
   *    写死高度就变成"这一条被切掉一半、字看不全"（2026-10-24 走查在 /usage 撞到）。
   */
  min-height: 56px;
  padding: 8px 20px;
  border-bottom: 1px solid color-mix(in srgb, var(--tone) 22%, var(--border));
  background: color-mix(in srgb, var(--tone) 4%, var(--surface));
}

/* 分区色。一边一个变量名，换配色只改这一处 */
.head--blue {
  --tone: #2563eb;
}

.head--teal {
  --tone: #0e9384;
}

.head--violet {
  --tone: #7a5af8;
}

.head--amber {
  --tone: #b54708;
}

.head--green {
  --tone: #067647;
}

.head--slate {
  --tone: #475467;
}

.head__dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 3px;
  background: var(--tone);
}

/*
 * 返回按钮：**带文字**，不只一个 `←`。
 * 只有一个箭头的"返回"在走查里被当成了装饰（人不敢点）；文字让它变成一条路。
 */
.head__back {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 0.8rem;
  text-decoration: none;
}

.head__back:hover {
  border-color: var(--text-faint);
  color: var(--text);
}

/* 窄一点就只留箭头（这时候标题和按钮更值钱），但 `title` 仍在 */
@media (max-width: 1199.98px) {
  .head__back-text {
    display: none;
  }
}

.head__title {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  font-size: 1.02rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.head__hint {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--text-faint);
  font-size: 0.76rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.head__actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  /* 动作按钮靠右：标题和按钮之间永远有一条弹性空隙 */
  margin-left: auto;
}

/* 环境角标：小、但不是装饰 —— 它回答"我现在动的是哪套数据" */
.env {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.env--dev {
  border: 1px solid var(--warn);
  background: var(--warn-soft);
  color: var(--warn);
}

.env--prod {
  border: 1px solid var(--ok);
  background: #ecfdf3;
  color: var(--ok);
}
</style>
