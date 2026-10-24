<script setup lang="ts">
/**
 * 配额与成本（**只读**）。
 *
 * ⚠️ 这个页面**不新开一套计量**：token 是 `app/core/limits.py` 在回答结束时写进 Redis 的
 *    （`gq:quota:{user_id}:{日期}`），这里只读它。页面自己再算一遍 = 迟早两边数字不一致。
 *
 * ⚠️ 三条「不许裸数字」的落法（与评测报告页、运维看板同一条规矩）：
 *    · 每个用量都带**分母**（`12,345 / 200,000`），不单摆一个数；
 *    · 整屏带**生成时间**与**统计口径**（只有问答的 token、只有今天、北京时间切日）；
 *    · 取不到时 `null` → 显示 `—`，**不显示 0** ——
 *      "不知道花了多少"和"没花"混起来，会让人以为额度还很足。
 *
 * ⚠️ 成本是**估算**：单价从配置来（`LLM_PRICE_PER_MILLION_TOKENS`），没配就不显示数字。
 *    显示时一定把单价一起印出来 —— 一个不带单价的金额是不可解释的。
 */

import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { ApiError } from '@/api/client'
import { getMyUsage, getTenantUsage } from '@/api/usage'
import { currentUser } from '@/stores/auth'
import type { TenantUsageOut, UsageOut } from '@/types/api'

const usage = ref<UsageOut | null>(null)
const tenant = ref<TenantUsageOut | null>(null)
const loading = ref(true)
const error = ref('')

/** ⚠️ 函数式写法（AGENTS §10：裸标识符当整个 `v-if` 条件时会被编译器折叠）。 */
function isAdmin(): boolean {
  return currentUser.value?.role === 'admin'
}

/** 用量占配额的比例（0~1）。`tokens_used` 为 `null`（取不到）时返回 `null`。 */
const ratio = computed<number | null>(() => {
  const data = usage.value
  if (data === null || data.tokens_used === null || data.quota_limit <= 0) return null
  return Math.min(1, data.tokens_used / data.quota_limit)
})

/** 进度条颜色：>90% 才变黄（它是熔断线附近），其余都用主色。 */
const barLevel = computed<string>(() => {
  const value = ratio.value
  if (value === null) return 'none'
  if (value >= 0.9) return 'danger'
  if (value >= 0.7) return 'warn'
  return 'ok'
})

function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('zh-CN')
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `¥${value.toFixed(4)}`
}

function percentOf(used: number | null, limit: number): string {
  if (used === null || limit <= 0) return '—'
  return `${((used / limit) * 100).toFixed(1)}%`
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    usage.value = await getMyUsage()
    // 管理员多拿一份租户视图；**失败不影响自己的用量卡**（别让一块坏了整页红）
    tenant.value = isAdmin() ? await getTenantUsage() : null
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
    <AppHeader title="配额与成本" hint="只读 · 按北京时间自然日" tone="green">
      <button class="btn btn--ghost btn--sm" type="button" :disabled="loading" @click="load">
        刷新
      </button>
    </AppHeader>

    <main class="body">
      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <!-- 骨架屏：一张用量卡 + 一张成员表 -->
      <div v-if="loading" class="sk-stack" aria-busy="true" aria-label="加载中">
        <div class="sk sk--block" style="height: 150px"></div>
        <div class="sk sk--block" style="height: 120px"></div>
      </div>

      <template v-else-if="usage">
        <p class="note">
          {{ usage.note }} · 这一屏生成于 {{ usage.generated_at_display }}（北京时间）
        </p>

        <!-- 今天 / 配额 -->
        <section class="card">
          <header class="card__head">
            <h3 class="card__title">我今天的问答消耗</h3>
            <span class="stamp">{{ usage.date }}</span>
          </header>

          <p class="figure">
            <span class="figure__main">{{ num(usage.tokens_used) }}</span>
            <span class="figure__den"> / {{ num(usage.quota_limit) }} token</span>
          </p>

          <div class="bar" :class="`bar--${barLevel}`">
            <div class="bar__fill" :style="{ width: `${(ratio ?? 0) * 100}%` }"></div>
          </div>

          <dl class="rows">
            <div class="row">
              <dt>剩余</dt>
              <dd>{{ num(usage.tokens_remaining) }}</dd>
            </div>
            <div class="row">
              <dt>本分钟的问答次数</dt>
              <dd>{{ num(usage.requests_this_minute) }} / {{ num(usage.rate_limit_per_minute) }}</dd>
            </div>
            <div class="row">
              <dt>额度重置</dt>
              <dd>{{ usage.reset_at_display }}</dd>
            </div>
            <div class="row">
              <dt>估算成本</dt>
              <dd v-if="usage.cost_estimate_cny !== null">
                {{ money(usage.cost_estimate_cny) }}
                <span class="aside">
                  （按 ¥{{ usage.price_per_million_tokens }} / 百万 token 估算）
                </span>
              </dd>
              <dd v-else class="aside">未配单价 —— 只看 token，不折算成钱</dd>
            </div>
          </dl>

          <p v-if="usage.tokens_used === null" class="alert alert--warn alert--inline">
            取不到用量（Redis 不可用）。这是个 `—`，<b>不是 0</b> —— 额度到底还剩多少，现在不知道。
          </p>
        </section>

        <!-- 租户视图：只有管理员有 -->
        <section v-if="tenant" class="tenant">
          <header class="tenant__head">
            <h3 class="tenant__title">本租户成员（{{ tenant.members }} 人）</h3>
            <span class="stamp">
              合计 {{ num(tenant.tokens_used_total) }} token · 生成于
              {{ tenant.generated_at_display }}
            </span>
          </header>

          <table class="table">
            <thead>
              <tr>
                <th>成员</th>
                <th>角色</th>
                <th class="right">今日 token</th>
                <th class="right">占个人配额</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in tenant.items" :key="item.email">
                <td class="email">{{ item.email }}</td>
                <td>{{ item.role === 'admin' ? '管理员' : '成员' }}</td>
                <td class="right mono">{{ num(item.tokens_used) }}</td>
                <td class="right mono">{{ percentOf(item.tokens_used, item.quota_limit) }}</td>
              </tr>
            </tbody>
          </table>

          <p class="foot">
            ⚠️ 这是<b>租户内</b>的视图：你看不到别的租户的人。用量取不到的成员显示 `—`，并排在最后。
          </p>
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
  max-width: 860px;
  margin: 0 auto;
  padding: 28px 20px 60px;
}

.note {
  margin: 0 0 18px;
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.7;
}

.stamp {
  color: var(--text-faint);
  font-size: 0.72rem;
}

.figure {
  margin: 10px 0 10px;
  font-family: var(--mono, ui-monospace, monospace);
  font-variant-numeric: tabular-nums;
}

.figure__main {
  font-size: 1.7rem;
}

/* 分母必须同样显眼 —— 单摆一个 12,345 读者不知道它是多是少 */
.figure__den {
  color: var(--text-faint);
  font-size: 0.95rem;
}

.bar {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--primary-soft);
}

.bar__fill {
  height: 100%;
  border-radius: 999px;
  background: var(--primary);
  transition: width 0.25s ease;
}

.bar--warn .bar__fill {
  background: var(--warn);
}

.bar--danger .bar__fill {
  background: var(--danger);
}

.rows {
  display: grid;
  gap: 6px;
  margin: 14px 0 0;
}

.row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.row dt {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.row dd {
  margin: 0;
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 0.82rem;
  font-variant-numeric: tabular-nums;
}

.aside {
  color: var(--text-faint);
  font-family: inherit;
  font-size: 0.72rem;
}

.alert--inline {
  margin: 14px 0 0;
  font-size: 0.78rem;
}

.tenant {
  margin-top: 28px;
}

.tenant__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.tenant__title {
  margin: 0;
  font-size: 0.9rem;
}

/* 表格密度统一走全局 `.table`（`main.css`） */
.right {
  text-align: right;
}

.mono {
  font-family: var(--mono, ui-monospace, monospace);
  font-variant-numeric: tabular-nums;
}

.email {
  color: var(--text-muted);
}

.foot {
  margin: 10px 0 0;
  color: var(--text-faint);
  font-size: 0.72rem;
  line-height: 1.6;
}
</style>
