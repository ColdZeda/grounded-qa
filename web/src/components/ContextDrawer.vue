<script setup lang="ts">
/**
 * 「看原文」抽屉：显示检索命中的那一块 ＋ 前后各一块。
 *
 * ## 为什么是抽屉，不是在会话里就地展开
 *
 * 会话是**读答案**的地方，核对是**临时跑出去看一眼**的动作。就地展开会让消息流
 * 忽长忽短、滚动位置乱跳；抽屉不占版面，关掉就回到原样。
 * 窄屏（<900）抽屉变成整屏 —— 半屏抽屉在手机上只会剩一条缝。
 *
 * ## 三条硬约束（PLAN §12.3 P2.6）
 *
 * ⚠️ ① **只取 `ordinal ± 1`，不拉整篇**：流量是一方面，更重要的是"你看到的是片段"
 *    这件事要一直成立 —— 整篇会让人以为那就是全部，然后拿别处的原文来对不上。
 * ⚠️ ② **点了才请求**：不点就零流量，会话默认视图一个字不变。
 * ⚠️ ③ 上一块 / 下一块只用**已拿到的 `total`** 判断边界，不额外请求。
 */

import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { getChunkContext } from '@/api/documents'
import { ApiError } from '@/api/client'
import { renderChunk } from '@/lib/markdown'
import type { DocumentChunksOut } from '@/types/api'

const props = defineProps<{
  documentId: string
  /** 点开的那一块（从引用卡上来）。 */
  ordinal: number
  /** 小标题（`来源 [2]`）—— 让人知道自己在核对哪一条引用。 */
  title: string
}>()

const emit = defineEmits<{ (event: 'close'): void }>()

const data = ref<DocumentChunksOut | null>(null)
const loading = ref(true)
const error = ref('')
/** 当前看的是哪一块（上一块 / 下一块会改它）。 */
const current = ref(props.ordinal)

async function load(around: number): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    data.value = await getChunkContext(props.documentId, around)
    current.value = data.value.ordinal
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

function step(delta: number): void {
  const total = data.value?.total ?? 0
  const next = current.value + delta
  if (next < 0 || next >= total) return
  void load(next)
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
  // 左右方向键翻块：核对时手不用离开键盘
  if (event.key === 'ArrowLeft') step(-1)
  if (event.key === 'ArrowRight') step(1)
}

onMounted(() => {
  void load(props.ordinal)
  window.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

// 换了一条引用（同一个抽屉实例复用）就重新取
watch(
  () => [props.documentId, props.ordinal],
  () => void load(props.ordinal),
)
</script>

<template>
  <div class="drawer">
    <div class="drawer__scrim" @click="emit('close')"></div>

    <aside class="drawer__panel" role="dialog" aria-modal="true" aria-label="原文上下文">
      <header class="drawer__head">
        <div class="drawer__heading">
          <span class="drawer__badge">{{ title }}</span>
          <h3 class="drawer__file" :title="data?.filename ?? ''">
            {{ data?.filename ?? '加载中…' }}
          </h3>
        </div>
        <button class="drawer__close" type="button" aria-label="关闭" title="关闭（Esc）" @click="emit('close')">
          ✕
        </button>
      </header>

      <div class="drawer__bar">
        <button
          class="drawer__step"
          type="button"
          :disabled="loading || current <= 0"
          title="上一块（←）"
          @click="step(-1)"
        >
          ← 上一块
        </button>
        <!-- ⚠️ 分母用**这一块的序号 + 1 / 总数**：人要知道"这是第几块、还有多少" -->
        <span class="drawer__pos">
          第 {{ (data?.total ?? 0) === 0 ? '—' : current + 1 }} / {{ data?.total ?? '—' }} 块
        </span>
        <button
          class="drawer__step"
          type="button"
          :disabled="loading || data === null || current >= (data.total - 1)"
          title="下一块（→）"
          @click="step(1)"
        >
          下一块 →
        </button>
      </div>

      <div class="drawer__body">
        <p v-if="loading" class="drawer__muted">加载中…</p>
        <p v-else-if="error" class="alert alert--danger">{{ error }}</p>
        <p v-else-if="!data || data.items.length === 0" class="drawer__muted">
          这份文档还没有切片（或已被清理）。
        </p>
        <template v-else>
          <article
            v-for="item in data.items"
            :key="item.ordinal"
            class="block"
            :class="{ 'block--hit': item.hit }"
          >
            <p class="block__head">
              第 {{ item.ordinal + 1 }} 块
              <span v-if="item.hit" class="block__tag">被引用的就是这块</span>
            </p>
            <!--
              ⚠️ 过 markdown 渲染（`renderChunk`）：语料里的政策表本来就是 markdown 表格，
                  直接当纯文本显示会是一屏 `|---|---|` 源码。`html: false` 那条红线不变。
            -->
            <div class="block__text" v-html="renderChunk(item.content)"></div>
          </article>
        </template>
      </div>

      <footer class="drawer__foot">
        只显示这一块和前后各一块 —— <b>不是全文</b>。要读完整文档请回源查看。
      </footer>
    </aside>
  </div>
</template>

<style scoped>
.drawer {
  position: fixed;
  z-index: 80;
  inset: 0;
}

.drawer__scrim {
  position: absolute;
  inset: 0;
  background: rgba(16, 24, 40, 0.34);
}

/*
 * ⚠️ 抽屉自己 **没有 `overflow` 的限制问题**：它是 `fixed`，不受 `.app` 的
 *    `overflow-x: auto` 裁剪（P2 的侧栏抽屉踩过这个坑，见 architecture §9.45 ③）。
 */
.drawer__panel {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  display: flex;
  width: min(560px, 100%);
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--surface);
  box-shadow: var(--shadow);
}

/* 窄屏：整屏 —— 半屏抽屉在手机上只剩一条缝 */
@media (max-width: 899.98px) {
  .drawer__panel {
    width: 100%;
  }
}

.drawer__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}

.drawer__heading {
  min-width: 0;
}

.drawer__badge {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary);
  font-family: var(--mono);
  font-size: 0.72rem;
}

.drawer__file {
  margin: 4px 0 0;
  overflow: hidden;
  font-size: 0.9rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drawer__close {
  margin-left: auto;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-faint);
  font-size: 0.9rem;
}

.drawer__close:hover {
  border-color: var(--border);
  color: var(--text);
}

.drawer__bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-sunken);
}

.drawer__step {
  padding: 3px 9px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.78rem;
}

.drawer__step:disabled {
  opacity: 0.45;
}

.drawer__pos {
  margin: 0 auto;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 0.75rem;
}

.drawer__body {
  flex: 1;
  padding: 14px;
  overflow-y: auto;
}

.drawer__muted {
  color: var(--text-muted);
  font-size: 0.85rem;
}

.block {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
}

.block + .block {
  margin-top: 8px;
}

/* 命中块高亮：前后两块是上下文，**这一块才是被引用的** */
.block--hit {
  border-color: var(--primary);
  background: var(--primary-soft);
}

.block__head {
  margin: 0 0 6px;
  color: var(--text-faint);
  font-size: 0.72rem;
}

.block__tag {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--primary);
  color: #fff;
  font-size: 0.68rem;
}

.block__text {
  margin: 0;
  font-size: 0.85rem;
  line-height: 1.7;
}

/*
 * 片段里的 markdown 是 `v-html` 出来的，**scoped 样式管不到** ⟹ 必须 `:deep()`。
 * 只治排版，不改语义：表格给边框、列表收窄缩进、代码块等宽。
 */
.block__text :deep(p) {
  margin: 0 0 8px;
}

.block__text :deep(p:last-child) {
  margin-bottom: 0;
}

.block__text :deep(table) {
  width: 100%;
  margin: 8px 0;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.block__text :deep(th),
.block__text :deep(td) {
  padding: 4px 8px;
  border: 1px solid var(--border);
  text-align: left;
}

.block__text :deep(th) {
  background: var(--surface);
  color: var(--text-muted);
  font-weight: 500;
}

.block__text :deep(ul),
.block__text :deep(ol) {
  margin: 6px 0;
  padding-left: 18px;
}

.block__text :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--surface);
  font-family: var(--mono);
  font-size: 0.8rem;
}

.drawer__foot {
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 0.72rem;
}
</style>
