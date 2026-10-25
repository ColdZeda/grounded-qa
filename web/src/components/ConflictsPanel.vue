<script setup lang="ts">
/**
 * 知识库体检（冲突检查）—— 从「评测报告」页搬过来的（2026-10-25 走查）。
 *
 * ## 为什么搬
 *
 * 冲突是**知识库的健康**，不是**评测分数**：
 * 「评测报告」回答"模型答得怎么样"（只读快照），「知识库」回答"库里有没有互相打架的条款"。
 * 原来放在报告页是 v1.4 时的权宜（"前端将来都在报告页读它"），现在拆开更清楚。
 *
 * ## 两个刻意的设计
 *
 * ⚠️ ① **默认收起、点开才请求**：体检要扫全库（几十篇、几秒），
 *    而知识库页的主活是"传 / 看文档"，不该让一个每次都要算几秒的块挡在最前面。
 * ⚠️ ② **标题行直接写结论**（`3 处冲突`）：不展开也知道有事没有 ——
 *    要展开才知道有没有事，等于这个块不存在。
 */

import { ref } from 'vue'

import { ApiError } from '@/api/client'
import { getConflicts } from '@/api/reports'
import type { ConflictFact, ConflictItem, ConflictReport } from '@/types/api'

const open = ref(false)
const report = ref<ConflictReport | null>(null)
const loading = ref(false)
/** 不是"零冲突"，而是"看不了 / 出错了" —— 这两种状态必须分开显示 */
const note = ref('')

/** 冲突类型。⚠️ 两种的**运维动作不同**：前者改内部稿，后者要走产品决策。 */
const KIND_LABEL: Record<string, string> = {
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

function conflictKindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind
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

async function load(): Promise<void> {
  loading.value = true
  note.value = ''
  try {
    report.value = await getConflicts()
  } catch (err) {
    // ⚠️ 403 不是错误，是**权限**（这块只给管理员）
    note.value =
      err instanceof ApiError && err.status === 403
        ? '体检只对管理员开放：它会把内部材料和现行政策并排摆出来。'
        : `体检没跑起来：${err instanceof ApiError ? err.message : (err as Error).message}`
  } finally {
    loading.value = false
  }
}

/** 第一次展开才请求；之后展开不重复请求（要最新就点右上角「重新体检」）。 */
function toggle(): void {
  open.value = !open.value
  if (open.value && report.value === null && note.value === '') void load()
}

function recheck(): void {
  void load()
}
</script>

<template>
  <section class="check">
    <header class="check__head">
      <button
        class="check__toggle"
        type="button"
        :aria-expanded="open"
        :title="open ? '收起体检' : '展开体检（会扫一遍全库）'"
        @click="toggle"
      >
        <span class="check__arrow" aria-hidden="true">{{ open ? '▾' : '▸' }}</span>
        <span class="check__title">知识库体检</span>
        <!-- 结论直接放在标题上：不展开也知道有事没有 -->
        <span
          v-if="report"
          class="check__badge"
          :class="{ 'check__badge--bad': report.conflicts > 0 }"
        >
          {{ report.conflicts > 0 ? `${report.conflicts} 处冲突` : '无冲突' }}
        </span>
        <span v-else-if="loading" class="check__badge">体检中…</span>
        <span v-else class="check__badge check__badge--idle">点开才跑 · 现算</span>
      </button>

      <button v-if="open" class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="recheck">
        重新体检
      </button>
    </header>

    <p class="check__hint">
      同主题 ＋ 同适用范围 ＋ 同单位族，数值不同 → 列出来。
      <strong>只报不一致，不判谁对</strong>：每条都带原句，30 秒就能人工判断该改哪一份。
    </p>

    <div v-if="open" class="check__body">
      <!-- 骨架屏：体检要现算（几秒），比别的加载态更长 -->
      <div v-if="loading" class="sk-stack" aria-busy="true" aria-label="体检中">
        <div class="sk sk--line" style="width: 55%"></div>
        <div class="sk sk--block"></div>
      </div>
      <p v-else-if="note" class="check__note">{{ note }}</p>

      <template v-else-if="report">
        <p class="check__stats">
          扫描 {{ report.documents + report.skipped_documents }} 篇 → 参与配对
          <strong>{{ report.documents }}</strong> 篇
          <span class="check__aside"
            >（跳过 {{ report.skipped_documents }} 篇：讲解稿 / 辟谣稿写的是"别人的规则"）</span
          >
          · 抽出 <strong>{{ report.facts }}</strong> 条期限事实 ·
          <strong :class="{ 'check__total--bad': report.conflicts > 0 }">{{
            report.conflicts
          }}</strong>
          处冲突
        </p>

        <p v-if="report.conflicts === 0" class="check__empty">
          没发现冲突。
          <span class="check__aside">⚠️ 这是"没找到"，不是"没有" —— 它只认 4 个主题、只比同单位族。</span>
        </p>

        <ul v-else class="conflict-list">
          <li v-for="(item, index) in report.items" :key="index" class="conflict">
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
    </div>
  </section>
</template>

<style scoped>
.check {
  margin: 0 0 20px;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.check__head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.check__toggle {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text);
  text-align: left;
}

.check__arrow {
  color: var(--text-faint);
  font-size: 0.8rem;
}

.check__title {
  font-size: 0.9rem;
  font-weight: 600;
}

.check__badge {
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--ok, #067647);
  color: #fff;
  font-size: 0.72rem;
  white-space: nowrap;
}

.check__badge--bad {
  background: var(--danger, #b42318);
}

.check__badge--idle {
  background: var(--surface-sunken);
  color: var(--text-faint);
}

.check__hint {
  margin: 6px 0 0;
  color: var(--text-muted);
  font-size: 0.76rem;
  line-height: 1.6;
}

.check__body {
  margin-top: 12px;
}

.check__note {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.check__stats {
  margin: 0 0 12px;
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.7;
}

.check__aside {
  color: var(--text-faint);
  font-size: 0.72rem;
}

.check__total--bad {
  color: var(--danger, #b42318);
}

.check__empty {
  padding: 20px 0;
  color: var(--text-faint);
  font-size: 0.85rem;
  text-align: center;
}

.conflict-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.conflict {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--danger, #b42318);
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
  background: var(--primary-soft);
  color: var(--text-muted);
  font-size: 0.7rem;
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
</style>
