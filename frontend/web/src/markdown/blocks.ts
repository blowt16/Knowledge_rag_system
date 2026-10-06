/**
 * 流式分块（§4.2.4.1）。
 *
 * **要解决的问题**：每来一个 token 就把整段 Markdown 全量重解析 —— M0 的
 * 最简聊天页就是这么写的（方案点的反例）。答案长起来之后，每 token 一次
 * unified 管道是纯浪费，而且会闪。
 *
 * **做法**：把答案切成「已完结块」+「尾部未完结块」。已完结的块用 `memo`
 * 包住只渲染一次，每个 token 只重渲染尾部那一块。
 *
 * ⚠️ 三条边界，每一条错了都是「看着还行」的错：
 * ① **代码围栏没闭合时不许切** —— 切在围栏中间，两半都会渲染成普通段落。
 * ② **松散列表的项之间不许切** —— `1. a / 空行 / 2. b` 一切开，第二块从 1 数起。
 *    （段落与列表之间**可以**切：两者本就是独立的块。）
 * ③ **拼接必须逐字还原** —— 分块一旦丢字或多字，引用角标的字符偏移
 *    与置灰标注会跟着错位（§4.2.1.2 说这类错位「无法靠任何校验发现」）。
 *    块文本**带结尾空行**正是为了让 `closed.join('') + tail` 逐字等于原文。
 *
 * 回归脚本：`npx tsx scripts/stream_blocks_probe/run.ts`
 */

export interface Block {
  /** 块正文。已完结块**可能带结尾空行** —— 那是为了拼接能逐字还原。 */
  text: string
  /** 在整段答案里的起始偏移（码点位置，与原文一致） */
  start: number
}

export interface SplitResult {
  /** 已完结的块，按顺序；只增不改（memo 才成立） */
  closed: Block[]
  /** 还在流的那一块（永远是最后一段） */
  tail: Block
}

/** 行首的围栏标记：``` 或 ~~~，允许最多 3 个前导空格 */
const FENCE = /^ {0,3}(`{3,}|~{3,})/
/** 列表项：`- x` / `* x` / `+ x` / `1. x` / `1) x` */
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s/

export function splitBlocks(text: string): SplitResult {
  const closed: Block[] = []
  let blockStart = 0
  let i = 0
  let fence: { marker: string; len: number } | null = null
  /** 当前块最后一条**非空**行 —— 判断列表要不要连在一起 */
  let lastLine = ''

  while (i < text.length) {
    const nl = text.indexOf('\n', i)
    const line = nl === -1 ? text.slice(i) : text.slice(i, nl)

    const fenceMatch = FENCE.exec(line)
    if (fenceMatch) {
      const run = fenceMatch[1]
      const marker = run[0]
      if (fence === null) {
        // 记下**开围栏的游程长度**：CommonMark 里 ``` 不闭合 ````，
        // 只看首字符会把两对错配成一对，于是在未闭合的围栏中间切一刀
        fence = { marker, len: run.length }
      } else if (marker === fence.marker && run.length >= fence.len) {
        fence = null
      }
    }

    const isBlank = line.trim() === ''
    if (isBlank && fence === null && i > blockStart) {
      const rest = nl === -1 ? '' : text.slice(nl + 1)
      const nextLine = rest.split('\n').find((l) => l.trim() !== '') ?? ''
      // 列表项之间不切：切了第二块会从 1 重新编号
      const keepTogether = LIST_ITEM.test(lastLine) && LIST_ITEM.test(nextLine)
      if (!keepTogether) {
        const blockEnd = nl === -1 ? text.length : nl + 1
        // ⚠️ **纯空白块也要收**：跳过一个块却仍然推进游标，那些字符就既不在
        //    `closed` 也不在 `tail` 里，「逐字还原」当场失效（评审 M4-K1 实测：
        //    `"\n\n甲段。"` 拼回来是 `"甲段。"`）。它渲染出来是空块，无害；
        //    但少了它，引用角标与置灰的偏移参照就不完整了。
        closed.push({ text: text.slice(blockStart, blockEnd), start: blockStart })
        blockStart = blockEnd
        lastLine = ''
      }
    } else if (!isBlank) {
      lastLine = line
    }

    if (nl === -1) break
    i = nl + 1
  }

  return { closed, tail: { text: text.slice(blockStart), start: blockStart } }
}
