/**
 * 置灰标注的手工回归脚本（§8.2 测试点 A / 方案 §4.2.1、§6.2）。
 *
 * 跑法：`cd frontend/web && npx tsx scripts/annotation_probe/run.ts`
 *
 * ⚠️ **口径**：这是**手工回归脚本，不是自动化测试** —— 方案 §6.2 明确
 *    「前端不做自动化测试」，偏移映射靠这个脚本手工跑。别把它塞进 CI 当测试层。
 *
 * ⚠️ **必须 import 前端的真实实现**（`src/markdown/uncitedAnnotation.ts`）。
 *    把标注逻辑抄一份进脚本的话，脚本全绿而页面照错 —— 等于白写。
 *
 * 它存在的理由：偏移映射出错时页面只是「标歪了」，肉眼很难发现；
 * 而脚本能一眼看红绿。方案 §4.2.1.5 的三个漂移坑，本脚本逐个复现。
 */

import assert from 'node:assert/strict'
import rehypeStringify from 'rehype-stringify'
import remarkParse from 'remark-parse'
import remarkRehype from 'remark-rehype'
import { remarkCitations } from '../../src/markdown/citationPlugin'
import { unified } from 'unified'
import {
  UNCITED_TITLE,
  annotateTree,
  checkClaims,
  remarkUncited,
  visibleText,
  type MdastNode,
  type UncitedClaim,
} from '../../src/markdown/uncitedAnnotation'

// ---- 小工具 ---------------------------------------------------------------

function nthIndexOf(haystack: string, needle: string, occurrence = 0): number {
  let from = 0
  for (let i = 0; i <= occurrence; i += 1) {
    const at = haystack.indexOf(needle, from)
    if (at === -1) throw new Error(`源码里找不到第 ${i} 处 ${JSON.stringify(needle)}`)
    if (i === occurrence) return at
    from = at + 1
  }
  return -1
}

/** 造一条 claim：偏移按**码点**算（服务端口径）。 */
function claimFor(md: string, sentence: string, occurrence = 0, shift = 0): UncitedClaim {
  const at = nthIndexOf(md, sentence, occurrence)
  const start = [...md.slice(0, at)].length + shift
  return { sentence, char_start: start, char_end: start + [...sentence].length }
}

interface Outcome {
  html: string
  report: ReturnType<typeof annotateTree>
  visible: string
}

function render(md: string, claims: UncitedClaim[]): Outcome {
  const processor = unified().use(remarkParse).use(remarkRehype).use(rehypeStringify)
  const tree = processor.parse(md) as unknown as MdastNode
  const report = annotateTree(tree, md, claims)
  const html = String(processor.stringify(processor.runSync(tree as never)))
  return { html, report, visible: visibleText(tree) }
}

/** 取出所有 `<span class="uncited">…</span>` 里的文本。 */
function spans(html: string): string[] {
  return [...html.matchAll(/<span class="uncited"[^>]*>(.*?)<\/span>/g)].map((m) => m[1])
}

// ---- 用例 -----------------------------------------------------------------

type Case = { name: string; why: string; run: () => void }
const cases: Case[] = []

function probe(name: string, why: string, run: () => void) {
  cases.push({ name, why, run })
}

// ① 基本：纯文本里的一句无依据的话
probe('基本标注', '偏移能切出原句时，那句话被 <span class="uncited"> 包住', () => {
  const md = '转专业需要满足条件一[1]。申请材料需要提前提交。'
  const outcome = render(md, [claimFor(md, '申请材料需要提前提交。')])

  assert.deepEqual(spans(outcome.html), ['申请材料需要提前提交。'])
  assert.equal(outcome.report.degraded, false)
  assert.equal(outcome.report.passedA, 1)
  assert.ok(outcome.html.includes(`title="${UNCITED_TITLE}"`), '提示文案必须留余地')
})

// ② 偏移指向第二处（重复句）：字符串查找会标错，偏移不会
probe('重复句子', '同一句话出现两次时，只有偏移指的那一处被标', () => {
  const md = '申请材料需要提前提交。中间的别的话[1]。申请材料需要提前提交。'
  const outcome = render(md, [claimFor(md, '申请材料需要提前提交。', 1)])

  const marked = spans(outcome.html)
  assert.equal(marked.length, 1)
  // 标的是**第二处**：后面紧跟着 </p>（也就是行尾那句）
  assert.ok(/<span class="uncited"[^>]*>申请材料需要提前提交。<\/span><\/p>$/.test(outcome.html))
})

// ③ 坑①：非 text 叶子不能被静默跳过
probe('坑① 非 text 叶子', 'inlineCode 也要进「可见文本」与标注范围', () => {
  const md = '结论甲[1]。请提交`身份证原件`和复印件。'
  const outcome = render(md, [claimFor(md, '请提交`身份证原件`和复印件。')])

  assert.ok(outcome.visible.includes('身份证原件'), '可见文本漏掉了 inlineCode')
  // 代码片段被整段覆盖 → 至少要有标注落到它身上（class 打在 code 上）
  assert.ok(
    outcome.html.includes('class="uncited"'),
    '整段覆盖的 inlineCode 一处都没标 —— 正是坑①描述的静默跳过',
  )
})

// ④ 坑②：value 与源码切片不等（inlineCode 的反引号）
probe('坑② value ≠ 切片', '按 value 重新定位，不拿源码偏移去索引 value', () => {
  const md = '结论乙[1]。请提交`身份证原件`。'
  const outcome = render(md, [claimFor(md, '请提交`身份证原件`。')])

  assert.ok(
    outcome.report.issues.some((issue) => issue.kind === 'relocated'),
    '没有报告「重新定位」—— 说明断言没生效',
  )
  assert.equal(outcome.report.annotated > 0, true, '重新定位之后仍应标上')
  assert.ok(!outcome.html.includes('class="uncited">`'), '反引号被包进标注里了（坑②原样复现）')
})

// ⑤ 坑③：容器节点的 position 含标记，不能用它包裹
probe('坑③ 容器节点', 'strong 的切片是 **30%**，标注只能落在叶子上', () => {
  const md = '结论丙[1]。合格线是 **30%** 以上。'
  const outcome = render(md, [claimFor(md, '合格线是 **30%** 以上。')])

  // 正确形态：span 落在 strong **里面**，`**` 标记不进 span
  assert.ok(
    /<strong><span class="uncited"[^>]*>30%<\/span><\/strong>/.test(outcome.html),
    '粗体里的文字没有被标注（只标了容器外的那段）',
  )
  const marked = spans(outcome.html)
  assert.ok(marked.includes('30%'), '粗体里的文字没有被标注')
  assert.ok(!marked.some((text) => text.includes('**')), '** 标记被包进标注里了（坑③原样复现）')
})

// ⑥ 星平面字符：码点偏移必须换算成 UTF-16 才切得对
probe('星平面字符', 'emoji 占 1 个码点、2 个 UTF-16 单元，偏移不能漂', () => {
  const md = '📚资料显示甲[1]。申请材料需要提前提交。'
  const outcome = render(md, [claimFor(md, '申请材料需要提前提交。')])

  assert.deepEqual(spans(outcome.html), ['申请材料需要提前提交。'])
  assert.equal(outcome.report.degraded, false)
})

// ⑦ 校验 A：偏移漂移必须检出（这是它存在的意义）
probe('校验 A 漂移 ±1', '偏移错一位 → 检出、整条不标（宁可不标）', () => {
  const md = '转专业需要满足条件一[1]。申请材料需要提前提交。'
  for (const shift of [1, -1, 2, -2]) {
    const claim = claimFor(md, '申请材料需要提前提交。', 0, shift)
    const failed = checkClaims(md, [claim])
    assert.equal(failed.length, 1, `偏移 ${shift >= 0 ? '+' : ''}${shift} 没被检出`)

    const outcome = render(md, [claim])
    assert.equal(outcome.report.degraded, true, '检出漂移后必须降级')
    assert.equal(spans(outcome.html).length, 0, '漂移时一处都不许标 —— 标歪比不标更糟')
  }
})

// ⑧ 校验 B：标注自己不能吃掉字符
probe('校验 B 自检', '标注前后可见文本逐字相同', () => {
  const md = '结论甲[1]。申请材料需要提前提交。\n\n第二段也有话[2]。'
  const claims = [claimFor(md, '申请材料需要提前提交。'), claimFor(md, '第二段也有话[2]。')]
  const processor = unified().use(remarkParse).use(remarkRehype).use(rehypeStringify)
  const tree = processor.parse(md) as unknown as MdastNode
  const before = visibleText(tree)
  const report = annotateTree(tree, md, claims)

  assert.equal(visibleText(tree), before, '标注改动了可见文本')
  assert.ok(!report.issues.some((issue) => issue.kind === 'validation_b'))
  assert.equal(spans(String(processor.stringify(processor.runSync(tree as never)))).length, 2)
})

// ⑨ 部分覆盖的非 text 叶子：上报，不硬标
probe('部分覆盖不硬标', '代码片段切不开 → 上报 partial_leaf，其余部分照标', () => {
  const md = '结论甲[1]。请提交`身份证原件`即可。'
  // 这条「句子」故意停在代码片段中间：偏移落在 `身份` 之后
  const codeAt = md.indexOf('身份证原件')
  const cpAt = [...md.slice(0, codeAt)].length
  // cpAt 是 `身` 的偏移：往前 4 个字是「请提交」（再往前一个是反引号）
  const partial = { sentence: '请提交`身份', char_start: cpAt - 4, char_end: cpAt + 2 }
  const outcome = render(md, [partial])

  assert.ok(
    outcome.report.issues.some((issue) => issue.kind === 'partial_leaf'),
    '部分覆盖没被上报',
  )
  assert.ok(outcome.html.includes('<code>身份证原件</code>'), '代码片段被切开了')
  assert.ok(spans(outcome.html).includes('请提交'), '代码之外的那半句仍然该标')
})

// ⑩ 端到端形状：标记与提示都在
probe('产出形状', 'span 带 class 与 title，且句子逐字一致', () => {
  const md = '结论甲[1]。申请材料需要提前提交。'
  const outcome = render(md, [claimFor(md, '申请材料需要提前提交。')])
  assert.ok(outcome.html.includes('class="uncited"'))
  assert.ok(outcome.html.includes(`title="${UNCITED_TITLE}"`))
  assert.deepEqual(spans(outcome.html), ['申请材料需要提前提交。'])
})

// ⑪ 两个插件共用一条管道（**顺序不能反**）
probe('角标 + 置灰共用管道', '含 [n] 的段落里，无依据句照样标得上', () => {
  const md = '结论甲[1]。另起一句[2]。资料中未找到关于黄色预警具体后果的进一步规定。'
  const claim = claimFor(md, '资料中未找到关于黄色预警具体后果的进一步规定。')
  const processor = unified()
    .use(remarkParse)
    .use(remarkUncited, { rawText: md, claims: [claim] })
    .use(remarkCitations)
    .use(remarkRehype)
    .use(rehypeStringify)
  const tree = processor.parse(md) as unknown as MdastNode
  const html = String(processor.stringify(processor.runSync(tree as never)))

  assert.deepEqual(spans(html), ['资料中未找到关于黄色预警具体后果的进一步规定。'],
    '角标插件把文本节点拆了之后，置灰就再也标不上了（顺序反了）')
  assert.ok(html.includes('cite-badge') || html.includes('#cite-1'), '角标节点没产出')
})

// ---- 跑 -------------------------------------------------------------------

let failed = 0
for (const item of cases) {
  try {
    item.run()
    console.log(`  ✅ ${item.name} —— ${item.why}`)
  } catch (error) {
    failed += 1
    console.log(`  ❌ ${item.name} —— ${item.why}`)
    console.log(`     ${(error as Error).message.split('\n')[0]}`)
  }
}

console.log(`\n${cases.length - failed}/${cases.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
