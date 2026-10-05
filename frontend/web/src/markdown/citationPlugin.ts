/**
 * 行内引用角标 —— remark 插件（§4.2.3 三层入口的第一层）。
 *
 * ⚠️ **实测**：`[n]` 在 mdast 里是**普通文本节点，不是链接** ——
 *    `"[1]，且无违纪记录[2]。"` 整段是一个 `text` 节点。
 *    所以角标只能靠**文本替换**产出，不能指望 Markdown 链接语法。
 *
 * 做法：把 `[n]` 换成 `link` 节点，url 记成 `#cite-N`；
 * 渲染层再把这种 href 认出来换成角标组件（见 MarkdownAnswer）。
 * 这样不需要 raw HTML、也不需要 rehype-sanitize（后者会吃掉 class）。
 */

import type { MdastNode } from './uncitedAnnotation'

const CITE_PREFIX = '#cite-'
const MARKER = /\[(\d+)\]/g
const HAS_MARKER = /\[\d+\]/

/** 角标 href 的前缀 —— 渲染层据此把普通链接与角标分开。 */
export function citeHref(marker: number): string {
  return `${CITE_PREFIX}${marker}`
}

/** 从 href 解出引用编号；不是角标返回 null。 */
export function parseCiteHref(href: string | undefined): number | null {
  if (!href || !href.startsWith(CITE_PREFIX)) return null
  const n = Number(href.slice(CITE_PREFIX.length))
  return Number.isInteger(n) && n > 0 ? n : null
}

function splitText(node: MdastNode): MdastNode[] {
  const value = node.value ?? ''
  const parts: MdastNode[] = []
  let cursor = 0
  for (const match of value.matchAll(MARKER)) {
    const at = match.index ?? 0
    // ⚠️ 拆出来的片段必须**继承原节点的 data 与 position**：
    //    置灰标注把 `<span class="uncited">` 记在 data.hName 上，
    //    新建裸节点会把置灰整段弄丢（实测踩过一次）。
    if (at > cursor) parts.push({ ...node, value: value.slice(cursor, at) })
    parts.push({
      type: 'link',
      url: citeHref(Number(match[1])),
      children: [{ type: 'text', value: match[0] }],
    } as MdastNode)
    cursor = at + match[0].length
  }
  if (cursor < value.length) parts.push({ ...node, value: value.slice(cursor) })
  return parts
}

function walk(node: MdastNode) {
  const children = node.children
  if (!children) return
  const next: MdastNode[] = []
  for (const child of children) {
    if (child.type === 'text' && child.value && HAS_MARKER.test(child.value)) {
      next.push(...splitText(child))
      continue
    }
    if (child.value === undefined) walk(child)
    next.push(child)
  }
  if (next.length !== children.length) node.children = next
}

/** remark 插件：把正文里的 `[n]` 换成角标节点。 */
export function remarkCitations() {
  return (tree: MdastNode) => walk(tree)
}
