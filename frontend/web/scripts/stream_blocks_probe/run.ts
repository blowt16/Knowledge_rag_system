/**
 * 流式分块的回归脚本（§4.2.4.1）—— **手工回归脚本，不是自动化测试**（文档 6.2）。
 *
 * 跑法：`npx tsx scripts/stream_blocks_probe/run.ts`
 *
 * ⚠️ **import 前端真实实现**，不许把分块逻辑抄一份进来 ——
 *    抄一份的话脚本全绿而页面照错，等于白写。
 *
 * 为什么值得单独一条：分块的错法都是「看着还行」的错 ——
 * 切多了一处，列表编号会从 1 重来；切少了一处，每个 token 又在全量重解析。
 * 只有把不变式钉死才看得出来。
 */
import { splitBlocks } from '../../src/markdown/blocks'

let pass = 0
let fail = 0

function check(name: string, cond: boolean, detail = '') {
  if (cond) {
    pass++
    console.log(`  ✅ ${name}`)
  } else {
    fail++
    console.log(`  ❌ ${name}${detail ? `\n      ${detail}` : ''}`)
  }
}

/** ★ 最强的那条不变式：切完拼回去必须**逐字等于**原文。
 *  分块一旦丢字或多字，引用角标与置灰偏移都会跟着错位。 */
function roundTrip(text: string) {
  const { closed, tail } = splitBlocks(text)
  const joined = closed.map((b) => b.text).join('') + tail.text
  check(
    `拼接还原 === 原文（${JSON.stringify(text.slice(0, 18))}…）`,
    joined === text,
    joined === text ? '' : `得到 ${JSON.stringify(joined.slice(0, 60))}`,
  )
}

console.log('— 基本边界 —')
{
  const { closed, tail } = splitBlocks('第一段。\n\n第二段。\n\n第三段还在流')
  check('两个空行 → 两个已完结块', closed.length === 2, `实为 ${closed.length}`)
  check('尾部是未完结块', tail.text === '第三段还在流', tail.text)
  check('块起点偏移正确（空行整条归前一块）', closed[0].start === 0 && closed[1].start === 6,
        JSON.stringify(closed.map((b) => b.start)))
  roundTrip('第一段。\n\n第二段。\n\n第三段还在流')
}

console.log('— 围栏没闭合时不许切 —')
{
  const text = '说明：\n\n```python\nprint(1)\n\nprint(2)\n```\n\n结束。'
  const { closed, tail } = splitBlocks(text)
  check('围栏内的空行不算边界 —— 围栏整体是一块',
        closed.length >= 2 && closed[1].text.includes('print(1)')
        && closed[1].text.includes('print(2)'), JSON.stringify(closed.map((b) => b.text)))
  check('围栏闭合之后才切', closed.length === 2 && closed[0].text.trimEnd() === '说明：',
        JSON.stringify(closed.map((b) => b.text)))
  roundTrip(text)
}

console.log('— 未闭合的围栏整体留在尾部 —')
{
  const text = '前面的话。\n\n```\n还没写完的代码\n'
  const { closed, tail } = splitBlocks(text)
  check('尾部含未闭合围栏', tail.text.includes('```') && tail.text.includes('还没写完'),
        tail.text)
  check('前面的话已完结', closed.length === 1 && closed[0].text.trimEnd() === '前面的话。',
        JSON.stringify(closed.map((b) => b.text)))
  roundTrip(text)
}

console.log('— ★ 松散列表不能被切开（编号会从 1 重来）—')
{
  const text = '先说结论。' + '\n\n' + '1. 第一步' + '\n\n' + '2. 第二步' + '\n\n' + '3. 第三步'
  const { closed, tail } = splitBlocks(text)
  check('段落到列表之间可以切', closed.length === 1 && closed[0].text.trimEnd() === '先说结论。',
        JSON.stringify(closed.map((b) => b.text)))
  check('列表整体留在尾部（一项都没被切出去）',
        tail.text.trimStart().startsWith('1. 第一步') && tail.text.includes('3. 第三步'),
        JSON.stringify(tail.text))
  roundTrip(text)
}

console.log('— 表格 —')
{
  const text = '| 级别 | 后果 |\n| --- | --- |\n| 黄 | 提醒 |\n\n下面是说明。'
  const { closed, tail } = splitBlocks(text)
  check('表格是一整块', closed.length === 1 && closed[0].text.includes('| 黄 |'),
        JSON.stringify(closed.map((b) => b.text)))
  check('表格之后是尾部', tail.text === '下面是说明。', tail.text)
  roundTrip(text)
}

console.log('— 尾巴里的半截语法不能崩 —')
{
  for (const text of ['**加粗没写完', '| 表头 | 表头', '```js\nconst a', '- 列表项', '## 标题']) {
    const { closed, tail } = splitBlocks(text)
    check(`不切半截：${JSON.stringify(text.slice(0, 12))}`,
          closed.length === 0 && tail.text === text, JSON.stringify(closed.map((b) => b.text)))
    roundTrip(text)
  }
}

console.log('— 幂等与空串 —')
{
  const a = splitBlocks('')
  check('空串不产出块', a.closed.length === 0 && a.tail.text === '')
  const text = '一段。\n\n二段。'
  const first = splitBlocks(text)
  const again = splitBlocks(first.closed.map((b) => b.text).join('') + first.tail.text)
  check('切两遍结果一致', JSON.stringify(first) === JSON.stringify(again))
  const trailing = splitBlocks('一段。\n\n')
  check('末尾空行不产生空块',
        trailing.closed.length === 1 && trailing.tail.text === '',
        JSON.stringify(trailing))
}

console.log('— 还原流式过程：切出来的块必须**单调前进** —')
{
  // 模拟逐字到达：每个前缀都要能切，且已完结块只增不减、内容不变
  const full = '甲甲甲。\n\n乙乙乙。\n\n丙丙丙。'
  let lastClosedTexts: string[] = []
  let stable = true
  for (let i = 0; i <= full.length; i++) {
    const { closed } = splitBlocks(full.slice(0, i))
    const texts = closed.map((b) => b.text)
    // 已完结的块不能再变（否则 memo 就是白 memo，页面会闪）
    if (texts.length < lastClosedTexts.length) stable = false
    for (let k = 0; k < lastClosedTexts.length; k++) {
      if (texts[k] !== lastClosedTexts[k]) stable = false
    }
    lastClosedTexts = texts
  }
  check('流式过程中已完结块只增不改（memo 才有意义）', stable)
}

console.log(`\n${pass}/${pass + fail} 通过`)
if (fail > 0) process.exit(1)
