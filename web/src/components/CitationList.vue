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
</style>
