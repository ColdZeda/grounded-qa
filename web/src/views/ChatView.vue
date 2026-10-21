<script setup lang="ts">
/**
 * 聊天页 —— 消费 SSE 的那一端。
 *
 * 这个文件里有两处**不看注释就一定会写错**的地方，都标了 ⚠️：
 *   1. 流式消息必须用 `reactive()` 包一层再 push（否则界面不动）
 *   2. 输入框必须判 `isComposing`（否则中文输入法选词时按回车会被提前发出去）
 */

import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import MessageItem from '@/components/MessageItem.vue'
import { ApiError } from '@/api/client'
import { streamAnswer } from '@/api/chat'
import { createConversation, getConversation, listMessages } from '@/api/conversations'
import { IMAGE_ACCEPT, describeImageProblem, primeImageUrl, uploadImage } from '@/api/images'
import type { MessageOut } from '@/types/api'
import type { UiMessage } from '@/types/ui'

const props = defineProps<{ id?: string }>()
const router = useRouter()

const conversationTitle = ref('')
const messages = ref<UiMessage[]>([])
const draft = ref('')
const loading = ref(true)
const loadError = ref('')

/** `retrieving` = 检索中（还没收到 sources）；`generating` = 正在出字。 */
const phase = ref<'idle' | 'retrieving' | 'generating'>('idle')
const abort = ref<AbortController | null>(null)

const scroller = ref<HTMLElement | null>(null)
/** 用户是不是"贴着底部"。不贴就说明他在往上翻历史 —— 这时**不能**把他拽回来。 */
const stick = ref(true)

// ------------------------------------------------------------
// 附件（一张图）
// ------------------------------------------------------------
/**
 * 待发送的那张图。
 *
 * ⚠️ **选好就先本地预览**（`URL.createObjectURL(file)`），不等上传 ——
 *    上传 3 MB 在弱网要几秒，用户需要立刻看到"我选的就是这张"。
 * ⚠️ 上传是**先落库**再提问的（两步）：图要能长期留着（刷新还得看得见），
 *    所以它天然属于"先存好、再引用"，不能等发消息时才一并塞过去。
 */
interface Attachment {
  file: File
  /** 本地预览 URL。上传成功后也用它当缩略图，省一次"下载自己刚传的图"。 */
  previewUrl: string
  /** 上传成功后的 id。`null` = 还不能发。 */
  imageId: string | null
  status: 'uploading' | 'ready' | 'error'
  error: string
}

const attach = ref<Attachment | null>(null)
/** 选图阶段就被挡下来的提示（格式 / 大小）。和"上传失败"分开：那个有图可看，这个没有。 */
const attachNotice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const uploading = computed(() => attach.value?.status === 'uploading')
const canSend = computed(() => draft.value.trim().length > 0 && !uploading.value)

const phaseLabel = computed(() => {
  // ⚠️ 上传中也得给个说法：不然按钮明明亮着却点不动（或者点下去什么都没发生）。
  if (uploading.value) return '图片上传中…'
  if (phase.value === 'retrieving') return '正在检索知识库…'
  if (phase.value === 'generating') return '正在生成回答…'
  return ''
})

function pickImage(): void {
  attachNotice.value = ''
  fileInput.value?.click()
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // ⚠️ **立刻清空**：不清的话，同一个文件选第二次不会触发 `change`
  //    （用户改完图又选回原来那张时，表现就是"点了没反应"）。
  input.value = ''
  if (file) void attachFile(file)
}

/** 本地预检 + 上传。任意一步失败都留在界面上，不静默吞掉。 */
async function attachFile(file: File): Promise<void> {
  attachNotice.value = ''

  const problem = describeImageProblem(file)
  if (problem) {
    attachNotice.value = problem
    return
  }

  const previewUrl = URL.createObjectURL(file)
  attach.value = { file, previewUrl, imageId: null, status: 'uploading', error: '' }

  try {
    const image = await uploadImage(file)
    // ⚠️ 等上传的这段时间里用户可能已经移除了这张图、或换了一张 ——
    //    那就别把结果挂到别的东西上（否则缩略图会对不上）。
    if (attach.value?.previewUrl !== previewUrl) return
    attach.value.imageId = image.id
    attach.value.status = 'ready'
    // 让消息里的缩略图直接用这张本地预览：图就在眼前，没必要再下一遍。
    primeImageUrl(image.id, previewUrl)
  } catch (err) {
    if (attach.value?.previewUrl !== previewUrl) return
    attach.value.status = 'error'
    attach.value.error =
      err instanceof ApiError ? err.message : `上传失败：${(err as Error).message}`
  }
}

function removeAttach(): void {
  attach.value = null
  attachNotice.value = ''
}

/** 上传失败（限流 / 网络抖一下）后重试，不用让用户重新选一遍图。 */
function retryAttach(): void {
  const current = attach.value
  if (current) void attachFile(current.file)
}

/**
 * 粘贴一张图（Ctrl+V）。
 *
 * ⚠️ 这是**截图提问**最主要的入口：用户截完图顺手就是粘贴，
 *    比"点选图 → 在文件对话框里找到刚才那张截图"快得多。
 * ⚠️ 只在剪贴板里真的有**文件**时才拦：不然会把用户粘贴的文字也吃掉。
 */
function onPaste(event: ClipboardEvent): void {
  const items = event.clipboardData?.items
  if (!items) return
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) {
        event.preventDefault()
        void attachFile(file)
        return
      }
    }
  }
}

/**
 * ⚠️ 懒创建会话后我们会自己 `router.replace` 到真实 id，
 *    那会让下面这个 watch 触发一次 `load()` —— 而此刻回答**正在流式接收中**，
 *    重新拉消息会把还没落库的助手消息抹掉（后端要到流结束才落库）。
 *    所以这一次要跳过。
 */
let skipNextLoad = false

function toUi(message: MessageOut): UiMessage {
  return {
    id: message.id,
    role: message.role === 'user' ? 'user' : 'assistant',
    content: message.content,
    citations: message.citations,
    refused: message.refused,
    latencyMs: message.latency_ms,
    // 2026-10-15 起置信度**落库了**（`architecture.md` §9.19）→ 刷新后徽标不再消失。
    // （这里以前写死 `null`，注释还写着"这是已知的不完美" —— 现在修掉了。）
    confidence: message.confidence,
    // 图也一样：`meta.image_id` 落库了，刷新后缩略图还在（不需要额外请求列表）。
    imageId: message.meta?.image_id ?? null,
    createdAt: message.created_at,
  }
}

/**
 * 历史消息 → 界面消息。**多一步搬运**，原因是读图读数存在哪：
 *
 * ⚠️ 后端把 `image_note` / `image_error` 写在**用户消息**的 `meta` 里（图属于那条消息），
 *    但界面上要挂在**它对应的那条回答**上 —— 否则"📷 读图结果"出现在我的问题底下，
 *    看着像我自己说的话。
 * ⚠️ 所以要搬一次：助手消息紧跟在它回答的那条用户消息之后（后端就是按这个顺序存的）。
 *    这样"刷新后看到的"和"刚生成时看到的"是同一个样子，不用两套渲染逻辑。
 */
function toUiList(history: MessageOut[]): UiMessage[] {
  const out = history.map(toUi)
  for (let i = 0; i < history.length; i += 1) {
    const source = history[i]
    if (source.role !== 'user' || !source.meta) continue
    const reply = out[i + 1]
    if (reply && reply.role === 'assistant') {
      reply.imageNote = source.meta.image_note ?? null
      reply.imageError = source.meta.image_error ?? null
    }
  }
  return out
}

let localSeq = 0
function nextLocalId(): string {
  localSeq += 1
  return `local-${localSeq}`
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''

  const id = props.id
  if (!id) {
    // `/conversations/new`：还没有会话，等第一条提问时才建。
    conversationTitle.value = ''
    messages.value = []
    loading.value = false
    return
  }

  try {
    const [conversation, history] = await Promise.all([getConversation(id), listMessages(id)])
    conversationTitle.value = conversation.title
    messages.value = toUiList(history)
    await scrollToBottom(true)
  } catch (err) {
    loadError.value =
      err instanceof ApiError && err.status === 404
        ? '这个会话不存在，或者不属于你。'
        : `加载失败：${(err as Error).message}`
  } finally {
    loading.value = false
  }
}

watch(
  () => props.id,
  () => {
    if (skipNextLoad) {
      skipNextLoad = false
      return
    }
    void load()
  },
  { immediate: true },
)

// ------------------------------------------------------------
// 滚动
// ------------------------------------------------------------
function onScroll(): void {
  const el = scroller.value
  if (!el) return
  stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 120
}

async function scrollToBottom(force = false): Promise<void> {
  if (!force && !stick.value) return
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

// ------------------------------------------------------------
// 提问
// ------------------------------------------------------------
/** 拿提问当会话标题。后端限 200 字符，这里截短一点，列表里才不会挤成一团。 */
function titleFrom(question: string): string {
  return question.length <= 30 ? question : `${question.slice(0, 30)}…`
}

async function send(): Promise<void> {
  const question = draft.value.trim()
  // ⚠️ 只有**上传成功**的图才跟着走。`uploading` 时直接不许发：
  //    发了也带不上图，而用户会以为图一起发过去了。
  const image = attach.value?.status === 'ready' ? attach.value : null
  if (!question || phase.value !== 'idle' || uploading.value) return

  draft.value = ''
  // 上传失败了还点了发送 → 别把"没带图"这件事悄悄咽掉，说一句。
  if (attach.value && image === null) {
    attachNotice.value = '图片没上传成功，这条问题只带了文字'
  }
  attach.value = null

  messages.value.push({
    id: nextLocalId(),
    role: 'user',
    content: question,
    citations: [],
    refused: false,
    latencyMs: null,
    confidence: null,
    imageId: image?.imageId ?? null,
    createdAt: new Date().toISOString(),
  })

  // ⚠️⚠️ **必须 `reactive()` 包一层。**
  //     `ref([])` 的深层响应式是**读取时**才把元素换成 Proxy 的；
  //     如果 push 一个普通对象、然后攥着那个**原始对象**的引用来改它，
  //     改动会绕过 Proxy → **界面完全不动**，控制台也不报错。
  //     这是 Vue 3 里最经典的"数据变了页面不变"，而且极难往响应式上联想。
  const pending = reactive<UiMessage>({
    id: nextLocalId(),
    role: 'assistant',
    content: '',
    citations: [],
    refused: false,
    latencyMs: null,
    confidence: null,
    createdAt: new Date().toISOString(),
    streaming: true,
    error: null,
  })
  messages.value.push(pending)

  phase.value = 'retrieving'
  void scrollToBottom()

  const controller = new AbortController()
  abort.value = controller

  try {
    let conversationId = props.id
    if (!conversationId) {
      // 会话**在这时候才建**，为了把提问当标题（见文件头说明）。
      const created = await createConversation(titleFrom(question))
      conversationTitle.value = created.title
      conversationId = created.id
      skipNextLoad = true
      await router.replace({ name: 'chat', params: { id: created.id } })
    }

    await streamAnswer(
      conversationId,
      question,
      {
        onSources: (event) => {
          pending.citations = event.citations
          pending.refused = event.refused
          pending.confidence = event.confidence
          // ⚠️ 图的状态**必须落到界面上**：`error` 非空说明这次**没读成图**，
          //    用户有权知道（否则会以为我们看过图了 —— `PLAN §8.3` D10）。
          if (event.image) {
            pending.imageNote = event.image.note
            pending.imageError = event.image.error
          }
          // 拒答是**直接给结论**的：后端不会再调模型，所以不会再有 delta。
          // 把它当成"生成中"会让界面一直转圈。
          phase.value = event.refused ? 'idle' : 'generating'
        },
        onDelta: (text) => {
          pending.content += text
          void scrollToBottom()
        },
        onDone: (event) => {
          pending.refused = event.refused
          pending.confidence = event.confidence
          pending.latencyMs = event.latency_ms
        },
        onError: (event) => {
          pending.error = event.detail
        },
      },
      controller.signal,
      image?.imageId ?? null,
    )
  } catch (err) {
    if (controller.signal.aborted) {
      // 用户点了"停止"。⚠️ 后端**照样会把已生成的部分落库**（生成器的 finally），
      // 所以这里说的是"停止接收"，不是"丢弃"。
      pending.cancelled = true
      pending.error = '已取消'
    } else if (err instanceof ApiError) {
      pending.error = err.message
    } else {
      pending.error = (err as Error).message
    }
  } finally {
    pending.streaming = false
    phase.value = 'idle'
    abort.value = null
    void scrollToBottom()
  }
}

function stop(): void {
  abort.value?.abort()
}

function onKeydown(event: KeyboardEvent): void {
  // ⚠️ `isComposing` 这一判是**中文界面的必修课**：
  //    用输入法打字时按回车是"确认候选词"，不是"发送"。
  //    不判的话，用户刚打完拼音就被发出去了 —— 而且只在中文输入法下复现。
  //    （`keyCode === 229` 是部分浏览器在组合输入期间给的旧标记，一并兜住。）
  if (event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

onBeforeUnmount(() => {
  // 离开页面就别再收流了。⚠️ 后端会继续把它生成完并落库 —— 这是刻意的
  // （否则"我看到了半截，刷新却没了"）。这里只是不再占用这个连接。
  abort.value?.abort()
})
</script>

<template>
  <div class="shell">
    <AppHeader>
      <span class="conv-title" :title="conversationTitle">{{ conversationTitle || '新会话' }}</span>
      <button class="btn btn--ghost btn--sm" type="button" @click="router.push({ name: 'chat-new' })">
        ＋ 新会话
      </button>
    </AppHeader>

    <main ref="scroller" class="stream" @scroll.passive="onScroll">
      <p v-if="loadError" class="alert alert--danger">{{ loadError }}</p>
      <p v-else-if="loading" class="muted">加载中…</p>

      <p v-else-if="messages.length === 0" class="intro">
        问点什么吧。回答会带上引用来源；如果知识库里找不到依据，它会直接拒答，而不是编一个。
      </p>

      <div v-else class="thread">
        <MessageItem v-for="message in messages" :key="message.id" :message="message" />
      </div>
    </main>

    <footer class="composer">
      <div class="composer__bar">
        <button
          class="btn btn--ghost btn--sm"
          type="button"
          :disabled="phase !== 'idle' || uploading"
          title="也可以直接把截图粘贴进输入框（Ctrl+V）"
          @click="pickImage"
        >
          ＋ 图片
        </button>
        <span v-if="phaseLabel" class="phase">{{ phaseLabel }}</span>
      </div>

      <!--
        ⚠️ `accept` 只是给文件对话框一个初始筛选，**不是校验**（用户能切成"所有文件"）。
       真正的拦截是 `describeImageProblem()`（前端）和 `app/images.py::sniff`（后端看文件头）。
      -->
      <input
        ref="fileInput"
        class="hidden-file"
        type="file"
        :accept="IMAGE_ACCEPT"
        @change="onFileChange"
      />

      <p v-if="attachNotice" class="alert alert--warn attach-notice">{{ attachNotice }}</p>

      <div v-if="attach" class="attach">
        <img class="attach__thumb" :src="attach.previewUrl" alt="待发送的图片" />
        <span class="attach__state">
          <template v-if="attach.status === 'uploading'">上传中…</template>
          <template v-else-if="attach.status === 'ready'">已就绪，发送时一起提交</template>
          <template v-else>{{ attach.error }}</template>
        </span>
        <button
          v-if="attach.status === 'error'"
          class="btn btn--ghost btn--sm"
          type="button"
          @click="retryAttach"
        >
          重试
        </button>
        <button class="btn btn--ghost btn--sm" type="button" @click="removeAttach">移除</button>
      </div>

      <div class="composer__row">
        <textarea
          v-model="draft"
          class="field composer__input"
          rows="2"
          placeholder="输入问题，Enter 发送，Shift + Enter 换行（可以直接 Ctrl+V 粘贴截图）"
          :disabled="phase !== 'idle'"
          @keydown="onKeydown"
          @paste="onPaste"
        ></textarea>

        <button
          v-if="phase !== 'idle'"
          class="btn btn--ghost composer__send"
          type="button"
          @click="stop"
        >
          停止
        </button>
        <button
          v-else
          class="btn composer__send"
          type="button"
          :disabled="!canSend"
          @click="send"
        >
          发送
        </button>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.conv-title {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.9rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stream {
  flex: 1;
  overflow-y: auto;
  padding: 24px 20px;
}

.thread {
  display: flex;
  flex-direction: column;
  gap: 22px;
  max-width: 820px;
  margin: 0 auto;
}

.muted {
  max-width: 820px;
  margin: 0 auto;
  color: var(--text-muted);
}

.intro {
  max-width: 560px;
  margin: 12vh auto 0;
  color: var(--text-faint);
  text-align: center;
}

.composer {
  border-top: 1px solid var(--border);
  background: var(--surface);
  padding: 10px 20px 14px;
}

.composer__bar {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 820px;
  margin: 0 auto 8px;
}

.phase {
  /* 阶段提示靠右，和左边的"＋ 图片"分开 —— 它们是两类信息 */
  margin-left: auto;
  color: var(--text-muted);
  font-size: 0.8rem;
}

.hidden-file {
  display: none;
}

.attach-notice {
  max-width: 820px;
  margin: 0 auto 8px;
}

/* 待发送的图：一条细带，不抢输入框的位置 */
.attach {
  display: flex;
  gap: 10px;
  align-items: center;
  max-width: 820px;
  margin: 0 auto 8px;
  padding: 6px 10px 6px 6px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
}

.attach__thumb {
  flex-shrink: 0;
  width: 42px;
  height: 42px;
  border-radius: 4px;
  object-fit: cover;
}

.attach__state {
  flex: 1;
  color: var(--text-muted);
  font-size: 0.8rem;
  word-break: break-word;
}

.composer__row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  max-width: 820px;
  margin: 0 auto;
}

.composer__input {
  resize: none;
  line-height: 1.5;
}

.composer__send {
  flex-shrink: 0;
  height: 38px;
}
</style>
