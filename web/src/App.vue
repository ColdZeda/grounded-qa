<script setup lang="ts">
/**
 * 根组件。
 *
 * ⚠️ 这里**故意只放一个 `<RouterView />`**：
 *    页面级布局（顶栏、侧栏）该由布局组件承担，不该堆在根组件上。
 *    堆上来的话，将来加一个"不需要顶栏"的页面（比如大屏看板）就得在这里写条件判断。
 */
</script>

<template>
  <RouterView />
</template>
