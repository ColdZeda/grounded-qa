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

/**
 * 哪一组「压成一行」渲染（2026-10-25 走查）。
 *
 * ⚠️ **只在页面上合并，后端仍然是"一个面板 = 一个指标"**：四个数的单位（%、MB、GB、负载）、
 *    口径、告警阈值各不相同，合成一条 PromQL 会让它们全丢掉自己的单位与阈值。
 *    合并解决的是"四张卡占了两行、信息密度反而低"这件事。
 */
const COMPACT_GROUPS = new Set(['资源'])

function isCompact(group: Group): boolean {
  return COMPACT_GROUPS.has(group.name)
}

/** 一行里显示的值：没有样本是 `—`，多条序列用 `·` 并列（资源组其实都是单条）。 */
function statValue(panel: MonitorPanel): string {
  if (panel.items.length === 0) return '—'
  if (panel.items.length === 1) return fmt(panel, panel.items[0])
  return panel.items.map((item) => fmt(panel, item)).join(' · ')
}

/** 布尔型面板 = "抓得到/抓不到"，它不该显示 1/0，而是 ✓ / ✗。 */
function isBoolPanel(panel: MonitorPanel): boolean {
  return panel.unit === 'bool'
}

function isOk(panel: MonitorPanel): boolean {
  return (panel.items[0]?.value ?? 0) >= 1
}

function boolHint(panel: MonitorPanel): string {
  return isOk(panel)
    ? 'Prometheus 能抓到 /metrics（容器在跑、端口对）'
    : '抓不到 /metrics —— 容器挂了、端口不对，或 Prometheus 自己没在跑'
}

/**
 * 「24 小时拒答率」的**样本量**（2026-10-25 走查）。
 *
 * ⚠️ 没有它，只有 1 次请求时页面会显示「拒答率 100%」—— 那既不是"系统坏了"，
 *    也不是"拒答很准"，只是**分母是 1**。比率必须和分母一起出现（本项目一贯的"不许裸数字"）。
 * 算法：把「24 小时问答结果」里各 outcome 的计数加起来（answered/refused/error）。
 */
const refusalSample = computed<number | null>(() => {
  const panel = snapshot.value?.panels.find((item) => item.title === '24 小时问答结果')
  if (!panel || panel.items.length === 0) return null
  const total = panel.items.reduce((sum, item) => sum + (item.value ?? 0), 0)
  // ⚠️ **必须取整**：这几个计数来自 Prometheus 的 `increase()`，它按抓取点数外推 ⟹ 原始值是
  //    小数（实测显示过「样本 8.0273096922373 次」）。样本量是"几次"，不存在 0.27 次。
  return Math.round(total)
})

/** 把「单位」翻成给人看的一串字。`null` 一律 `—`。 */
function fmt(panel: MonitorPanel, item: MonitorItem | undefined): string {
  const value = item?.value
  if (value === null || value === undefined) return '—'
  switch (panel.unit) {
    case 'bool':
      // 兜底文字：真正的显示是 ✓ / ✗ 图标（见模板里的 `bool` 分支）
      return value >= 1 ? '✓' : '✗'
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
      return `${Math.round(value).toLocaleString('zh-CN')} 块`
    // 负载没有单位，但**必须给小数**：0.09 显示成 "0" 会让人以为机器完全空闲
    case 'load':
      return num(value, 2)
    default:
      return Math.round(value).toLocaleString('zh-CN')
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
    <AppHeader title="运维看板" hint="只读 · 数据来自 Prometheus" tone="slate">
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p class="hint">
        这一屏是<b>打开页面那一刻</b>去问 Prometheus 的结果，不会自动刷新（点右上角「刷新」）。
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
            这通常是「监控还没启起来」或「容器刚重启」。<b>业务本身不受影响</b> ——
            看板是旁路，它挂了不影响问答。
          </span>
        </p>

        <section v-for="group in groups" :key="group.name" class="group">
          <h3 class="group__title">{{ group.name }}</h3>

          <!--
            资源组：**四个数一行**（CPU / 可用内存 / 根分区 / 负载）。
            每格仍保留自己的单位与口径（悬停见 `panel.hint`）—— 见 `COMPACT_GROUPS` 的注释。
          -->
          <div v-if="isCompact(group)" class="stats">
            <article
              v-for="panel in group.panels"
              :key="panel.key"
              class="stats__item"
              :title="panel.hint"
            >
              <span class="stats__label">{{ panel.title }}</span>
              <span class="stats__value">{{ statValue(panel) }}</span>
              <span v-if="isStale(panel)" class="stale">样本是 {{ ageOf(panel) }}的</span>
            </article>
            <p class="stats__hint">主机级指标 · Prometheus 每 30 秒采一次</p>
          </div>

          <div v-else class="cards">
            <article v-for="panel in group.panels" :key="panel.key" class="card">
              <header class="card__head">
                <h4 class="card__title">{{ panel.title }}</h4>
                <span v-if="isStale(panel)" class="stale">样本是 {{ ageOf(panel) }}的</span>
              </header>

              <!-- 布尔面板：不显示 1/0，显示 ✓ / ✗（悬停与读屏文字给完整解释） -->
              <p v-if="isBoolPanel(panel)" class="value">
                <span
                  class="bool"
                  :class="isOk(panel) ? 'bool--ok' : 'bool--bad'"
                  role="img"
                  :aria-label="boolHint(panel)"
                  :title="boolHint(panel)"
                >
                  {{ isOk(panel) ? '✓' : '✗' }}
                </span>
              </p>
              <p v-else-if="panel.items.length === 0" class="value value--none" title="没有采到样本">
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
              <!-- 比率没有分母就没有意义：样本只有 1 的时候，「拒答率 100%」什么都不说明 -->
              <p v-if="panel.title === '24 小时拒答率' && refusalSample !== null" class="card__sample">
                样本 {{ refusalSample }} 次 —— 少于几十次时，这个百分比基本没意义
              </p>
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
  /* 190px 是「五块卡片排一行」的下限：卡里最长的东西是那行注释，
     再窄就会把 "0 = 抓不到" 这类句子拆得太碎。 */
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
}

/* ---------- 压成一行的资源组 ----------
   ⚠️ 四个数一行，**不是四张卡挤一行**：每格只有"标题 + 数"，口径收进悬停。
   窄屏时自动折成两列（`auto-fit`），不会把数字挤成竖排。 */
.stats {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
}

.stats__item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  cursor: help;
}

.stats__label {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.stats__value {
  color: var(--text);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 1.05rem;
  font-variant-numeric: tabular-nums;
}

.stats__hint {
  margin: 2px 0 0;
  color: var(--text-faint);
  font-size: 0.72rem;
  grid-column: 1 / -1;
}

/* ---------- 布尔状态：✓ / ✗ ----------
   ⚠️ 图标之外还有 `title` + `aria-label`（`boolHint()`）：
      只靠一个符号，读屏用户和色弱用户都拿不到"这是好还是坏"。 */
.bool {
  font-size: 1.5rem;
  font-weight: 700;
  line-height: 1.2;
}

.bool--ok {
  color: var(--ok);
}

.bool--bad {
  color: var(--danger);
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

/* 样本量：比 hint 更靠上、更显眼一点 —— 它是判读这个比率的前提 */
.card__sample {
  margin: 6px 0 0;
  color: var(--warn);
  font-size: 0.72rem;
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
