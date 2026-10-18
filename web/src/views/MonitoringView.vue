<script setup lang="ts">
/**
 * 运维看板（**只读**，数据来自 Prometheus）。
 *
 * ⚠️ 为什么自己画一页，而不是上 Grafana：见 `docs/architecture.md` §10「不做清单」——
 *    2C2G 上再塞一个 Grafana 是**多一个要 OOM 的进程**，而我们要看的只有十几条曲线。
 *    代价说清楚：**没有告警静默、没有历史回看、没有现成的图** —— 只有"现在是多少"。
 *
 * ⚠️ **不许裸数字**（与评测报告页同一条规矩）：
 *    · 每块卡片都带 `hint`（口径：窗口多长、是分桶估算还是精确值、分母是什么）
 *    · 整屏带**生成时间**和**问的是哪台 Prometheus**
 *    · 样本比 `stale_after_s` 还旧就标黄 —— 一个十分钟前的数字看起来和一个十秒前的
 *      一模一样，而它俩的可信度完全不同
 *    · 取不到数据时 `value` 是 `null` ⟹ 显示 `—`，**不显示 0**
 *
 * ⚠️ 页面**不自动刷新**：这是运维面，不是仪表盘墙。自动刷新会让人盯着一个
 *    30 秒才更新一次的数，误以为是实时。
 */

import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { getMonitorSnapshot } from '@/api/monitoring'
import type { MonitorItem, MonitorPanel, MonitorSnapshot } from '@/types/api'

const snapshot = ref<MonitorSnapshot | null>(null)
const loading = ref(true)
const error = ref('')
/** 「现在」的秒。**打开时就定住**，不跟着表走 —— 陈旧标记要相对"这一屏"算。 */
const nowS = ref(Date.now() / 1000)

interface Group {
  name: string
  panels: MonitorPanel[]
}

/** 按后端给的顺序分组（不用 Map：顺序就是"先看什么"的信息）。 */
const groups = computed<Group[]>(() => {
  const out: Group[] = []
  for (const panel of snapshot.value?.panels ?? []) {
    let group = out.find((item) => item.name === panel.group)
    if (group === undefined) {
      group = { name: panel.group, panels: [] }
      out.push(group)
    }
    group.panels.push(panel)
  }
  return out
})

function num(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(digits)
}

/** 把「单位」翻成给人看的一串字。`null` 一律 `—`。 */
function fmt(panel: MonitorPanel, item: MonitorItem | undefined): string {
  const value = item?.value
  if (value === null || value === undefined) return '—'
  switch (panel.unit) {
    case 'bool':
      return value >= 1 ? '抓得到' : '抓不到'
    case 'ratio':
      return `${num(value * 100)}%`
    case 'pct':
      return `${num(value)}%`
    case 's':
      // 亚秒的用毫秒更直观；P95 在 1 秒上下是最常看的那一格
      return value < 1 ? `${Math.round(value * 1000)} ms` : `${num(value, 2)} s`
    case 'req_s':
      return `${num(value, 2)} /s`
    case 'mb':
      return `${num(value, 0)} MB`
    case 'gb':
      return `${num(value)} GB`
    case 'chunks':
      return `${num(value, 0)} 块`
    default:
      return num(value, 0)
  }
}

/** `outcome=answered` → `answered`（标签键对读的人没用，值才有）。 */
function shortLabel(label: string): string {
  if (!label) return '合计'
  return label.split('=').pop() ?? label
}

/** 这块卡片里**最新**的样本时间；一个都没有就是 `null`。 */
function newestTs(panel: MonitorPanel): number | null {
  const stamps = panel.items.map((item) => item.ts).filter((ts): ts is number => ts !== null)
  return stamps.length === 0 ? null : Math.max(...stamps)
}

function isStale(panel: MonitorPanel): boolean {
  const ts = newestTs(panel)
  if (ts === null || snapshot.value === null) return false
  return nowS.value - ts > snapshot.value.stale_after_s
}

/** 样本有多旧，说成人话。 */
function ageOf(panel: MonitorPanel): string {
  const ts = newestTs(panel)
  if (ts === null) return ''
  const seconds = Math.max(0, Math.round(nowS.value - ts))
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`
  return `${(seconds / 3600).toFixed(1)} 小时前`
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    snapshot.value = await getMonitorSnapshot()
    nowS.value = Date.now() / 1000
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
      <h2 class="title">运维看板 <span class="count">只读 · 数据来自 Prometheus</span></h2>

      <p class="hint">
        这一屏是**打开页面那一刻**去问 Prometheus 的结果，不会自动刷新（点右上角「刷新」）。
        Prometheus 每 30 秒抓一次指标，所以最细的颗粒就是 30 秒 —— 它不是实时监控。
      </p>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>
      <p v-if="loading" class="muted">正在问 Prometheus…</p>

      <template v-else-if="snapshot">
        <p class="note">
          问的是 <code>{{ snapshot.prometheus_url }}</code> · 这一屏生成于
          {{ snapshot.generated_at_display }}（北京时间）· 样本超过
          {{ snapshot.stale_after_s }} 秒算陈旧
        </p>

        <p v-if="!snapshot.available" class="alert alert--warn">
          {{ snapshot.error }}<br />
          <span class="alert__sub">
            这通常是「监控还没启起来」或「容器刚重启」。**业务本身不受影响** ——
            看板是旁路，它挂了不影响问答。
          </span>
        </p>

        <section v-for="group in groups" :key="group.name" class="group">
          <h3 class="group__title">{{ group.name }}</h3>
          <div class="cards">
            <article v-for="panel in group.panels" :key="panel.key" class="card">
              <header class="card__head">
                <h4 class="card__title">{{ panel.title }}</h4>
                <span v-if="isStale(panel)" class="stale">样本是 {{ ageOf(panel) }}的</span>
              </header>

              <p v-if="panel.items.length === 0" class="value value--none" title="没有采到样本">
                —
              </p>
              <p v-else-if="panel.items.length === 1" class="value">
                {{ fmt(panel, panel.items[0]) }}
              </p>
              <ul v-else class="series">
                <li v-for="item in panel.items" :key="item.label" class="series__row">
                  <span class="series__label">{{ shortLabel(item.label) }}</span>
                  <span class="series__value">{{ fmt(panel, item) }}</span>
                </li>
              </ul>

              <p class="card__hint">{{ panel.hint }}</p>
            </article>
          </div>
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
  max-width: 1080px;
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

.note {
  margin: 0 0 20px;
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.7;
}

.note code {
  font-size: 0.76rem;
}

.alert__sub {
  font-size: 0.78rem;
  line-height: 1.6;
}

.muted {
  color: var(--text-muted);
}

.group {
  margin-bottom: 26px;
}

.group__title {
  margin: 0 0 10px;
  color: var(--text-faint);
  font-size: 0.78rem;
  font-weight: 500;
  letter-spacing: 0.04em;
}

.cards {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.card {
  display: flex;
  flex-direction: column;
  padding: 14px 16px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.card__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.card__title {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 500;
}

/* 陈旧标记必须显眼 —— 十分钟前的数和十秒前的数长得一模一样 */
.stale {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--warn-soft);
  color: var(--warn);
  font-size: 0.68rem;
}

.value {
  margin: 0 0 8px;
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 1.45rem;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}

.value--none {
  color: var(--text-faint);
}

.series {
  margin: 0 0 8px;
  padding: 0;
  list-style: none;
}

.series__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 2px 0;
}

.series__label {
  color: var(--text-faint);
  font-size: 0.75rem;
}

.series__value {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.95rem;
  font-variant-numeric: tabular-nums;
}

/* 口径说明：小、灰，但**不许省略** */
.card__hint {
  margin: auto 0 0;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
  color: var(--text-faint);
  font-size: 0.72rem;
  line-height: 1.6;
}
</style>
