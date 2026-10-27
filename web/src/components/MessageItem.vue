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
import { submitFeedback, type FeedbackVerdict } from '@/api/feedback'
import ImageThumb from './ImageThumb.vue'
import { renderAnswer } from '@/lib/markdown'
import { isDetailed } from '@/stores/prefs'
import type { UiMessage } from '@/types/ui'

const props = defineProps<{
  message: UiMessage
  /**
   * 这条消息所在会话的 id。
   *
   * ⚠️ **只为"上报错误的兜底"存在**（2026-10-28）：本页刚生成的回答 id 是 `local-N`，
   *    不是 UUID，直接提交会被后端拒 ⟹ 拿到会话 id 才能回去查出真 id。
   */
  conversationId?: string
}>()

/**
 * 纠错反馈的本地状态。
 *
 * ⚠️ **不做点赞**：点踩只产生一个数字，对治理没用。这里要的是"哪里不对 + 当时的答案"，
 *    所以点开是个三选一 + 一句话，而不是一个大拇指朝下的图标。
 */
const feedbackOpen = ref(false)
const feedbackDone = ref(false)
const feedbackBusy = ref(false)
const feedbackVerdict = ref<FeedbackVerdict | null>(null)
const feedbackNote = ref('')
const feedbackError = ref('')

const canFeedback = computed(
  () => props.message.role === 'assistant' && !props.message.streaming && !props.message.error,
)

/**
 * 参考来源：**默认折叠 + 最多先给 5 条**（2026-10-27 走查第 6 条）。
 *
 * ⚠️ 为什么不直接全列：`rerank_top_n` 是 5，但 agent 多次检索后引用会更多，
 *    一次铺开会把答案挤到屏幕外。先给 5 条，想看全的点一下 —— **信息不删，只是分层**。
 */
const SOURCE_PREVIEW = 5
const showAllSources = ref(false)
const visibleCitations = computed(() =>
  showAllSources.value
    ? props.message.citations
    : props.message.citations.slice(0, SOURCE_PREVIEW),
)
const hiddenSourceCount = computed(() =>
  Math.max(props.message.citations.length - SOURCE_PREVIEW, 0),
)

/**
 * 引用可核对性（**内部侧**才显示）。
 *
 * ⚠️ 一句话就够："有几句我们没核对上"。**不说"答案是错的"** —— 两件事不一样，
 *    而且判据刻意宽松，误报是存在的（`app/rag/citations.py`）。
 */
const unsupportedCount = computed(() => props.message.citationAudit?.unsupported ?? 0)
const repairs = computed(() => props.message.citationRepairs ?? [])

async function sendFeedback(): Promise<void> {
  const verdict = feedbackVerdict.value
  if (verdict === null || feedbackBusy.value) return
  if (verdict === 'other' && feedbackNote.value.trim() === '') {
    feedbackError.value = '选「其他」时请写一句说明'
    return
  }
  feedbackBusy.value = true
  feedbackError.value = ''
  try {
    await submitFeedback({
      message_id: props.message.id,
      verdict,
      note: feedbackNote.value.trim() || null,
      // 兜底用：id 是 `local-N` 时，靠"会话 + 答案正文"去换真 id（见 `api/feedback.ts`）
      conversation_id: props.conversationId ?? null,
      answer: props.message.content,
    })
    feedbackDone.value = true
    feedbackOpen.value = false
  } catch (err) {
    // ⚠️ 失败要说出来：坐席以为记下了、其实没记下，是最糟的一种静默
    feedbackError.value = (err as Error).message
  } finally {
    feedbackBusy.value = false
  }
}

/** 降级原因的人话说法（hover 显示）。后端给的是中文短语，这里只补一句“意味着什么”。 */
const degradedTitle = computed(
  () => `这次的回答是回落到固定流水线生成的：${props.message.degraded}`,
)

/**
 * 「看原文」一路往上抛到 `ChatView`。
 * ⚠️ 抽屉只有**一个**（在页面级）：一条消息 10 条来源就挂 10 个抽屉是错的设计，
 *    而且"同时开两个"迟早发生。组件这里只负责转发，不持有抽屉状态。
 */
const emit = defineEmits<{
  (event: 'open-context', payload: { documentId: string; ordinal: number; title: string }): void
}>()

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
 * 助手回答渲染成 HTML（Markdown + `[n]` 按钮）。
 *
 * ⚠️ 只给**助手消息**用。用户自己打的字保持"所见即所得"的纯文本 ——
 *    不然他打一个 `*` 或 `#` 就被排版掉了（而且他打的 `[1]` 也不该变成按钮）。
 *    ⚠️ 安全：`lib/markdown.ts` 里 `html: false`，原始 HTML 被转义后才进 `v-html`。
 */
const validIndexes = computed(() => new Set(props.message.citations.map((cite) => cite.index)))

const bodyHtml = computed(() =>
  renderAnswer(props.message.content, validIndexes.value, activeIndex.value),
)

/**
 * `[n]` 的点击：**事件委托**。
 *
 * ⚠️ `v-html` 出来的 DOM 里挂不了 Vue 的 `@click`，所以由外层容器统一接住，
 *    再顺着 `data-cite` 找是哪个索引。`lib/markdown.ts` 里就是按这个属性拼的按钮。
 */
function onBodyClick(event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  const marker = target?.closest?.('[data-cite]') as HTMLElement | null
  if (!marker) return

  const index = Number(marker.dataset.cite)
  if (!Number.isFinite(index)) return
  activeIndex.value = activeIndex.value === index ? null : index
}

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
      <!--
        短追问改写（v1.4 C1'）：这条提问的**检索**并进了上一轮问句。
        ⚠️ 挂在**用户**这条上，因为被改写的是"这一问的检索"，不是回答本身。
        ⚠️ 只在**详细**档显示：终端用户只打了一句「德国呢」，告诉他"合并了上一轮"
           反而会困惑 —— 这是"我们内部怎么检索的"，和置信度、精排分同一类。
        ⚠️ `isDetailed()` 必须是**函数调用**（见 prefs.ts 与 `architecture.md` §9.20）。
      -->
      <span
        v-if="message.followupPrev && isUser && isDetailed()"
        class="chip chip--merged"
        :title="`检索时并进了上一轮的问句：「${message.followupPrev}」。只影响召回，回答仍是按你这一问生成的。`"
      >
        已合并上一轮检索
      </span>
    </div>

    <!--
      用户发的那张图。
      ⚠️ 放在气泡**上面**（像聊天软件的惯例）：先看图，再看用户配的那句话。
    -->
    <ImageThumb v-if="message.imageId" :image-id="message.imageId" />

    <!-- 拒答横幅：**放在正文上面**，先看到结论再看到文案 -->
    <p v-if="message.refused" class="alert alert--warn banner">
      已拒答 —— 检索到的依据不足以支撑回答。这是刻意的：宁可说不知道，也不编。
    </p>

    <!--
      ⚠️ 读图失败**必须说出来**，而且**任何档位都显示**（`PLAN §8.3` D10）：
         不说的话，用户会以为我们看过这张图了 —— 那就是"静默降级"，
         而这条问答的质量无从解释（"我明明发了图，它怎么没看见"）。
    -->
    <p v-if="message.imageError" class="alert alert--warn banner">
      {{ message.imageError }}
    </p>

    <div v-if="message.content" class="bubble">
      <!--
        用户消息：**纯文本**（所见即所得）。
        助手消息：渲染 Markdown —— 带图提问必然触发结构化长回答，
        不渲染的话 `**加粗**` / `| 表格 |` 会原样露在脸上（2026-10-22 真机看到的就是这样）。
      -->
      <span v-if="isUser">{{ message.content }}</span>
      <div v-else class="md" @click="onBodyClick" v-html="bodyHtml"></div>

      <!-- 流式光标：让人一眼看出"还在写"，而不是"卡住了" -->
      <span v-if="message.streaming" class="caret" aria-hidden="true"></span>
    </div>

    <p v-else-if="!message.streaming && !message.error" class="bubble bubble--empty">（空回答）</p>

    <p v-if="message.error" class="alert alert--danger banner">
      <template v-if="message.cancelled">已停止生成。</template>
      <template v-else>生成出错：{{ message.error }}</template>
      <template v-if="message.content"> 上面的内容是已经生成的部分，不是完整回答。</template>
    </p>

    <!--
      读图读到了什么 —— **只在详细档显示**（和置信度同一类东西：实现细节）。
      ⚠️ 用 `<details>` 折叠：这句最长 300 多字（`memory_line` 里的转写截到 240 字），
         直接铺开会把回答挤到屏幕外面去。
      ⚠️ `isDetailed()` 必须是**函数调用** —— 裸标识符会被编译器当常量折叠（见 prefs.ts）。
    -->
    <details v-if="message.imageNote && isDetailed()" class="image-note">
      <summary>📷 读图结果</summary>
      <p class="image-note__body">{{ message.imageNote }}</p>
    </details>

    <!-- 纠错反馈：报告的入口（内部工作台才有） -->
    <div v-if="canFeedback" class="fb">
      <button
        v-if="!feedbackOpen && !feedbackDone"
        type="button"
        class="fb__btn"
        @click="feedbackOpen = true"
      >
        ⚑ 上报错误
      </button>
      <span v-else-if="feedbackDone" class="fb__done">✓ 已记录，谢谢</span>

      <div v-if="feedbackOpen" class="fb__panel">
        <!--
          ⚠️ 2026-10-27 走查 C1：以前这块**没有标题、边框也没生效**（CSS 变量名写错了：
              `--line`/`--muted`/`--brand` 都不存在），看起来像一句普通提示。
              现在：标题 + 卡片 + 胶囊选项，一眼就知道"这是个能操作的东西"。
        -->
        <p class="fb__title">这条哪里不对？</p>
        <div class="fb__chips">
          <button
            v-for="option in [
              { value: 'wrong_answer', label: '答案错了' },
              { value: 'wrong_citation', label: '引用引错了' },
              { value: 'other', label: '其他' },
            ]"
            :key="option.value"
            type="button"
            :class="{ on: feedbackVerdict === option.value }"
            @click="feedbackVerdict = option.value as FeedbackVerdict"
          >
            {{ option.label }}
          </button>
        </div>
        <input
          v-model="feedbackNote"
          class="fb__note"
          type="text"
          maxlength="1000"
          placeholder="一句话说明（选「其他」时必填）"
        />
        <div class="fb__actions">
          <button type="button" class="fb__submit" :disabled="feedbackBusy" @click="sendFeedback">
            提交
          </button>
          <button type="button" @click="feedbackOpen = false">取消</button>
        </div>
        <p v-if="feedbackError" class="fb__error">{{ feedbackError }}</p>
      </div>
    </div>

    <!--
      ⚠️ **拒答时不能直接叫"参考来源"。**

         后端在拒答时**照样会把候选落库并返回**（`architecture.md` §9.19："数据层永远存全，
         拒答也落 citations"）—— 这是对的，评测和归因全靠它。但原样铺出来会自相矛盾：
         上面刚说"依据不足，我不推测"，下面却列 5 条像模像样的来源（2026-10-24 用户走查
         就卡在这里）。正确的说法是：这些是**检索到的候选，均低于阈值、未被采用**，
         而且默认折叠 —— 想看的人点开，不想看的人不受干扰。
    -->
    <section v-if="message.citations.length > 0 && message.refused" class="sources sources--refused">
      <details>
        <summary class="sources__title">
          未采用的候选 · {{ message.citations.length }} 条（均低于拒答阈值，与答案无关）
        </summary>
        <CitationList
          :citations="message.citations"
          :active-index="activeIndex"
          @open-context="emit('open-context', $event)"
        />
      </details>
    </section>

    <!--
      ⚠️ **降级角标（而不是正文里一句话）**。
          原来它是答案末尾的“（本次用了快速模式）”：混在正文里像答案的一部分，
          用户要读到句尾才知道降级了。现在搬到这里：扫一眼就知道，而且能看到原因。
          用灰色而不是红色 —— 它**不是错误**，答案仍是好的，只是走的是老路。
    -->
    <div v-if="message.degraded" class="degraded" :title="degradedTitle">
      ⚡ 快速模式<span v-if="message.agentSteps"> · 走了 {{ message.agentSteps }} 步后回落</span>
    </div>

    <!--
      **引用核对结果**（内部侧，2026-10-27）：
        · 有对不上的 → 黄条，说"我们没核对上 N 处"（**不说答案是错的**）；
        · 有被自动改正的编号 → 也必须说（否则等于"系统悄悄动了证据"）。
      判据 → `architecture.md §9.54`。
    -->
    <div v-if="unsupportedCount > 0" class="audit">
      ⚠️ 本次有 {{ unsupportedCount }} 处引用**未能核对上**，请以原文为准
    </div>
    <details v-if="repairs.length > 0" class="audit audit--repaired">
      <summary>
        🔧 已自动改正 {{ repairs.length }} 处引用编号（只改编号、正文一个字没动）
      </summary>
      <ul class="audit__list">
        <li v-for="(fix, index) in repairs" :key="index">
          <code>[{{ fix.marker }}] → [{{ fix.corrected }}]</code>
          <span class="audit__sentence">{{ fix.sentence }}</span>
        </li>
      </ul>
    </details>

    <!-- agent 轨迹：点开回放“它当时为什么这么做”（只有 agent 跑过才有） -->
    <details v-if="(message.agentTrace?.length ?? 0) > 0" class="trace">
      <summary class="trace__title">
        agent 轨迹 · {{ message.agentTrace?.length }} 条<span
          v-if="message.agentRetries"
        >
          · 自愈 {{ message.agentRetries }} 次</span
        >
      </summary>
      <ol class="trace__list">
        <li
          v-for="(step, index) in message.agentTrace"
          :key="index"
          :class="['trace__item', `trace__item--${step.kind}`]"
        >
          <span class="trace__step">#{{ step.step }}</span>
          <template v-if="step.kind === 'tool'">
            <strong>{{ step.name }}</strong>
            <span v-if="step.ok === false" class="trace__bad">失败</span>
            <span class="trace__ms">{{ step.ms }} ms</span>
            <span class="trace__summary">{{ step.summary }}</span>
          </template>
          <template v-else-if="step.kind === 'model'">
            选了 {{ (step.tool_calls ?? []).join('、') || '不调工具' }}
          </template>
          <template v-else-if="step.kind === 'ask_user'">反问：{{ step.question }}</template>
          <template v-else-if="step.kind === 'budget'">预算/{步数}用完（{{ step.reason }}）→ 回落</template>
          <template v-else-if="step.kind === 'model_error'">
            模型报错<span v-if="step.attempt">（第 {{ step.attempt }} 次）</span> → 回落
          </template>
          <template v-else-if="step.kind === 'refused'">阈值拒答（{{ step.reason }}）</template>
        </li>
      </ol>
    </details>

    <!--
      **参考来源：默认折叠 + 先给 5 条**（2026-10-27 走查第 6 条）。

      ⚠️ 两个容易搞错的地方：
        ① 这段**必须排除拒答**。以前它挂在一个 `v-else-if` 链上，而那条链的
           `v-if` 是"有没有 agent 轨迹" —— 于是拒答时**上面刚说"依据不足"、
           下面又列 5 条来源**（2026-10-24 走查就卡在这）。
        ② "折叠"用 `<details>` 而不是 v-if：内容还在 DOM 里，
           坐着看的和想复制的人拿到的是同一份。
    -->
    <details v-if="!message.refused && message.citations.length > 0" class="sources">
      <summary class="sources__title">
        参考来源 · {{ message.citations.length }} 条<span class="sources__hint">
          （点开可核对原文）</span
        >
      </summary>
      <CitationList
        :citations="visibleCitations"
        :active-index="activeIndex"
        @open-context="emit('open-context', $event)"
      />
      <button
        v-if="hiddenSourceCount > 0"
        type="button"
        class="sources__more"
        @click="showAllSources = !showAllSources"
      >
        {{ showAllSources ? '收起' : `还有 ${hiddenSourceCount} 条` }}
      </button>
    </details>
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
  word-break: break-word;
}

/*
  ⚠️ `pre-wrap` 只留给**用户消息**（他自己打的换行要保住）。
     助手消息现在走 Markdown 渲染：换行由 `breaks: true` 负责，
     这里再 `pre-wrap` 会让 `<p>` 之间多出一堆空行。
*/
.msg--user .bubble {
  border-color: transparent;
  background: var(--primary);
  color: #fff;
  white-space: pre-wrap;
}

/* ---------- 助手回答的 Markdown 排版 ----------
   ⚠️ 全部要 `:deep()`：内容由 `v-html` 插入，**不带 scoped 属性**，
      直接写 `.md p` 匹配不到（这是 Vue scoped CSS 与 v-html 的经典坑）。 */

.md > :deep(*:first-child) {
  margin-top: 0;
}

.md > :deep(*:last-child) {
  margin-bottom: 0;
}

.md :deep(p) {
  margin: 0 0 0.6em;
}

.md :deep(ul),
.md :deep(ol) {
  margin: 0 0 0.6em;
  padding-left: 1.4em;
}

.md :deep(li) {
  margin: 0.15em 0;
}

.md :deep(strong) {
  font-weight: 650;
}

.md :deep(h1),
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) {
  margin: 0.8em 0 0.4em;
  font-size: 1em;
  font-weight: 650;
}

.md :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--surface-sunken);
  font-family: var(--mono);
  font-size: 0.9em;
}

.md :deep(pre) {
  margin: 0 0 0.6em;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  overflow-x: auto;
}

.md :deep(pre code) {
  padding: 0;
  background: none;
}

.md :deep(blockquote) {
  margin: 0 0 0.6em;
  padding: 2px 10px;
  border-left: 2px solid var(--border-strong);
  color: var(--text-muted);
}

/* 表格：模型特别爱用它做对比。不给样式就是一坨竖线 */
.md :deep(table) {
  margin: 0 0 0.6em;
  border-collapse: collapse;
  font-size: 0.92em;
}

.md :deep(th),
.md :deep(td) {
  padding: 4px 10px;
  border: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}

.md :deep(th) {
  background: var(--surface-sunken);
  font-weight: 600;
}

.bubble--empty {
  color: var(--text-faint);
}

.banner {
  margin: 0;
}

.image-note {
  color: var(--text-muted);
  font-size: 0.8rem;
}

.image-note summary {
  cursor: pointer;
  user-select: none;
}

.image-note__body {
  margin: 4px 0 0;
  padding: 8px 10px;
  border-left: 2px solid var(--border-strong);
  background: var(--surface-sunken);
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}

.sources {
  margin-top: 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  padding: 6px 10px;
}

.sources__title {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
  /* ⚠️ 折叠后它就是个 `<summary>`：必须给 `cursor: pointer`，
     否则没人知道能点开（这是"默认折叠"最容易漏的一步）。 */
  cursor: pointer;
  user-select: none;
}

.sources__hint {
  font-weight: 400;
  color: var(--text-muted);
  opacity: 0.8;
}

.sources__more {
  margin-top: 6px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  font-size: 0.78rem;
  cursor: pointer;
}

.sources__more:hover {
  border-color: var(--primary);
  color: var(--primary);
}

/* 引用核对结果（内部侧）：**黄条，不是红条** —— "我们没核对上" ≠ "答案是错的" */
.audit {
  margin-top: 6px;
  padding: 4px 10px;
  border-left: 3px solid var(--warn);
  border-radius: 4px;
  background: var(--warn-soft);
  color: var(--text);
  font-size: 0.8rem;
}

/* 被自动改正过编号：用 info 色。它是"兜底生效了"，不是问题 */
.audit--repaired {
  border-left-color: var(--primary);
  background: var(--primary-soft);
  cursor: pointer;
}

.audit__list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 0.78rem;
}

.audit__sentence {
  margin-left: 6px;
  color: var(--text-muted);
}

/* 拒答时的候选：折叠 + 说明"未采用"，不再像"参考来源"那样默认铺开 */
.sources--refused {
  border-style: dashed;
}

.sources--refused summary.sources__title {
  cursor: pointer;
  user-select: none;
}

/* 正文里的 [n]。⚠️ `:deep()` 同上：按钮是 `v-html` 拼出来的 */
.md :deep(.marker) {
  padding: 0 3px;
  border: none;
  border-radius: 4px;
  background: var(--primary-soft);
  color: var(--primary);
  font-family: var(--mono);
  font-size: 0.8em;
  vertical-align: 1px;
}

.md :deep(.marker:hover),
.md :deep(.marker--active) {
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

<style scoped>
.degraded {
  display: inline-block;
  margin-top: 6px;
  padding: 1px 8px;
  border-radius: 10px;
  background: #f1f3f5;
  color: #868e96;
  font-size: 12px;
  cursor: help;
}
.trace {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted);
}
.trace__title {
  cursor: pointer;
}
.trace__list {
  margin: 6px 0 0;
  padding-left: 18px;
}
.trace__item {
  margin: 2px 0;
}
.trace__step {
  color: #adb5bd;
  margin-right: 6px;
}
.trace__ms {
  margin-left: 6px;
  color: #adb5bd;
}
.trace__summary {
  margin-left: 8px;
}
.trace__bad {
  margin-left: 6px;
  color: #e03131;
}
</style>

<style scoped>
/*
 * 纠错反馈（2026-10-27 走查 C1 重做）。
 *
 * ⚠️ 三条都是走查直接指出来的：
 *    ① **入口要像按钮**：描边 + 悬停变色，不能长得像一句提示；
 *    ② **面板要像卡片**：边框 + 圆角 + 底色 + 内边距，和正文分开；
 *    ③ **选项要像胶囊**：圆角要大（999px），选中的那颗实心。
 * ⚠️ 只用 `main.css` 里**真的存在**的变量（`--border` / `--text-muted` / `--primary`）——
 *    以前写的是 `--line` / `--muted` / `--brand`，全都不存在 ⟹ 静默退化成"没边框"。
 */
.fb {
  margin-top: 10px;
  font-size: 12px;
}

.fb__btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s,
    background 0.15s;
}

.fb__btn:hover {
  border-color: var(--primary);
  background: var(--primary-soft);
  color: var(--primary);
}

.fb__panel {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-sunken);
  box-shadow: var(--shadow-sm);
}

.fb__title {
  margin: 0 0 8px;
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
}

.fb__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.fb__chips button {
  padding: 3px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.fb__chips button.on {
  border-color: var(--primary);
  background: var(--primary);
  color: #fff;
  font-weight: 600;
}

.fb__note {
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 8px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: 12px;
}

.fb__actions {
  display: flex;
  gap: 6px;
}

.fb__actions button {
  padding: 4px 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.fb__actions button.fb__submit {
  border-color: var(--primary);
  background: var(--primary);
  color: #fff;
}

.fb__actions button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.fb__error {
  margin: 6px 0 0;
  color: var(--danger);
}

.fb__done {
  color: var(--ok);
}
</style>
