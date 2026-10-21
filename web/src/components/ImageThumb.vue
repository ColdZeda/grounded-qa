<script setup lang="ts">
/**
 * 一张消息里的缩略图。
 *
 * 它单独成一个组件，唯一原因是**取图是异步且要带 token 的**（见 `api/images.ts`）：
 * 图片不是 `<img src="/api/v1/images/…">` 就能拿到的，得先 fetch 成 Blob。
 * 把这个过程关在组件里，消息渲染的地方就只写一个 `<ImageThumb :image-id="…">`。
 *
 * ⚠️ 三种状态都要有**看得见的区别**，不能都表现成"一块空白"：
 *   加载中（骨架）· 加载失败（一句人话）· 正常（可点开看原图）
 */

import { ref, watch } from 'vue'

import { imageUrl } from '@/api/images'

const props = defineProps<{ imageId: string }>()

const src = ref('')
const failed = ref(false)

async function resolve(id: string): Promise<void> {
  src.value = ''
  failed.value = false
  try {
    const url = await imageUrl(id)
    // ⚠️ 组件会被**复用**（`v-for` 的 key 是消息 id，但重建列表时可能拿到同一个实例）：
    //    等待期间 `imageId` 可能已经变了，那就丢掉这次的结果，否则会显示**别人的图**。
    if (props.imageId === id) src.value = url
  } catch {
    if (props.imageId === id) failed.value = true
  }
}

watch(
  () => props.imageId,
  (id) => {
    void resolve(id)
  },
  { immediate: true },
)
</script>

<template>
  <span v-if="failed" class="thumb thumb--failed" title="图片没取回来（可能已被清理）">
    图片加载失败
  </span>
  <span v-else-if="!src" class="thumb thumb--loading" aria-label="图片加载中">…</span>
  <!--
    ⚠️ 用 `<a target="_blank">` 直接指向 blob URL，而不是自己做弹层：
       blob URL 是同源的，浏览器新标签页能直接打开，缩放 / 保存都是原生行为。
       自己写弹层要额外处理 Esc、点遮罩、滚动锁 —— 换不来更好的效果。
    ⚠️ `rel="noopener"`：不加的话新页面能通过 `window.opener` 反向操纵本页。
  -->
  <a v-else class="thumb" :href="src" target="_blank" rel="noopener" title="点开看原图">
    <img :src="src" alt="这条消息带的图" />
  </a>
</template>

<style scoped>
.thumb {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  max-width: 220px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  overflow: hidden;
}

.thumb img {
  display: block;
  max-width: 220px;
  /* ⚠️ 只限宽不限高的话，一张长截图会把整屏撑开、把回答挤到看不见的地方。 */
  max-height: 220px;
  object-fit: contain;
}

.thumb--loading,
.thumb--failed {
  padding: 18px 22px;
  color: var(--text-faint);
  font-size: 0.8rem;
}

.thumb--failed {
  border-style: dashed;
}
</style>
