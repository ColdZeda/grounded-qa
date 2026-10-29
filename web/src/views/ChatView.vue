<script setup lang="ts">
/**
 * 聊天页 —— 消费 SSE 的那一端。两处不看注释就会写错：流式消息要先 `reactive()` 再 push；
 * 输入框要判 `isComposing`（中文输入法选词时回车会被提前发出去）。
 */

import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import ContextDrawer from '@/components/ContextDrawer.vue'
import MessageItem from '@/components/MessageItem.vue'
import { ApiError } from '@/api/client'
import { streamAnswer } from '@/api/chat'
import { currentUser, setChatMode } from '@/stores/auth'
import { createConversation, getConversation, listMessages } from '@/api/conversations'
import {
  IMAGE_ACCEPT,
  describeImageProblem,
  primeImageUrl,
  squeezeForUpload,
  uploadImage,
} from '@/api/images'
import type { Channel, MessageOut } from '@/types/api'
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
/**
 * agent 正在干什么（`tool` 帧的 summary）。空 = 没在调工具。
 *
 * ⚠️ 这一行字不是装饰：agent 的中间步骤对用户是不可见的，
 *    不告诉他"正在查订单"，就只剩下一个转圈的等待。
 */
const toolLabel = ref('')

/**
 * 这次提问走哪条路。**默认快速**（人已经答定的产品选择）。
 *
 * ⚠️ 三层开关里它只管第二层：这里选什么都不作数，还要与服务端总闸相与。
 *    总闸关掉时，按钮直接**置灰**（不是隐藏）—— 让人知道“有这个功能，但管理员关了”。
 */
const chatMode = ref<'fast' | 'agent'>(
  currentUser.value?.preferences?.chat_mode === 'agent' ? 'agent' : 'fast',
)

/** 服务端总闸是不是开着（`GET /auth/me` 里的 `agent_available`）。 */
const agentAvailable = computed(() => currentUser.value?.agent_available === true)

/**
 * 这个会话是从哪来的（`web` / `feishu_p2p` / `feishu_group`）。
 *
 * ⚠️ **飞书会话在 Web 端只读**（2026-10-27 走查第 5 条）：在这里打字发不出去到飞书，
 *    却会进下一轮的上下文 —— 坐席以为顾客看见了，其实没有。所以：去发送框、去图片、
 *    去 agent 开关，只留"看 + 反馈"。
 * ⚠️ `/conversations/new` 还没有会话 ⟹ 先当 `web`（一定是本端发起的）。
 */
const conversationChannel = ref<Channel>('web')
const isExternalChannel = computed(() => conversationChannel.value !== 'web')
const agentHint = computed(() =>
  agentAvailable.value
    ? '更准（会自己查订单/改写检索），但慢 2~4 倍'
    : '管理员已关闭 Agent 模式',
)

/** 切模式：本地立即生效（不等网络），同步记到后端供下次回显。 */
function pickMode(mode: 'fast' | 'agent'): void {
  if (mode === 'agent' && !agentAvailable.value) return
  chatMode.value = mode
  void setChatMode(mode)
}
const abort = ref<AbortController | null>(null)

/**
 * "最后一条是我的提问、后面没有回答"（2026-10-28 新增的兜底）。
 *
 * ⚠️ **为什么需要它**：走查第三次发现，生成中途切走页面会让后端那一轮**被取消** ——
 *    不是"落库失败"，是答案没生成、没落库、连 token 都没记，库里只剩一条没人回的提问。
 *    现在已经改成"离开也让它跑完"（见 `onBeforeUnmount`），但**仍然可能**发生
 *    （关标签页 / 断网 / 后端真的挂了），所以这里要显式告诉人，而不是显示一片空白。
 * ⚠️ 不要一看到就断言"失败了"：更常见的情况是**它还在生成**。所以先轮询，
 *    见 `watchOrphan`。
 */
const orphanQuestion = ref('')
/**
 * 两种状态（2026-10-28 用户定）：
 * · `waiting` —— 刚回来、还在等。**大多数情况它真的还在生成**（agent 最长 30 秒），
 *   所以这里只说"还在生成"，**不吓人**。
 * · `failed` —— 等满 `ORPHAN_WAIT_MS` 还没有 ⟹ 基本可以断定那一轮断了，
 *   才说"连接断开了，这次回答没有生成"并给重发。
 * ⚠️ 不要一进门就报"断开"：那是误报（走查里确认过这个判断）。
 */
const orphanPhase = ref<'waiting' | 'failed'>('waiting')
/** 轮询的令牌：换会话 / 重新加载时作废上一轮，避免两个循环抢着改 `messages`。 */
let orphanSeq = 0

/**
 * 回来时发现"提问没人回"⟹ 轮询几次看是不是还在生成。
 *
 * 时间表：先 0.4s 一次，之后每 2s 一次，**总共约 35 秒**（`ORPHAN_WAIT_MS`）。
 * ⚠️ 为什么是 35 秒：agent 的**总预算是 30 秒**（`agent_total_budget_seconds`）。
 *    以前只等 6.4 秒就停手 —— 那条其实还在生成，界面却已经说"没生成完"，
 *    等于把正在发生的事报成故障。宁可多问十几次（几十字节的 GET），也不要误报。
 * 等满了还没有 ⟹ 才把 `orphanPhase` 翻成 `failed`（说"连接断开了"）。
 */
const ORPHAN_WAIT_MS = 35_000

async function watchOrphan(conversationId: string): Promise<void> {
  const seq = ++orphanSeq
  const deadline = Date.now() + ORPHAN_WAIT_MS
  let first = true
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, first ? 400 : 2000))
    first = false
    if (seq !== orphanSeq || !orphanQuestion.value) return
    try {
      const history = await listMessages(conversationId)
      // ⚠️ 判据是"条数变多了"而不是"最后一条是 assistant"：
      //    后端可能落进来的是**两条**（提问 + 回答），比位置判断稳。
      if (history.length > messages.value.length) {
        messages.value = toUiList(history)
        orphanQuestion.value = ''
        orphanPhase.value = 'waiting'
        await scrollToBottom(true)
        return
      }
    } catch {
      // 拉不到就下一轮再试 —— 真都没有，就停在提示上（让人自己点重发）
    }
  }
  // 等够了还是没有 ⟹ 那一轮确实断了（不是"还在生成"）
  if (seq === orphanSeq && orphanQuestion.value) orphanPhase.value = 'failed'
}

/**
 * 重试 = **把那句话填回输入框**，由人自己按发送。
 *
 * ⚠️ 刻意**不新增"重新生成"接口**：那要动 `ask()` 的入参语义（复用哪条提问、
 *    旧的孤儿怎么处理），收益只是少按一次回车。填回输入框零风险、行为可预期。
 */
function retryOrphan(): void {
  draft.value = orphanQuestion.value
  orphanQuestion.value = ''
  orphanPhase.value = 'waiting'
}

const scroller = ref<HTMLElement | null>(null)
/** 用户是不是"贴着底部"。不贴就说明他在往上翻历史 —— 这时**不能**把他拽回来。 */
const stick = ref(true)

// 附件（一张图）
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
  // agent 跑过的工具优先：它比"正在生成"具体得多，而且是真实发生的事
  if (toolLabel.value) return toolLabel.value
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
    // ⚠️ agent 的痕迹在 `meta` 里（只有助手消息可能有）。
    //    不映射的话，刷新页面后角标和轨迹就都消失了 —— 而“为什么回落”恰恰是事后才要查的。
    degraded: message.meta?.degraded ?? null,
    agentSteps: message.meta?.agent_steps ?? null,
    agentRetries: message.meta?.agent_retries ?? null,
    agentTrace: message.meta?.agent_trace ?? null,
    // 引用核对结果 / 编号自动改正 —— **两种模式都有**，不看 `agent_trace`。
    // ⚠️ 不映射的话，刷新后"这条被改过编号"就消失了 ⟹ 等于悄悄改。
    citationAudit: message.meta?.citation_audit ?? null,
    citationRepairs: message.meta?.citation_repairs ?? null,
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
    // 新会话一定是本端发起的 ⟹ 一定可写（别把上一次打开的飞书会话状态带过来）
    conversationChannel.value = 'web'
    messages.value = []
    loading.value = false
    return
  }

  try {
    const [conversation, history] = await Promise.all([getConversation(id), listMessages(id)])
    conversationTitle.value = conversation.title
    // ⚠️ **飞书会话在 Web 端只读**（2026-10-27 走查第 5 条）。
    //    在这里发出去的消息飞书那边收不到，却会**污染下一轮的上下文** ——
    //    坐席会以为顾客看见了，其实没有。这是最坏的一种"看起来能用"。
    conversationChannel.value = conversation.channel
    messages.value = toUiList(history)
    // ⚠️ 最后一条是**我的提问**、后面没有回答 ⟹ 要么还在生成，要么那一轮被中断了。
    //    两种情况都要给人一个交代（以前是**一片空白**，看着像卡死）。
    const last = messages.value[messages.value.length - 1]
    if (last?.role === 'user') {
      orphanQuestion.value = last.content
      void watchOrphan(id)
    } else {
      orphanQuestion.value = ''
      orphanSeq += 1 // 作废可能在跑的轮询
    }
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

// 滚动
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

// 提问
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
        onTool: (event) => {
          // 一旦开始出字，工具状态就该让位 —— 否则会与正文同时在屏幕上赛跑。
          toolLabel.value = event.summary || ''
        },
        onDelta: (text) => {
          toolLabel.value = ''
          pending.content += text
          void scrollToBottom()
        },
        onDone: (event) => {
          pending.refused = event.refused
          pending.confidence = event.confidence
          pending.latencyMs = event.latency_ms
          // ⚠️ 降级要**当场**告诉用户，不能等刷新页面才从 meta 里看到
          pending.degraded = event.degraded ?? null
          pending.agentSteps = event.agent_steps ?? null
          // ⚠️ 轨迹**当轮就下发**（2026-10-27 走查 B2）：以前只有刷新才有，
          //    而"刚才它为什么这么做"恰恰是当场才想问的。
          pending.agentTrace = event.agent_trace ?? pending.agentTrace ?? null
          pending.citationAudit = event.citation_audit ?? null
          pending.citationRepairs = event.citation_repairs ?? null
        },
        onError: (event) => {
          pending.error = event.detail
        },
      },
      controller.signal,
      image?.imageId ?? null,
      chatMode.value,
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
    // ⚠️ **只清"我这一路"的状态**：离开页面不再 abort（见 `onBeforeUnmount`）⟹
    //    旧会话的流可能在**新一次提问之后**才收尾，无条件写会把新的那条打断。
    if (abort.value === controller) {
      abort.value = null
      toolLabel.value = ''
      phase.value = 'idle'
    }
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
  // ⚠️⚠️ **2026-10-28 改：这里不再 `abort()`。**
  //
  //    以前离开页面就掐断连接 ⟹ 后端那一轮被 Starlette **取消**：
  //    答案不生成、不落库、`record_tokens` 也不执行（**钱花了、账没记**），
  //    会话里只剩一条没人回的提问。走查第三次实测：两次切走，两次都这样
  //    （`chat.py` 里"断开也会落库"那句注释只在最后几十毫秒成立，等于没有）。
  //
  //    现在只"不再渲染"，让流自己读完 ⟹ 后端照常生成 → 落库 → 记账，
  //    你回到这个会话能看到答案（`watchOrphan` 负责"还没落库"的那几秒）。
  //    ⚠️ 用户点「停止」仍然 abort —— 那是**明确的意图**，见 `stop()`。
  //    ⚠️ 代价：离开页面这一轮照旧把 token 花完。它本来就在花，以前是花完还没结果。
  phase.value = 'idle'
  toolLabel.value = ''
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
          :conversation-id="id"
          @open-context="openContext"
        />
        <!--
          "提问没人回"的交代（2026-10-28）。**两段**：
          · 前 35 秒说"还在生成"（大多数情况真的还在跑，别吓人）；
          · 等满了才说"连接断开了"（这时才确定那一轮没跑完）。
        -->
        <p v-if="orphanQuestion && orphanPhase === 'waiting'" class="orphan orphan--waiting">
          <span class="orphan__dot"></span>
          回答还在生成…
        </p>
        <p v-else-if="orphanQuestion" class="orphan">
          连接断开了，这次回答没有生成。
          <button type="button" class="orphan__retry" @click="retryOrphan">重发</button>
        </p>
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

    <!--
      ⚠️ **飞书会话在 Web 端只读**（2026-10-27 走查第 5 条）。

      为什么不是"发得出去就行"：这条会话的消息由飞书那边的顾客产生，
      我们在这里打字**发不到他手机上**，却会进下一轮的上下文 ——
      坐席会以为顾客看见了，其实没有。所以整条 composer 换成一句说明，
      只留"看 + 反馈"。（要回复顾客请去飞书群里回，那是唯一真实的通道。）
    -->
    <footer v-if="isExternalChannel" class="composer composer--readonly">
      <p class="composer__readonly">
        🔒 这是
        <strong>{{ conversationChannel === 'feishu_group' ? '飞书群' : '飞书私聊' }}</strong>
        会话，在 Web 端<strong>只读</strong> —— 这里发出的消息顾客收不到。
        <span class="composer__readonly-hint">要回复请回到飞书；要复核证据就点上面的来源。</span>
      </p>
    </footer>

    <footer v-else class="composer">
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
        <div class="mode-switch" role="group" aria-label="提问模式">
          <button
            type="button"
            :class="{ on: chatMode === 'fast' }"
            :disabled="phase !== 'idle'"
            title="固定流水线，快（约 4 秒）"
            @click="pickMode('fast')"
          >
            快速
          </button>
          <button
            type="button"
            :class="{ on: chatMode === 'agent' }"
            :disabled="phase !== 'idle' || !agentAvailable"
            :title="agentHint"
            @click="pickMode('agent')"
          >
            Agent
          </button>
        </div>
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

/* 「提问没人回」的提示：**低调**但看得见 —— 它是兜底说明，不是错误。 */
.orphan {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 0;
  padding: 10px 14px;
  border: 1px dashed var(--border);
  border-radius: 10px;
  background: var(--surface-sunken);
  color: var(--text-muted);
  font-size: 13px;
}

.orphan__retry {
  padding: 3px 10px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.orphan__retry:hover {
  border-color: var(--primary);
  color: var(--primary);
}

/* 「还在生成」那一档：一个会呼吸的小圆点，比一行字更像"在动" */
.orphan--waiting {
  border-style: solid;
}

.orphan__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--primary);
  animation: orphan-pulse 1s ease-in-out infinite;
}

@keyframes orphan-pulse {
  0%,
  100% {
    opacity: 0.25;
  }
  50% {
    opacity: 1;
  }
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

/* 飞书会话的只读条：说明「这里发不出去」，别让它看起来像坏掉的输入框 */
.composer--readonly {
  background: var(--surface-sunken);
}

.composer__readonly {
  max-width: 820px;
  margin: 0 auto;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.6;
}

.composer__readonly-hint {
  display: block;
  margin-top: 2px;
  color: var(--text-faint);
  font-size: 0.8rem;
}

.composer__bar {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 820px;
  margin: 0 auto 8px;
}

.mode-switch {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
  margin-right: 8px;
  vertical-align: middle;
}
.mode-switch button {
  border: 0;
  background: transparent;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
}
.mode-switch button.on {
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 600;
}
.mode-switch button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
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
