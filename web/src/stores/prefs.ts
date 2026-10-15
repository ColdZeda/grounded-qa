/**
 * 界面偏好 —— **只有一样东西**：详细程度档位。
 *
 * ⚠️ 刻意不做成"设置中心"：一个开关不需要一套框架。
 *    真到了要存十来个偏好的那天，再考虑换 Pinia 或加 schema（现在加了是净增）。
 *
 * 两档（设计理由见 `architecture.md` §9.19）：
 *
 * | 档位 | 给谁 | 来源 | 分数（`精排分 0.466`） |
 * |---|---|---|---|
 * | `detailed` | 开发 / 评测 / **面试官** | ✅ | ✅ |
 * | `concise`  | 终端用户 | ✅ | ❌ |
 *
 * ⚠️ **对终端用户要藏的不是"来源"，而是"分数"。**
 *    「这条答案来自《退款政策》第 3 条」是**建立信任**的东西，不该藏；
 *    「精排分 0.466」才是实现细节 —— 客户看不懂，也没必要看。
 *
 * ⚠️ 而且这只是**显示层**的开关。**数据层永远存全**（`citations` ＋ `confidence`），
 *    否则"刷新之后信息就断了"，badcase 复盘根本无从谈起。
 */

import { ref } from 'vue'

export type DetailLevel = 'detailed' | 'concise'

const STORAGE_KEY = 'gq.detail_level'

/** 默认**详细** —— 这是求职作品集，展示分数与口径恰恰是卖点。 */
const DEFAULT_LEVEL: DetailLevel = 'detailed'

function readStored(): DetailLevel {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'concise' ? 'concise' : DEFAULT_LEVEL
  } catch {
    // 隐私模式 / 禁用存储时 localStorage 会直接抛异常，不是返回 null
    return DEFAULT_LEVEL
  }
}

export const detailLevel = ref<DetailLevel>(readStored())

/**
 * 当前是不是"详细"档。
 *
 * ⚠️⚠️ **这里刻意导出的是「函数」，不是 `computed`。踩过坑：**
 *
 *     如果写成 `export const isDetailed = computed(...)`，然后在模板里写
 *     `v-if="isDetailed"`，`@vue/compiler-sfc` 会把**裸标识符**当成常量表达式
 *     **直接折叠掉** —— 编译产物里那个 `v-if` **整段消失**
 *     （2026-10-15 实测：模块里只剩一句 import，既没有 `_unref(isDetailed)`，
 *     也没有 `_ctx.isDetailed`）。
 *
 *     症状是：**点了"简洁"档，分数照样显示，而且控制台不报任何错。**
 *     （同样的 `detailLevel` 写在表达式里 —— 如 `detailLevel === 'x'` —— 就没问题，
 *     所以这个坑只在"裸标识符当整个条件"时出现，非常难联想。）
 *
 *     写成**函数调用** `isDetailed()` 就没有这个歧义：调用表达式不会被当成常量。
 */
export function isDetailed(): boolean {
  return detailLevel.value === 'detailed'
}

export function setDetailLevel(level: DetailLevel): void {
  detailLevel.value = level
  try {
    window.localStorage.setItem(STORAGE_KEY, level)
  } catch {
    // 存不进去就退化成"只在本次会话生效"，不影响功能
  }
}
