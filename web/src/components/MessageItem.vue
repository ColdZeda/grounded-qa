<script setup lang="ts">
/**
 * 一条消息。
 *
 * 三件事是这个组件存在的理由，缺一个"引用溯源"就只是句口号：
 *
 *   1. **正文里的 `[n]` 要能点** —— 点了高亮下面第 n 条来源。
 *      不然用户看到 `[2]` 还得自己数。
 *   2. **拒答要显示得像个结论**，不能和普通回答长一样 ——
 *      拒答是这个系统的**特性**（宁可说不知道，也不编），藏起来就等于没做。
 *   3. **来源要连"分数口径"一起显示**（见 `CitationList.vue` 的注释）。
 */

import { computed, ref } from 'vue'

import CitationList from './CitationList.vue'
import { isDetailed } from '@/stores/prefs'
import type { UiMessage } from '@/types/ui'

const props = defineProps<{ message: UiMessage }>()

const activeIndex = ref<number | null>(null)

const isUser = computed(() => props.message.role === 'user')

const time = computed(() => {
  const date = new Date(props.message.createdAt)
  if (Number.isNaN(date.getTime())) return ''
  // ⚠️ 后端存的是 `timestamptz`，序列化出来带时区偏移，所以 `new Date()` 解析正确，
  //    `toLocaleTimeString` 会自动折算到**浏览器所在时区**。手写解析字符串会踩时区的坑。
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
})

/**
 * 正文切段。
 *
 * ⚠️ 用 `{ text, citeIndex }` 这种**扁平结构**而不是可辨识联合
 *    （`{kind:'text'} | {kind:'cite'}`）：联合类型在 Vue 模板里
 *    靠 `v-if` 做窄化并不可靠，vue-tsc 经常推不出来。
 */
interface Segment {
  text: string
  citeIndex: number | null
}

const segments = computed<Segment[]>(() => {
  const content = props.message.content
  if (!content) return []

  const valid = new Set(props.message.citations.map((cite) => cite.index))
  if (valid.size === 0) return [{ text: content, citeIndex: null }]

  const out: Segment[] = []
  const pattern = /\[(\d{1,2})\]/g
  let cursor = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(content)) !== null) {
    const index = Number(match[1])
    // ⚠️ 只把**确实存在于来源列表里**的 [n] 变成按钮。
    //    否则正文里正常的方括号（`[2024]`、或者一个没有引用却写了 `[1]` 的回答）
    //    会变成点了没反应的按钮 —— 比不渲染更让人困惑。
    if (!valid.has(index)) continue

    if (match.index > cursor) {
      out.push({ text: content.slice(cursor, match.index), citeIndex: null })
    }
    out.push({ text: '', citeIndex: index })
    cursor = match.index + match[0].length
  }

  if (cursor < content.length) out.push({ text: content.slice(cursor), citeIndex: null })
  return out
})
</script>

<template>
  <article class="msg" :class="isUser ? 'msg--user' : 'msg--bot'">
    <div class="meta">
      <span class="who">{{ isUser ? '我' : '助手' }}</span>
      <span class="time">{{ time }}</span>

      <span v-if="message.latencyMs !== null && !isUser" class="chip">
        {{ message.latencyMs }} ms
      </span>
      <!--
        置信度只在**详细**档显示 —— 它是最典型的"实现细节"（architecture.md §9.19）。
        ⚠️ `isDetailed()` 是**函数调用**：裸标识符会被编译器当常量折叠（见 prefs.ts）。
      -->
      <span
        v-if="message.confidence !== null && !isUser && isDetailed()"
        class="chip"
        title="置信度取自精排分（或退化时的向量余弦）。绝不用 RRF 分数——RRF 是名次的函数，库里只有一条不相关内容时它照样排第一。"
      >
        置信度 {{ message.confidence.toFixed(3) }}
      </span>
    </div>

    <!-- 拒答横幅：**放在正文上面**，先看到结论再看到文案 -->
    <p v-if="message.refused" class="alert alert--warn banner">
      已拒答 —— 检索到的依据不足以支撑回答。这是刻意的：宁可说不知道，也不编。
    </p>

    <div v-if="message.content" class="bubble">
      <template v-for="(seg, i) in segments" :key="i">
        <button
          v-if="seg.citeIndex !== null"
          type="button"
          class="marker"
          :class="{ 'marker--active': seg.citeIndex === activeIndex }"
          :title="`跳到来源 [${seg.citeIndex}]`"
          @click="activeIndex = activeIndex === seg.citeIndex ? null : seg.citeIndex"
        >
          [{{ seg.citeIndex }}]
        </button>
        <span v-else>{{ seg.text }}</span>
      </template>
      <!-- 流式光标：让人一眼看出"还在写"，而不是"卡住了" -->
      <span v-if="message.streaming" class="caret" aria-hidden="true"></span>
    </div>

    <p v-else-if="!message.streaming && !message.error" class="bubble bubble--empty">（空回答）</p>

    <p v-if="message.error" class="alert alert--danger banner">
      <template v-if="message.cancelled">已停止生成。</template>
      <template v-else>生成出错：{{ message.error }}</template>
      <template v-if="message.content"> 上面的内容是已经生成的部分，不是完整回答。</template>
    </p>

    <section v-if="message.citations.length > 0" class="sources">
      <h4 class="sources__title">参考来源 · {{ message.citations.length }} 条</h4>
      <CitationList :citations="message.citations" :active-index="activeIndex" />
    </section>
  </article>
</template>

<style scoped>
.msg {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 780px;
}

.msg--user {
  margin-left: auto;
  align-items: flex-end;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-faint);
  font-size: 0.75rem;
}

.who {
  color: var(--text-muted);
  font-weight: 600;
}

.bubble {
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
  /* ⚠️ `pre-wrap` 是必须的：回答里的换行要保留，
     否则模型精心排的列表会挤成一坨。 */
  white-space: pre-wrap;
  word-break: break-word;
}

.msg--user .bubble {
  border-color: transparent;
  background: var(--primary);
  color: #fff;
}

.bubble--empty {
  color: var(--text-faint);
}

.banner {
  margin: 0;
}

.sources {
  margin-top: 4px;
}

.sources__title {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
}

/* 正文里的 [n] */
.marker {
  padding: 0 3px;
  border: none;
  border-radius: 4px;
  background: var(--primary-soft);
  color: var(--primary);
  font-family: var(--mono);
  font-size: 0.8em;
  vertical-align: 1px;
}

.marker:hover,
.marker--active {
  background: var(--primary);
  color: #fff;
}

.caret {
  display: inline-block;
  width: 7px;
  height: 1em;
  margin-left: 2px;
  background: var(--primary);
  vertical-align: -2px;
  animation: blink 1s steps(2, start) infinite;
}

@keyframes blink {
  to {
    visibility: hidden;
  }
}
</style>
