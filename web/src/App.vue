<script setup lang="ts">
/**
 * 根组件 = **应用骨架**（左侧栏 ＋ 内容区）。
 *
 * ⚠️ 2026-10-25（P2）这里改过一次口径，改的原因记在这：
 *    以前本文件"故意只放一个 `<RouterView />`"，理由是"页面级布局该由布局组件承担，
 *    不该堆在根组件上"。这句话对**页面级**布局仍然成立，但**侧栏不是页面级布局** ——
 *    它跨页常驻、折叠/抽屉状态与具体页面无关。放在每个页面里 = 8 份状态，
 *    必然不同步（而且每跳一次页就重建一次，抽屉会自己关上）。
 *
 * 仍然保留的例外：**登录页不套侧栏**（`meta.public`）。它是唯一一个
 * "还不知道自己是谁"的页面，套上侧栏会出现「成员/运维」这种点不动的入口。
 */

import { useRoute } from 'vue-router'

import AppSidebar from '@/components/AppSidebar.vue'

const route = useRoute()

/**
 * ⚠️ 写成**函数调用**而不是裸标识符：`AGENTS §10` 的编译期常量折叠坑 ——
 *    `v-if="someComputed"`（裸标识符当整个条件）会被编译器折叠成恒真。
 */
function isBare(): boolean {
  return route.meta.public === true
}
</script>

<template>
  <RouterView v-if="isBare()" />
  <div v-else class="app">
    <AppSidebar />
    <div class="app__main">
      <RouterView />
    </div>
  </div>
</template>
