<script setup lang="ts">
import { ref } from 'vue'

/**
 * 骨架阶段的占位页。
 *
 * 它唯一的作用是**验证前后端链路通**：点一下按钮打 /healthz
 * （开发时走 vite proxy，服务器上走 nginx 反代）。
 * 真正的客服工作台 / 监控看板在 v1.0 / v1.1 补。
 */
const health = ref('未检测')
const checking = ref(false)

async function checkHealth(): Promise<void> {
  checking.value = true
  try {
    const resp = await fetch('/healthz')
    if (!resp.ok) {
      health.value = `后端异常：HTTP ${resp.status}`
      return
    }
    const body = (await resp.json()) as { status: string; version: string }
    health.value = `后端正常 · ${body.status} · v${body.version}`
  } catch (err) {
    health.value = `连不上后端：${(err as Error).message}`
  } finally {
    checking.value = false
  }
}
</script>

<template>
  <main class="shell">
    <h1>grounded-qa</h1>
    <p class="tagline">多租户知识库问答平台 · 客服是第一个业务场景</p>

    <section class="probe">
      <button :disabled="checking" @click="checkHealth">
        {{ checking ? '检测中…' : '检测后端连通性' }}
      </button>
      <output>{{ health }}</output>
    </section>

    <p class="note">
      前端骨架已就位。客服工作台（v1.0）与监控看板（v1.1）待实现。
    </p>
  </main>
</template>

<style scoped>
.shell {
  max-width: 640px;
  margin: 12vh auto;
  padding: 0 24px;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: #1f2933;
}

h1 {
  margin: 0;
  font-size: 2rem;
  letter-spacing: 0.02em;
}

.tagline {
  margin: 8px 0 32px;
  color: #6b7280;
}

.probe {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 20px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fafafa;
}

button {
  padding: 8px 16px;
  border: 1px solid #2563eb;
  border-radius: 6px;
  background: #2563eb;
  color: #fff;
  font-size: 0.95rem;
  cursor: pointer;
}

button:disabled {
  opacity: 0.6;
  cursor: progress;
}

output {
  font-family: ui-monospace, 'Cascadia Code', monospace;
  font-size: 0.9rem;
}

.note {
  margin-top: 32px;
  color: #9ca3af;
  font-size: 0.875rem;
}
</style>
