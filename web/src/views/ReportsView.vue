<script setup lang="ts">
/**
 * 评测报告（**只读快照**）。
 *
 * ⚠️ 这个页面**不在服务器上跑评测** —— 它只读 `evals/results/*.json`
 *    （报告的原始文件进镜像，题集与 runner 不进，见 `AGENTS.md §13`）。
 *
 * ⚠️ **不许裸数字**，这是本页最硬的约束：
 *    每个指标都带分母（`0.920（23/25）`），每张卡片都带**生成时间 ＋ 那次运行的配置**。
 *    理由：换个 `fulltext_min` 检索命中率就能从 0.84 变 0.92 —— 只放一个 0.92，
 *    读者会以为那是"系统的检索准确率"，而它其实是"某次配置下的 25 道题"。
 *
 * ⚠️ 同一层出现多份快照是**正常的**（判分口径改过三次）。所以：
 *    · 卡片只显示每层**最新的一份**（当"现在能讲的故事"）
 *    · 下面列出全部快照，让人知道还有别的口径 —— 但**不鼓励横向比**
 */

import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { listReports } from '@/api/reports'
import type { ReportSnapshot } from '@/types/api'

const snapshots = ref<ReportSnapshot[]>([])
const note = ref('')
const loading = ref(true)
const error = ref('')

/** 层的中文名。后端只给 `kind`，展示是前端的事。 */
const KIND_LABEL: Record<string, string> = {
  retrieval: '检索层',
  generation: '生成层',
  refusal: '拒答层',
}

/** 每层最新的一份（列表已按时间倒序，所以"第一次出现"就是最新）。 */
const latest = computed<ReportSnapshot[]>(() => {
  const seen = new Set<string>()
  const picked: ReportSnapshot[] = []
  for (const item of snapshots.value) {
    if (seen.has(item.kind)) continue
    seen.add(item.kind)
    picked.push(item)
  }
  return picked
})

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const page = await listReports()
    snapshots.value = page.snapshots
    note.value = page.note
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <AppHeader>
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <h2 class="title">评测报告 <span class="count">只读快照</span></h2>

      <p class="hint">
        数字来自开发机上跑的 <code>evals/run.py</code>，产物是结果 JSON；这个页面**只读**它。
        每张卡片都标了**生成时间**与**那次的配置** —— 换个参数数字就会变，所以不能脱离它们看。
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="loading" class="muted">加载中…</p>

      <template v-else>
        <p v-if="note" class="note">{{ note }}</p>

        <p v-if="snapshots.length === 0" class="empty">还没有评测结果。</p>

        <section v-else class="cards">
          <article v-for="snap in latest" :key="snap.file" class="card">
            <header class="card__head">
              <h3 class="card__title">{{ kindLabel(snap.kind) }}</h3>
              <span class="card__stamp">
                该层最新一份 · 只读快照 · 生成于 {{ snap.generated_at_display }} ·
                {{ snap.questions }} 题
              </span>
            </header>

            <dl class="metrics">
              <div v-for="metric in snap.metrics" :key="metric.name" class="metric">
                <dt class="metric__name" :title="metric.hint">{{ metric.name }}</dt>
                <dd class="metric__value">{{ metric.value }}</dd>
              </div>
            </dl>

            <footer class="config">
              <span v-for="(value, key) in snap.config" :key="key" class="chip">
                <span class="chip__key">{{ key }}</span>{{ value }}
              </span>
            </footer>
          </article>
        </section>

        <section v-if="snapshots.length > 0" class="all">
          <h3 class="all__title">全部快照（{{ snapshots.length }} 份，新的在前）</h3>
          <p class="all__hint">
            ⚠️ 同一层有多份是**判分口径改过**留下的中间态。横向比之前先看它们的配置是否一致。
          </p>
          <table class="table">
            <thead>
              <tr>
                <th>层</th>
                <th>生成时间</th>
                <th>题数</th>
                <th>文件</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="snap in snapshots" :key="snap.file">
                <td>{{ kindLabel(snap.kind) }}</td>
                <td class="nowrap">{{ snap.generated_at_display }}</td>
                <td>{{ snap.questions }}</td>
                <td class="file">{{ snap.file }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.body {
  flex: 1;
  overflow-y: auto;
  width: 100%;
  max-width: 960px;
  margin: 0 auto;
  padding: 28px 20px 60px;
}

.title {
  margin: 0 0 8px;
  font-size: 1.1rem;
}

.count {
  color: var(--text-faint);
  font-size: 0.8rem;
  font-weight: 400;
}

.hint {
  margin: 0 0 14px;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.7;
}

.hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--primary-soft);
  font-size: 0.8rem;
}

.note {
  margin: 0 0 18px;
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
  color: var(--text-muted);
  font-size: 0.8rem;
  line-height: 1.7;
}

.muted {
  color: var(--text-muted);
}

.empty {
  padding: 40px 0;
  color: var(--text-faint);
  text-align: center;
}

.cards {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}

.card {
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.card__head {
  margin-bottom: 12px;
}

.card__title {
  margin: 0 0 4px;
  font-size: 0.95rem;
}

/* 生成时间必须显眼到"不能忽略"—— 它是数字可信度的一部分 */
.card__stamp {
  color: var(--text-faint);
  font-size: 0.72rem;
}

.metrics {
  display: grid;
  gap: 6px;
  margin: 0 0 12px;
}

.metric {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.metric__name {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.metric__value {
  margin: 0;
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.82rem;
  font-variant-numeric: tabular-nums;
}

.config {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}

.chip {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--text-muted);
  font-size: 0.7rem;
}

.chip__key {
  margin-right: 4px;
  color: var(--text-faint);
}

.all {
  margin-top: 32px;
}

.all__title {
  margin: 0 0 6px;
  font-size: 0.9rem;
}

.all__hint {
  margin: 0 0 12px;
  color: var(--text-faint);
  font-size: 0.75rem;
  line-height: 1.6;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.table th,
.table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}

.table th {
  color: var(--text-faint);
  font-weight: 500;
}

.nowrap {
  white-space: nowrap;
}

.file {
  color: var(--text-faint);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.72rem;
}
</style>
