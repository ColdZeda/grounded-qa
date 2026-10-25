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
import ContextDrawer from '@/components/ContextDrawer.vue'
import MessageItem from '@/components/MessageItem.vue'
import { ApiError } from '@/api/client'
import { streamAnswer } from '@/api/chat'
import { createConversation, getConversation, listMessages } from '@/api/conversations'
import {
  IMAGE_ACCEPT,
  describeImageProblem,
  primeImageUrl,
  squeezeForUpload,
  uploadImage,
} from '@/api/images'
import type { MessageOut } from '@/types/api'
import type { UiMessage } from '@/types/ui'
import { detailLevel, setDetailLevel } from '@/stores/prefs'

/**
 * 详细程度两档（设计理由 `architecture.md` §9.19）。
 *
 * ⚠️ 别把"简洁"理解成"给客户看所以藏来源" —— **藏的是分数，不是来源**。
 *    「这条来自《退款政策》」是建立信任的东西；「精排分 0.466」才是实现细节。
 */
const DETAIL_LEVELS = [
  { value: 'detailed', label: '详细', hint: '显示分数与口径 —— 给开发、评测、面试演示' },
  { value: 'concise', label: '简洁', hint: '隐藏分数，只留来源 —— 给终端用户' },
] as const

const props = defineProps<{ id?: string }>()
const router = useRouter()

const conversationTitle = ref('')
const messages = ref<UiMessage[]>([])
const draft = ref('')
const loading = ref(true)
const loadError = ref('')

/**
 * 「看原文」抽屉的状态。
 *
 * ⚠️ 放在**页面级**（不在 `MessageItem` 里）：一条消息可能有很多来源，
 *    每个来源各挂一个抽屉 = 一堆重复状态，而且"同时打开两个"迟早发生。
 *    `null` = 抽屉关着。
 */
const ctx = ref<{ documentId: string; ordinal: number; title: string } | null>(null)

function openContext(payload: { documentId: string; ordinal: number; title: string }): void {
  ctx.value = payload
}

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
 *    上传在弱网要几秒，用户需要立刻看到"我选的就是这张"。
 * ⚠️ 上传是**先落库**再提问的（两步）：图要能长期留着（刷新还得看得见），
 *    所以它天然属于"先存好、再引用"，不能等发消息时才一并塞过去。
 * ⚠️ `source` 和 `file` 是**两个**东西：前端会先压一遍（`squeezeForUpload`），
 *    `file` 是压完要上传的那个。重试必须从 `source` 重来 —— 在压过的 JPEG 上
 *    再压一次只会越压越糊，而且没有任何收益。
 */
interface Attachment {
  /** 用户最初选的那个文件。 */
  source: File
  /** 真正要上传的字节（压成功就是 JPEG，没压等于 `source`）。 */
  file: File
  /** 本地预览 URL。上传成功后也用它当缩略图，省一次"下载自己刚传的图"。 */
  previewUrl: string
  /** 上传成功后的 id。`null` = 还不能发。 */
  imageId: string | null
  /** `preparing` = 正在压缩（还没开始传）。 */
  status: 'preparing' | 'uploading' | 'ready' | 'error'
  error: string
  /** 压缩结果的一行说明（"3.2 MB → 410 KB"）。没压就是空串。 */
  note: string
}

const attach = ref<Attachment | null>(null)
/** 选图阶段就被挡下来的提示（格式 / 大小）。和"上传失败"分开：那个有图可看，这个没有。 */
const attachNotice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/**
 * 给每次附件操作发一个递增号。
 *
 * ⚠️ 压缩 ＋ 上传加起来可能有几秒，这期间用户完全可能**移除**这张图或**换一张**。
 *    用号牌判"我还是不是当前那次"，比拿 `previewUrl` 互相比更稳 ——
 *    压缩会把 `previewUrl` 换掉，旧的比对方式在这一步之后就失效了。
 */
let attachToken = 0

const uploading = computed(
  () => attach.value?.status === 'preparing' || attach.value?.status === 'uploading',
)
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

/** 本地预检 + 压缩 + 上传。任意一步失败都留在界面上，不静默吞掉。 */
async function attachFile(file: File): Promise<void> {
  attachNotice.value = ''

  const problem = describeImageProblem(file)
  if (problem) {
    attachNotice.value = problem
    return
  }

  const token = (attachToken += 1)
  const originalUrl = URL.createObjectURL(file)

  /**
   * ⚠️ 一律**换一个新对象**，不原地改字段。
   *    `ref({...})` 读出来的是内部 Proxy，但手里若留着当初那个普通对象去 `.status = …`，
   *    界面一个字都不会动、也不报错（`architecture.md` §9.13 那个坑）。
   */
  const patch = (next: Partial<Attachment>): void => {
    if (!attach.value) return
    attach.value = { ...attach.value, ...next }
  }

  attach.value = {
    source: file,
    file,
    previewUrl: originalUrl,
    imageId: null,
    status: 'preparing',
    error: '',
    note: '',
  }

  // ① 压缩。⚠️ 压不动就**照原图传**：让后端给出权威的报错（"格式不支持"这种话
  //    只有它知道得最准），而不是在这里编一句"压缩失败，无法发送"。
  let toSend = file
  try {
    const squeezed = await squeezeForUpload(file)
    if (token !== attachToken) return
    if (squeezed) {
      toSend = squeezed.file
      const squeezedUrl = URL.createObjectURL(squeezed.file)
      patch({ file: squeezed.file, previewUrl: squeezedUrl, note: squeezed.note })
      // ⚠️ 先换 `src` 再回收旧 URL：反过来的话，`<img>` 还挂着已经死掉的 blob，
      //    会闪一下碎图标（虽然只有一个微任务，但很难看）。
      URL.revokeObjectURL(originalUrl)
    }
  } catch {
    // 解不开 / canvas 不给 —— 保持原图
  }

  if (token !== attachToken) return
  patch({ status: 'uploading' })

  // ② 上传
  try {
    const image = await uploadImage(toSend)
    if (token !== attachToken) return
    patch({ imageId: image.id, status: 'ready' })
    // 让消息里的缩略图直接用这张本地预览：图就在眼前，没必要再下一遍。
    if (attach.value) primeImageUrl(image.id, attach.value.previewUrl)
  } catch (err) {
    if (token !== attachToken) return
    patch({
      status: 'error',
      error: err instanceof ApiError ? err.message : `上传失败：${(err as Error).message}`,
    })
  }
}

function removeAttach(): void {
  // ⚠️ 号牌要进位：不然还在压/还在传的那次回来时会把结果挂到"下一张图"上
  attachToken += 1
  attach.value = null
  attachNotice.value = ''
}

/** 上传失败（限流 / 网络抖一下）后重试，不用让用户重新选一遍图。 */
function retryAttach(): void {
  const current = attach.value
  // ⚠️ 从 `source` 重来，不传压过的 `file`
  if (current) void attachFile(current.source)
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
    // 短追问改写：刷新页面后 chip 也要在（这是"事后看得出有没有生效"的那条路）
    followupPrev: message.meta?.followup_prev ?? null,
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

  // ⚠️ 用户这条也要 `reactive()`：检索结束后要往它身上补 `followupPrev`
  //    （chip「已合并上一轮检索」）。攥着 push 进去的**原始对象**改是改不动的 ——
  //    原因见下面 `pending` 那段（`architecture.md` §9.13）。
  const mine = reactive<UiMessage>({
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
  messages.value.push(mine)

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
          // 短追问改写：挂到**用户那条**上（详细档才显示）。
          if (event.followup_prev) mine.followupPrev = event.followup_prev
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
    <!--
      标题就是会话标题（新会话时是「新会话」）：页面动作条只回答"这一页是什么"。
      ⚠️ `back` 是 2026-10-25 走查补的：此前回列表只有左下角侧栏的「会话」，
         它长得像"分区入口"、不像"返回"，外人第一反应是找不到路。
    -->
    <AppHeader
      :title="conversationTitle || '新会话'"
      tone="blue"
      back="/conversations"
      back-label="会话列表"
    >
      <button class="btn btn--ghost btn--sm" type="button" @click="router.push({ name: 'chat-new' })">
        ＋ 新会话
      </button>
      <!--
        详细程度开关**只属于会话页**（2026-10-24 走查结论）：
        它只影响三样东西 —— 消息里的置信度、来源卡片的精排分、读图结果，
        放在全局顶栏属于位置错配（在别的页面点它，什么都不会变）。
      -->
      <div class="detail" role="group" aria-label="详细程度">
        <button
          v-for="level in DETAIL_LEVELS"
          :key="level.value"
          type="button"
          class="detail__btn"
          :class="{ 'detail__btn--on': detailLevel === level.value }"
          :title="level.hint"
          :aria-pressed="detailLevel === level.value"
          @click="setDetailLevel(level.value)"
        >
          {{ level.label }}
        </button>
      </div>
    </AppHeader>

    <main ref="scroller" class="stream" @scroll.passive="onScroll">
      <p v-if="loadError" class="alert alert--danger">{{ loadError }}</p>
      <p v-else-if="loading" class="muted">加载中…</p>

      <p v-else-if="messages.length === 0" class="intro">
        问点什么吧。回答会带上引用来源；如果知识库里找不到依据，它会直接拒答，而不是编一个。
      </p>

      <div v-else class="thread">
        <MessageItem
          v-for="message in messages"
          :key="message.id"
          :message="message"
          @open-context="openContext"
        />
      </div>
    </main>

    <!-- 「看原文」抽屉：**点了才挂载、才请求**（关着的时候零成本） -->
    <ContextDrawer
      v-if="ctx"
      :document-id="ctx.documentId"
      :ordinal="ctx.ordinal"
      :title="ctx.title"
      @close="ctx = null"
    />

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

      <p v-if="attachNotice" class="alert alert--warn attach-notice">
        <span class="attach-notice__text">{{ attachNotice }}</span>
        <button
          class="attach-notice__close"
          type="button"
          title="关掉这条提示"
          aria-label="关掉这条提示"
          @click="attachNotice = ''"
        >
          ×
        </button>
      </p>

      <div v-if="attach" class="attach">
        <img class="attach__thumb" :src="attach.previewUrl" alt="待发送的图片" />
        <span class="attach__state">
          <template v-if="attach.status === 'preparing'">正在压缩图片…</template>
          <template v-else-if="attach.status === 'uploading'">上传中…</template>
          <template v-else-if="attach.status === 'ready'">已就绪，发送时一起提交</template>
          <template v-else>{{ attach.error }}</template>
          <span v-if="attach.note" class="attach__note">（{{ attach.note }}）</span>
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

/* 详细程度：两格分段开关。刻意做得不显眼 —— 它是设置，不是主操作。
   ⚠️ 只出现在会话页：它只影响消息里的置信度 / 来源精排分 / 读图结果。 */
.detail {
  display: flex;
  flex-shrink: 0;
  overflow: hidden;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
}

.detail__btn {
  padding: 4px 10px;
  border: none;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.detail__btn--on {
  background: var(--primary);
  color: #fff;
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

/* 提示条自己带 × ：这类"格式/大小不对"的说明看一遍就够了，
   但以前它只能等下一次选图才消失，占着地方。 */
.attach-notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  max-width: 820px;
  margin: 0 auto 8px;
}

.attach-notice__text {
  flex: 1;
}

.attach-notice__close {
  flex-shrink: 0;
  padding: 0 2px;
  border: none;
  background: none;
  color: inherit;
  font-size: 1.1rem;
  line-height: 1;
  opacity: 0.65;
  cursor: pointer;
}

.attach-notice__close:hover {
  opacity: 1;
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

/* 压缩结果（"3.2 MB → 410 KB"）：让人看得出"这么大也能发了"是压过的功劳 */
.attach__note {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
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
