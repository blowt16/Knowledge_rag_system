/**
 * 流式答案渲染（§4.2.4.1）。
 *
 * **流式进行中**：按「已完结块」切分（`blocks.ts`），每块 `memo` 住只渲染一次，
 * 每个 token 只重渲染尾部那一块 —— 不再每 token 全量重解析。
 *
 * **流式结束后**：整段交回 `MarkdownAnswer` 渲染一次。
 *
 * ⚠️ 为什么结束后要换回整段渲染，而不是继续按块渲染：
 *   置灰标注的 `uncited_claims.char_start/end` 是**相对整段原始答案**的偏移
 *   与**码点**单位（§4.2.1.2）。按块渲染就得把这些偏移逐块平移，
 *   而那个映射一旦错一格，页面只是「标歪了」，肉眼很难发现。
 *   流式期间 `verify` 根本还没到（它在 token 流之后才发），所以按块渲染时
 *   压根没有偏移可错 —— 两段各用各的最稳的做法。
 *   代价：答案收尾时多一次全量解析。一次，不是每个 token 一次。
 */
import { memo, useMemo } from 'react'

import { MarkdownAnswer } from './MarkdownAnswer'
import { splitBlocks } from './blocks'
import type { Citation, VerifyReport } from '../api/types'

/** 已完结块：props 不变就不重渲染 —— 这是「只渲染一次」的落点。 */
const ClosedBlock = memo(function ClosedBlock({
  text, onCitationClick,
}: {
  text: string
  onCitationClick?: (citation: Citation) => void
}) {
  // 不传 citations / verify：流式期间它们都还没到（citations 也在流末尾才发），
  // 传空数组等价，但明确写出来免得日后有人以为漏了
  return <MarkdownAnswer text={text} citations={[]} onCitationClick={onCitationClick} />
})

export function StreamingAnswer({
  text, citations, verify, onCitationClick,
}: {
  text: string
  citations?: Citation[]
  verify?: VerifyReport | null
  onCitationClick?: (citation: Citation) => void
}) {
  const { closed, tail } = useMemo(() => splitBlocks(text), [text])

  return (
    <>
      {closed.map((block) => (
        <ClosedBlock key={block.start} text={block.text}
                     onCitationClick={onCitationClick} />
      ))}
      {tail.text && (
        <MarkdownAnswer text={tail.text} citations={citations} verify={verify}
                        onCitationClick={onCitationClick} />
      )}
    </>
  )
}
