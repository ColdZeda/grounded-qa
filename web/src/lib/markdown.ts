/**
 * 把模型回答渲染成 HTML。
 *
 * ## 为什么现在才引 Markdown 渲染
 *
 * 之前只做"纯文本 + `[n]` 可点"，理由是"不想为排版引一整套依赖"。那时回答短、少表格，
 * 看不出问题。但**带图提问必然触发结构化长回答**：2026-10-22 用户真机问
 * 「这个椅子角坏了能不能退款」，屏幕上直接是 `**能否退款取决于…**`、`- 列点`、`| 支付方式 |`
 * 一串星号和竖线 —— 每次都被看见。
 *
 * ## 安全：`html: false` 是红线，不许改
 *
 * 回答是模型生成的，而模型会**复述用户输入**（比如有人问"`<img onerror=…>` 是什么意思"）。
 * `html: false`（markdown-it 默认）会把原始 HTML **转义**，所以 `v-html` 只可能渲染
 * "我们自己拼出来的标签"和"转义后的文本"。
 * ⚠️ 哪天有人为了"支持回答里的 HTML"改成 `true`，那就是一个 XSS 洞。
 *
 * ## `[n]` 引用按钮
 *
 * 索引号原来的实现是"切段 → 渲染成 Vue 按钮"。现在正文是 `v-html` 出来的 HTML，
 * 没法再塞 Vue 组件，所以改成：
 *   ① 把 `[n]` 替换成 `<button data-cite="n">`；
 *   ② 事件用**委托**（`MessageItem` 上挂一个 click，读 `data-cite`）。
 *
 * ⚠️ 替换时**跳过 HTML 标签内部**（`<[^>]*>` 分支），否则会改到属性值里；
 *    也正因为文本部分已被转义，`[n]` 里不可能夹带引号，不会拼出畸形属性。
 * ⚠️ 代价如实说：`[1]` 出现在**代码块里**也会被换成按钮。旧实现同样如此，先接受。
 */

import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  html: false, // ← 安全红线，见文件头
  linkify: false, // 回答里的裸 URL 不该被自动变成可点链接（预览不了目标）
  breaks: true, // 单个换行就是换行 —— 模型很爱用单换行分行，不这样的话会挤成一坨
})

/** 引用索引的替换：先匹配标签（原样保留），再匹配 `[n]`。 */
const MARKER = /(<[^>]*>)|\[(\d{1,2})\]/g

/**
 * @param content 模型返回的原始文本（**未转义**）
 * @param valid   正文里允许变成按钮的索引号（`citations[].index`）——
 *                ⚠️ 不在这个集合里的 `[n]` 一律保持原样：
 *                否则正文里正常的方括号（`[2024]`、或者写了 `[1]` 却没给来源的回答）
 *                会变成点了没反应的按钮，比不渲染更让人困惑。
 * @param active  当前高亮的索引（点了哪条来源）。
 *                ⚠️ 为什么把"高亮"塞进渲染参数：`v-html` 里的节点**绑不上 Vue 的
 *                `:class`**，只能重渲染一次 HTML 把 class 拼进去。一次点击重渲一次，
 *                代价可以忽略（回答是几 KB 的字符串）。
 */
export function renderAnswer(
  content: string,
  valid: Set<number>,
  active: number | null = null,
): string {
  const html = md.render(content)
  if (valid.size === 0) return html

  return html.replace(MARKER, (match, tag: string | undefined, digits: string | undefined) => {
    if (tag) return match
    const index = Number(digits)
    if (!valid.has(index)) return match
    const cls = index === active ? 'marker marker--active' : 'marker'
    return `<button type="button" class="${cls}" data-cite="${index}">[${index}]</button>`
  })
}

/**
 * 渲染**知识库原片段**（「看原文」抽屉里用）。
 *
 * ⚠️ 为什么也要过 markdown：语料里的政策表就是 markdown 表格，不过渲染的话
 *    「看原文」里是一屏 `| 支付方式 | 到账时间 |` 和 `|---|---|` —— 那是**源码**，
 *    不是"原文"。我们要让人看到的是**原文的意思**（表就是表），
 *    这与引用摘要的清洗（`app/rag/mdtext.py`）是两件事：那边是"塞不进一行里只好降级"，
 *    这里有版面，就该正常渲染。
 * ⚠️ 同样**不生成 `[n]` 按钮**：片段里的 `[1]` 是文字，不是引用。
 * ⚠️ `html: false` 这条红线对这里同样有效（文档内容也是外部输入）。
 */
export function renderChunk(content: string): string {
  return md.render(content)
}
