/**
 * 无依据句置灰 —— mdast 层的标注器（方案 §4.2.1）。
 *
 * **难点不在渲染，在于答案经 Markdown 渲染后无法再用字符串查找定位**
 * （重复句子、渲染后文本变化）—— 服务端给的是**字符偏移**，
 * 必须把偏移重新映射回渲染树上的节点。
 *
 * ⚠️ 三条硬契约（§4.2.1.2）：
 *   ① 单位 **Unicode 码点**（服务端是 Python，`len` 就是码点；
 *      而 JS 字符串下标是 UTF-16 单元 —— 星平面字符上两者不等，
 *      而后端自己就会把 📚 写进答案，所以这个差异**必然**会遇到）
 *   ② 参照系是**渲染前的原始答案文本**（不是渲染后的）
 *   ③ 客户端拼接的 token 必须逐字等于服务端校验用的文本
 *      —— 这条无法靠校验发现，只能靠协议保证（服务端发解码后的纯文本）
 *
 * ⚠️ **三个漂移坑**（§4.2.1.5，都已实测复现）：
 *   ① 只处理 `text` 会**静默漏掉** `inlineCode` 之类的非 text 叶子
 *      —— 而且两道校验都抓不到（校验用的「可见文本」也一起漏）
 *      → 叶子一律按 `value !== undefined` 收集
 *   ② `inlineCode` 的 position 切出的是 `` `身份证原件` ``（带反引号），
 *      而 `value` 只有 `身份证原件` —— 直接拿偏移去索引 value 必错
 *      → 每个节点先断言「源码切片 === node.value」，不等就重新定位
 *   ③ `strong` 这类**容器**节点的 position 切出 `**30%**`（含标记），
 *      用它包裹会把标记一起包进去 → 只用叶子节点
 *
 * ⚠️ **失败就降级，不硬标**（§4.2.1.4）：校验不过 → 退回「消息级徽标」。
 *    Glean 的口径：matching 不确定时退到文档级，**避免误导用户**。
 *    **宁可不标，也不要标错。**
 *
 * 本文件是**唯一实现**：页面用它，回归脚本也 import 它（不许抄一份进脚本，
 * 抄了就「脚本全绿、页面照错」）。
 */

/** 悬浮提示文案（§4.2.1.6）—— 是**提示**不是断言：「此结论未在资料中找到依据」这种写法被明确禁用。 */
export const UNCITED_TITLE = '未在资料中找到对应依据，建议核对'

export interface UncitedClaim {
  sentence: string
  /** 相对**渲染前原始答案**的偏移，单位 Unicode 码点 */
  char_start: number
  char_end: number
}

/** 最小 mdast 形状 —— 只声明用得到的字段，避免为一个插件引整套类型。 */
export interface MdastNode {
  type: string
  value?: string
  url?: string
  children?: MdastNode[]
  position?: {
    start: { offset?: number }
    end: { offset?: number }
  }
  data?: {
    hName?: string
    hProperties?: Record<string, unknown>
  }
}

export type IssueKind =
  | 'validation_a'      // 前置校验：源码切片 ≠ sentence（真正的漂移检测）
  | 'validation_b'      // 后置校验：标注吃掉了字符（标注器自检）
  | 'relocated'         // value 与切片不等，已按 value 重新定位（坑②）
  | 'relocate_failed'   // 重新定位也失败 → 该节点不标
  | 'partial_leaf'      // 非 text 叶子只被部分覆盖 → 不切开、不标（上报）

export interface AnnotationIssue {
  kind: IssueKind
  detail: string
}

export interface AnnotationResult {
  /** 校验 A 通过的 claim 数（漂移检测的判据） */
  passedA: number
  /** 真正落到 DOM 上的标注片段数 */
  annotated: number
  issues: AnnotationIssue[]
  /** 有任何 issue → 调用方应降级成消息级徽标 */
  degraded: boolean
}

// ---- 偏移换算 --------------------------------------------------------------

/**
 * 码点下标 → JS 字符串下标（UTF-16 单元）。
 *
 * 📚 这类星平面字符在 JS 里占 2 个单元、在 Python 里占 1 个码点 ——
 * 不换算的话，偏移从第一个 emoji 之后就开始漂。
 */
export function cpToUtf16(text: string, cp: number): number {
  let units = 0
  let count = 0
  for (const ch of text) {
    if (count === cp) return units
    units += ch.length
    count += 1
  }
  return units
}

/** 校验 A（§4.2.1.4）：服务端同时下发偏移与 sentence，两者互相验证。 */
export function checkClaims(rawText: string, claims: UncitedClaim[]): AnnotationIssue[] {
  const issues: AnnotationIssue[] = []
  claims.forEach((claim, index) => {
    const start = cpToUtf16(rawText, claim.char_start)
    const end = cpToUtf16(rawText, claim.char_end)
    const slice = rawText.slice(start, end)
    if (slice !== claim.sentence) {
      issues.push({
        kind: 'validation_a',
        detail: `第 ${index} 条偏移切出的是 ${JSON.stringify(slice)}，与 sentence ${JSON.stringify(claim.sentence)} 不符`,
      })
    }
  })
  return issues
}

// ---- 可见文本（两个校验都用它）---------------------------------------------

/** 按 `value !== undefined` 收集叶子 —— 坑①：漏掉非 text 叶子，校验也会跟着瞎。 */
export function collectLeaves(node: MdastNode, out: MdastNode[] = []): MdastNode[] {
  if (node.value !== undefined) out.push(node)
  for (const child of node.children ?? []) collectLeaves(child, out)
  return out
}

export function visibleText(node: MdastNode): string {
  return collectLeaves(node)
    .map((leaf) => leaf.value ?? '')
    .join('')
}

// ---- 标注 ------------------------------------------------------------------

interface Range {
  start: number // UTF-16，含
  end: number   // UTF-16，不含
  claim: number
}

/** 给叶子包一层 `<span class="uncited">`（text 叶子）或加 class（code 叶子）。 */
function markNode(node: MdastNode, asElement: boolean) {
  if (asElement) {
    node.data = { ...node.data, hName: 'span', hProperties: { className: 'uncited', title: UNCITED_TITLE } }
  } else {
    // inlineCode / code：换成 span 会丢掉等宽样式 —— 只加 class，保留原元素
    node.data = {
      ...node.data,
      hProperties: { ...(node.data?.hProperties ?? {}), className: 'uncited', title: UNCITED_TITLE },
    }
  }
}

function annotateNode(parent: MdastNode, rawText: string, ranges: Range[], result: AnnotationResult) {
  const children = parent.children
  if (!children) return
  const next: MdastNode[] = []

  for (const child of children) {
    if (child.value === undefined) {
      // 容器节点：**不用它的 position**（坑③），只递归
      annotateNode(child, rawText, ranges, result)
      next.push(child)
      continue
    }

    const rawStart = child.position?.start.offset
    const rawEnd = child.position?.end.offset
    if (rawStart === undefined || rawEnd === undefined) {
      next.push(child)
      continue
    }

    // 坑②：先断言「源码切片 === node.value」，不等就按 value 重新定位
    let start = rawStart
    let end = rawEnd
    if (rawText.slice(start, end) !== child.value) {
      const at = rawText.indexOf(child.value, rawStart)
      if (at === -1 || at + child.value.length > rawEnd) {
        result.issues.push({
          kind: 'relocate_failed',
          detail: `节点 ${child.type} 的 value 在源码区间内找不到：${JSON.stringify(child.value)}`,
        })
        next.push(child)
        continue
      }
      result.issues.push({
        kind: 'relocated',
        detail: `${child.type}: 切片 ${JSON.stringify(rawText.slice(rawStart, rawEnd))} → 按 value 定位到 ${at}`,
      })
      start = at
      end = at + child.value.length
    }

    const hits = ranges.filter((r) => r.start < end && r.end > start)
    if (hits.length === 0) {
      next.push(child)
      continue
    }

    if (child.type === 'text') {
      // text 叶子 → 按交集切开，中间那段包 span
      let cursor = start
      for (const hit of hits) {
        const from = Math.max(hit.start, start)
        const to = Math.min(hit.end, end)
        if (from > cursor) {
          next.push({ ...child, value: rawText.slice(cursor, from), data: undefined })
        }
        const marked: MdastNode = { ...child, value: rawText.slice(from, to) }
        markNode(marked, true)
        next.push(marked)
        result.annotated += 1
        cursor = to
      }
      if (cursor < end) {
        next.push({ ...child, value: rawText.slice(cursor, end), data: undefined })
      }
    } else {
      // 非 text 叶子（inlineCode / code）：整段被覆盖才标；部分覆盖上报、不硬标
      const fully = hits.some((r) => r.start <= start && r.end >= end)
      if (fully) {
        markNode(child, false)
        result.annotated += 1
        next.push(child)
      } else {
        result.issues.push({
          kind: 'partial_leaf',
          detail: `${child.type} 只被部分覆盖，代码片段无法切开：${JSON.stringify(child.value)}`,
        })
        next.push(child)
      }
    }
  }

  // ⚠️ **必须无条件赋值**：标注是「用改过的副本替换原节点」，
  //    节点数可能不变（1 进 1 出）—— 只在长度变化时赋值的话，
  //    这种替换会被整段丢掉（实测踩过：`**30%**` 里的标注消失，
  //    而外层两段照常，肉眼看着像「只标了一半」）。
  parent.children = next
}

/**
 * 标注整棵树。返回报告 —— 调用方据 `degraded` 决定是否退回消息级徽标。
 *
 * 两道校验都跑：**A 是漂移检测**（能抓住 ±1 / ±2 的偏移错位），
 * **B 只是标注器自检**（拿同一个错误偏移算两边，抓不到漂移）。
 */
export function annotateTree(tree: MdastNode, rawText: string, claims: UncitedClaim[]): AnnotationResult {
  const result: AnnotationResult = { passedA: 0, annotated: 0, issues: [], degraded: false }

  // ① 前置校验 A
  const failures = checkClaims(rawText, claims)
  result.issues.push(...failures)
  result.passedA = claims.length - failures.length
  if (failures.length > 0) {
    result.degraded = true
    return result // 校验不过就不标 —— 宁可不标，不要标错
  }

  const ranges: Range[] = claims
    .map((claim, index) => ({
      start: cpToUtf16(rawText, claim.char_start),
      end: cpToUtf16(rawText, claim.char_end),
      claim: index,
    }))
    .sort((a, b) => a.start - b.start)

  const before = visibleText(tree)
  annotateNode(tree, rawText, ranges, result)

  // ④ 后置校验 B：标注后文本必须与标注前逐字相同（标注不能吃掉字符）
  if (visibleText(tree) !== before) {
    result.issues.push({ kind: 'validation_b', detail: '标注前后可见文本不一致 —— 标注吃掉了字符' })
  }

  result.degraded = result.issues.some(
    (issue) => issue.kind === 'validation_b' || issue.kind === 'relocate_failed',
  )
  return result
}

// ---- remark 插件 -----------------------------------------------------------

export interface UncitedPluginOptions {
  rawText: string
  claims: UncitedClaim[]
  /** 插件跑完把报告回填到这里（页面用来决定要不要降级） */
  report?: AnnotationResult
}

/** remark 插件外壳：`remarkPlugins={[[remarkUncited, {rawText, claims, report}]]}`。 */
export function remarkUncited(options: UncitedPluginOptions) {
  return (tree: MdastNode) => {
    const result = annotateTree(tree, options.rawText, options.claims ?? [])
    if (options.report) Object.assign(options.report, result)
  }
}
