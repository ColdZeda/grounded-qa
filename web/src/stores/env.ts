/**
 * 当前连的是哪套后端（`dev` / `prod`），用于顶栏环境角标。
 *
 * ⚠️ **刻意做成"读一次、全局共享"**：环境在进程生命周期内不会变，
 *    没必要每次渲染都请求。失败就保持 `unknown`（**不显示角标**），
 *    而不是猜一个 —— 猜错比不显示更坏：那正是这个功能要解决的事。
 */

import { ref } from 'vue'

import { fetchHealth, type RuntimeEnv } from '@/api/health'

export const runtimeEnv = ref<RuntimeEnv>('unknown')

/** 启动时调一次（`main.ts`）。**失败静默**：角标是辅助信息，不该拦住页面。 */
export async function loadRuntimeEnv(): Promise<void> {
  try {
    runtimeEnv.value = (await fetchHealth()).env
  } catch {
    runtimeEnv.value = 'unknown'
  }
}
