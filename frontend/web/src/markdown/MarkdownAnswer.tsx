/**
 * 答案渲染：Markdown 管道 + 角标 + 置灰标注（§4.2.1 / §4.2.3）。
 *
 * ⚠️ **管道共用，插件按用途分开注入**（§4.2.2.6）：
 *    文档预览走同一条基础管道，但**不能**注入答案专用插件 ——
 *    否则文档正文里的 `[1]` 会被误变成可点角标。
 *
 * ⚠️ **不要引入 sanitize**：`rehype-sanitize` 的默认 schema 会剥掉 `class`，
 *    `<span class="uncited">` 会退化成 `<span>`，置灰**静默失效**。
 *    react-markdown 默认不渲染裸 HTML，也不带 sanitize —— 保持默认即可。
 *
 * 降级规则（§4.2.1.4）：校验不过 → **退回消息级徽标**，不硬标。
 * 「误标一句的伤害大于漏标一句」。
 */

import { memo, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Citation, VerifyReport } from '../api/types'
import { parseCiteHref, remarkCitations } from './citationPlugin'
import { UNCITED_TITLE, checkClaims, remarkUncited } from './uncitedAnnotation'

interface Props {
  /** 渲染前的**原始答案文本** —— 偏移的参照系就是它，不能传渲染后的 */
  text: string
  citations?: Citation[]
  verify?: VerifyReport | null
  /** 点角标 → 打开原文抽屉（第三层入口） */
  onCitationClick?: (citation: Citation) => void
}

/** `remarkPlugins` 的元素类型：插件 + 选项元组。 */
type RemarkPlugins = NonNullable<Parameters<typeof ReactMarkdown>[0]['remarkPlugins']>

export const MarkdownAnswer = memo(function MarkdownAnswer({
  text, citations = [], verify, onCitationClick,
}: Props) {
  const claims = verify?.uncited_claims ?? []

  // 校验 A（真正的漂移检测）：偏移切不出原句 → 整条消息不标灰
  const drifted = useMemo(() => checkClaims(text, claims).length > 0, [text, claims])

  const byMarker = useMemo(() => {
    const map = new Map<number, Citation>()
    for (const citation of citations) map.set(citation.marker, citation)
    return map
  }, [citations])

  const invalid = useMemo(
    () => new Set((verify?.invalid_markers ?? []).map((item) => item.marker)),
    [verify],
  )

  // ⚠️ **顺序不能反**：置灰标注依赖每个叶子自带的 mdast position，
  //    而角标插件会把含 `[n]` 的文本节点拆成新节点（新节点没有 position）。
  //    先角标后置灰 → 被拆过的那一段永远标不上（实测踩过：
  //    摘要显示「含 1 处未证实内容」，正文里一个灰标都没有）。
  const plugins = useMemo<RemarkPlugins>(() => {
    const list: unknown[] = [remarkGfm]
    if (claims.length > 0 && !drifted) list.push([remarkUncited, { rawText: text, claims }])
    list.push(remarkCitations)
    return list as RemarkPlugins
  }, [claims, drifted, text])

  const components = useMemo(() => ({
    a: ({ href, children }: { href?: string; children?: React.ReactNode }) => {
      const marker = parseCiteHref(href)
      if (marker === null) return <a href={href}>{children}</a>
      const citation = byMarker.get(marker)
      return (
        <button
          type="button"
          className={`cite-badge${invalid.has(marker) ? ' invalid' : ''}`}
          title={
            invalid.has(marker)
              ? `引用了不存在的证据 [${marker}]`
              : citation
                ? `${citation.document_name}${citation.chapter ? ` · ${citation.chapter}` : ''} · 第 ${citation.page} 页\n${citation.snippet}`
                : `引用 [${marker}]`
          }
          onClick={() => citation && onCitationClick?.(citation)}
        >
          {children}
        </button>
      )
    },
  }), [byMarker, invalid, onCitationClick])

  return (
    <div className="answer">
      <ReactMarkdown remarkPlugins={plugins} components={components}>
        {text}
      </ReactMarkdown>

      {claims.length > 0 && (
        <div className="uncited-summary" title={UNCITED_TITLE}>
          {drifted ? (
            // 校验 A 没过：偏移已经不可信 —— 只报数量与原文，不指位置
            <>
              本回答含 {claims.length} 处未证实内容（定位校验未通过，未标注位置）
              <ul className="uncited-list">
                {claims.map((claim) => <li key={claim.char_start}>{claim.sentence}</li>)}
              </ul>
            </>
          ) : (
            <>本回答含 {claims.length} 处未证实内容</>
          )}
        </div>
      )}
    </div>
  )
})
