<script setup lang="ts">
/**
 * 引用来源列表 —— **这个项目的展示重点**。
 *
 * ⚠️ 有一件事是刻意做的：**分数必须连"口径"一起显示**，不能只写一个"相关度 0.62"。
 *
 *     三种分数的量纲完全不同：
 *       · `rerank`       交叉编码器（query 和文档拼在一起过一遍模型）打的分
 *       · `vector_cosine` 两个向量的余弦相似度
 *       · `fulltext_trgm` pg_trgm 的 `word_similarity`
 *     0.62 的精排分和 0.62 的余弦相似度**不是一回事**，摆在一起会让人以为可比。
 *     这也是后端 `Citation.score_kind` 字段存在的唯一理由 ——
 *     前端如果不显示它，那个字段就白设计了。
 */

import type { Citation, ScoreKind } from '@/types/api'
import { isDetailed } from '@/stores/prefs'

defineProps<{
  citations: Citation[]
  /** 用户点了正文里的 `[n]` 时要高亮哪一条。 */
  activeIndex: number | null
}>()

/**
 * 点「看原文」→ 让**页面**（`ChatView`）去开抽屉。
 *
 * ⚠️ 抽屉不挂在这个组件里：一条消息可能有 10 条来源，挂 10 个抽屉 = 10 份状态
 *    （而且"同时打开两个"这种蠢事迟早发生）。抽屉只能有一个，所以状态放在页面级。
 */
const emit = defineEmits<{
  (event: 'open-context', payload: { documentId: string; ordinal: number; title: string }): void
}>()

/** 这条来源能不能点开：老数据没有 `ordinal` ⟹ 没有按钮（而不是点了报错）。 */
function canOpen(cite: Citation): boolean {
  return typeof cite.ordinal === 'number' && cite.ordinal >= 0
}

function open(cite: Citation): void {
  if (!canOpen(cite)) return
  emit('open-context', {
    documentId: cite.document_id,
    ordinal: cite.ordinal as number,
    title: `来源 [${cite.index}]`,
  })
}

const KIND_LABEL: Record<ScoreKind, string> = {
  rerank: '精排分',
  vector_cosine: '向量余弦',
  fulltext_trgm: '全文相似',
}

const KIND_HINT: Record<ScoreKind, string> = {
  rerank: '交叉编码器打分（query 与文档拼在一起过模型）。拒答阈值只挂在这个口径上。',
  vector_cosine: 'query 向量与片段向量的余弦相似度。量纲与精排分不同，不可直接比较。',
  fulltext_trgm: 'pg_trgm 的 word_similarity，衡量「query 像不像文中的某一小段」。量纲又不同。',
}

function kindLabel(kind: string): string {
  return KIND_LABEL[kind as ScoreKind] ?? kind
}

function kindHint(kind: string): string {
  return KIND_HINT[kind as ScoreKind] ?? ''
}
</script>

<template>
  <div class="citations">
    <!--
      「简洁」档藏的是分数（不是来源），但**必须说出来** —— 否则用户点了开关
      会觉得"什么也没变"（2026-10-24 走查的原话）。
      ⚠️ `isDetailed()` 必须是**函数调用**（裸标识符会被编译器常量折叠）。
    -->
    <p v-if="!isDetailed()" class="citations__hint">
      分数已隐藏（切到「详细」可看精排分与口径）
    </p>

    <div v-for="cite in citations" :key="cite.chunk_id" class="cite" :class="{ 'cite--active': cite.index === activeIndex }">
      <div class="cite__head">
        <span class="cite__index">[{{ cite.index }}]</span>
        <!--
          ⚠️ 分数只在**详细**档显示（architecture.md §9.19）。
             「简洁」档藏的是**分数**，不是来源 —— 来源要留着，它建立信任。
          ⚠️ 写成 `isDetailed()`（**函数调用**）而不是 `isDetailed`：
             裸标识符会被编译器当常量折叠掉，`v-if` 直接消失（见 prefs.ts 的注释）。
        -->
        <span v-if="isDetailed()" class="chip" :title="kindHint(cite.score_kind)">
          {{ kindLabel(cite.score_kind) }} {{ cite.score.toFixed(3) }}
        </span>
        <!--
          「看原文」：摘要只有 160 字，**判断不了有没有断章取义**（2026-10-25 走查）。
          点开 → 页面级抽屉显示这一块 ＋ 前后各一块。老数据没有 `ordinal` ⟹ 不出现这个按钮。
        -->
        <button
          v-if="canOpen(cite)"
          class="cite__open"
          type="button"
          :title="`看第 ${(cite.ordinal ?? 0) + 1} 块的原文（含上下文）`"
          @click="open(cite)"
        >
          看原文 · 第 {{ (cite.ordinal ?? 0) + 1 }} 块 ›
        </button>
      </div>
      <p class="cite__snippet">{{ cite.snippet }}</p>
    </div>
  </div>
</template>

<style scoped>
.citations {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.citations__hint {
  margin: 0;
  color: var(--text-faint);
  font-size: 0.75rem;
}

.cite {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  transition:
    border-color 0.2s,
    background 0.2s;
}

/* 被正文里的 [n] 点中时高亮 —— 这是"引用溯源"能不能用的关键：
   没有它，用户看到 [2] 得自己在下面找哪条是 2 */
.cite--active {
  border-color: var(--primary);
  border-left-color: var(--primary);
  background: var(--primary-soft);
}

.cite__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.cite__index {
  color: var(--primary);
  font-family: var(--mono);
  font-size: 0.8rem;
  font-weight: 600;
}

.cite__snippet {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.875rem;
  line-height: 1.55;
}

/* 「看原文」：做得小、靠右 —— 它是**二级动作**（先读摘要，想核对才点），
   但它必须在场：摘要能证明"有来源"，证明不了"没断章取义"。 */
.cite__open {
  margin-left: auto;
  padding: 1px 7px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.72rem;
  white-space: nowrap;
}

.cite__open:hover {
  border-color: var(--primary);
  color: var(--primary);
}
</style>
