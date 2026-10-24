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
import { getConflicts, listReports } from '@/api/reports'
import type { ConflictFact, ConflictItem, ConflictReport, ReportSnapshot } from '@/types/api'

const snapshots = ref<ReportSnapshot[]>([])
const note = ref('')
const snapshotsLoading = ref(true)
const error = ref('')

/**
 * 知识库冲突体检。
 *
 * ⚠️ **和上面的评测快照是两件事**：那是读文件，这个要当场扫库。
 *    所以两者的加载 / 出错 / 权限状态**都分开管** —— 体检跑不起来，不该让评测快照也看不见。
 */
const conflicts = ref<ConflictReport | null>(null)
const conflictsLoading = ref(true)
/** 不是"零冲突"，而是"看不了 / 出错了"。这两种状态在界面上必须分开 */
const conflictsNote = ref('')

/** 层的中文名。后端只给 `kind`，展示是前端的事。 */
const KIND_LABEL: Record<string, string> = {
  retrieval: '检索层',
  generation: '生成层',
  refusal: '拒答层',
}

/** 冲突的类型。⚠️ 两种的**运维动作不同**：前者改内部稿，后者要走产品决策。 */
const CONFLICT_KIND_LABEL: Record<string, string> = {
  internal_vs_policy: '内部材料 ≠ 现行政策',
  active_vs_active: '两份现行政策互斥',
}

const STATUS_LABEL: Record<string, string> = {
  active: '现行政策',
  internal: '内部材料',
  superseded: '已废止',
  draft: '未生效',
  sibling_brand: '另一品牌',
}

const UNIT_LABEL: Record<string, string> = {
  day: '天',
  month: '个月',
  year: '年',
  hour: '小时',
}

/** 两块各自加载：刷新按钮在两块都落地前保持禁用。 */
const refreshing = computed(() => snapshotsLoading.value || conflictsLoading.value)

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

function conflictKindLabel(kind: string): string {
  return CONFLICT_KIND_LABEL[kind] ?? kind
}

function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status
}

/** `30.0` → `30 天`。⚠️ 单位族必须跟着数值一起出现，否则 `30` 说不清是"天"还是"个月"。 */
function formatValue(fact: ConflictFact): string {
  const unit = UNIT_LABEL[fact.unit_family] ?? fact.unit_family
  const value = Number.isInteger(fact.value) ? String(fact.value) : fact.value.toFixed(1)
  return `${value} ${unit}`
}

/** 两边并排显示。用数组而不是写两遍模板 —— 两边的渲染规则必须**完全一样**。 */
function sides(item: ConflictItem): ConflictFact[] {
  return [item.left, item.right]
}

async function loadConflicts(): Promise<void> {
  conflictsLoading.value = true
  conflictsNote.value = ''
  conflicts.value = null
  try {
    conflicts.value = await getConflicts()
  } catch (err) {
    // ⚠️ 403 不是错误，是**权限**：普通成员本来就看不到这一块，别把页面刷成红的
    conflictsNote.value =
      err instanceof ApiError && err.status === 403
        ? '这一块只对管理员开放：报告里会把内部材料和现行政策并排摆出来。'
        : `体检没跑起来：${err instanceof ApiError ? err.message : (err as Error).message}`
  } finally {
    conflictsLoading.value = false
  }
}

async function loadSnapshots(): Promise<void> {
  snapshotsLoading.value = true
  error.value = ''
  snapshots.value = []
  try {
    const page = await listReports()
    snapshots.value = page.snapshots
    note.value = page.note
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : `加载失败：${(err as Error).message}`
  } finally {
    snapshotsLoading.value = false
  }
}

async function load(): Promise<void> {
  // 两个请求互不依赖 → 一起发；谁失败谁自己管自己的提示
  await Promise.all([loadSnapshots(), loadConflicts()])
}

onMounted(load)
</script>

<template>
  <div class="shell">
    <AppHeader title="评测报告" hint="只读快照" tone="amber">
      <button class="btn btn--ghost btn--sm" type="button" :disabled="refreshing" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p class="hint">
        数字来自开发机上跑的 <code>evals/run.py</code>，产物是结果 JSON；这个页面<strong>只读</strong>它。
        每张卡片都标了<strong>生成时间</strong>与<strong>那次的配置</strong> ——
        换个参数数字就会变，所以不能脱离它们看。
      </p>

      <!-- 知识库冲突体检（现算，和下面的只读快照不是一回事） -->
      <section class="conflicts">
        <header class="conflicts__head">
          <h3 class="conflicts__title">知识库冲突体检 <span class="count">现算 · 只读</span></h3>
          <p class="conflicts__hint">
            同主题 ＋ 同适用范围 ＋ 同单位族，数值不同 → 列出来。
            <strong>只报不一致，不判谁对</strong>：每条都带原句，30 秒就能人工判断该改哪一份。
          </p>
        </header>

        <!-- 骨架屏：体检要现算（几秒），比别的加载态更长 —— 更不能只留一句"体检中…" -->
        <div v-if="conflictsLoading" class="sk-stack" aria-busy="true" aria-label="体检中">
          <div class="sk sk--line" style="width: 55%"></div>
          <div class="sk sk--block"></div>
        </div>
        <p v-else-if="conflictsNote" class="muted">{{ conflictsNote }}</p>

        <template v-else-if="conflicts">
          <p class="conflicts__stats">
            扫描 {{ conflicts.documents + conflicts.skipped_documents }} 篇 → 参与配对
            <strong>{{ conflicts.documents }}</strong> 篇
            <span class="count"
              >（跳过 {{ conflicts.skipped_documents }} 篇：讲解稿 / 辟谣稿写的是"别人的规则"）</span
            >
            · 抽出 <strong>{{ conflicts.facts }}</strong> 条期限事实 ·
            <strong :class="{ 'conflicts__total--bad': conflicts.conflicts > 0 }">{{
              conflicts.conflicts
            }}</strong>
            处冲突
          </p>

          <p v-if="conflicts.conflicts === 0" class="empty">
            没发现冲突。
            <span class="count">⚠️ 这是"没找到"，不是"没有" —— 它只认 4 个主题、只比同单位族。</span>
          </p>

          <ul v-else class="conflict-list">
            <li v-for="(item, index) in conflicts.items" :key="index" class="conflict">
              <header class="conflict__head">
                <span class="flag" :class="`flag--${item.kind}`">{{ conflictKindLabel(item.kind) }}</span>
                <span class="conflict__topic">{{ item.topic }} · {{ item.scope }}</span>
              </header>

              <div class="conflict__sides">
                <div v-for="fact in sides(item)" :key="fact.doc + fact.sentence" class="side">
                  <p class="side__head">
                    <span class="side__status">{{ statusLabel(fact.status) }}</span>
                    <span class="side__value">{{ formatValue(fact) }}</span>
                  </p>
                  <p class="side__doc">{{ fact.doc }}</p>
                  <!-- ⚠️ 原句必须显示：没有原句的告警等于"你自己去找" -->
                  <blockquote class="side__sentence">{{ fact.sentence }}</blockquote>
                  <p v-if="(fact.docs?.length ?? 0) > 1" class="side__more">
                    另有 {{ (fact.docs?.length ?? 1) - 1 }} 篇写着同样的值
                  </p>
                </div>
              </div>
            </li>
          </ul>
        </template>
      </section>

      <h3 class="all__title">评测快照</h3>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <!-- 骨架屏：快照是 3 张卡片 -->
      <div v-if="snapshotsLoading" class="cards" aria-busy="true" aria-label="加载中">
        <div v-for="n in 3" :key="n" class="sk sk--block" style="height: 132px"></div>
      </div>

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
            ⚠️ 同一层有多份是<strong>判分口径改过</strong>留下的中间态。横向比之前先看它们的配置是否一致。
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

/* ---------- 知识库冲突体检 ---------- */

.conflicts {
  margin: 0 0 28px;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.conflicts__head {
  margin-bottom: 10px;
}

.conflicts__title {
  margin: 0 0 4px;
  font-size: 0.95rem;
}

.conflicts__hint {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.7;
}

.conflicts__stats {
  margin: 0 0 12px;
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.7;
}

.conflicts__total--bad {
  color: var(--danger, #c0392b);
}

.conflict-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 12px;
}

.conflict {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--danger, #c0392b);
  border-radius: var(--radius);
}

.conflict__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.conflict__topic {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.flag {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.7rem;
  background: var(--primary-soft);
  color: var(--text-muted);
}

/* 两种冲突的**紧急性不同**：内部稿写错是我们自己能改的；两份现行互斥要走产品决策 */
.flag--internal_vs_policy {
  background: #fff4e5;
  color: #8a5300;
}

.flag--active_vs_active {
  background: #fdecea;
  color: #a12a1e;
}

.conflict__sides {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
}

.side {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg, transparent);
}

.side__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin: 0 0 4px;
}

.side__status {
  color: var(--text-muted);
  font-size: 0.72rem;
}

.side__value {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.9rem;
  font-variant-numeric: tabular-nums;
}

.side__doc {
  margin: 0 0 6px;
  color: var(--text-faint);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.7rem;
  overflow-wrap: anywhere;
}

.side__sentence {
  margin: 0;
  padding-left: 8px;
  border-left: 2px solid var(--border);
  color: var(--text-muted);
  font-size: 0.76rem;
  line-height: 1.6;
}

.side__more {
  margin: 6px 0 0;
  color: var(--text-faint);
  font-size: 0.7rem;
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

/* 卡片/表格的边框与密度统一走全局（`main.css`），这里只留本页差异 */
.card__head {
  margin-bottom: 12px;
}

.card__title {
  margin: 0 0 4px;
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

/* 表格密度统一走全局 `.table`（`main.css`） */
.nowrap {
  white-space: nowrap;
}

.file {
  color: var(--text-faint);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.72rem;
}
</style>
