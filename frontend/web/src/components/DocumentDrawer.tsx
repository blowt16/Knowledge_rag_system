/**
 * 原文抽屉 —— 引用回跳的第三层入口（§4.2.2）。
 *
 * ```
 * ① 取 jump_target.document_id → GET /api/documents/{id}/file，打开抽屉
 * ② 跳到 jump_target.page
 * ③ L1 用 jump_target.boxes 直接按坐标高亮        ← 主路径
 *    bbox 缺失/越界 ↓
 *    L2 用 citation.snippet 在该页文本层检索匹配
 *    匹配失败 ↓
 *    L3 用 citation.chapter 匹配章节标题
 *    仍失败 ↓
 *    L4 只跳到该页，不高亮，提示「未能精确定位」
 * ```
 *
 * ⚠️ **四级都要实现**（§4.2.2.4）：L4 是常态兜底，不能假设前三级一定成功 ——
 *    非 PDF / 扫描件 / 旧索引的 `boxes` 都是空数组。
 *
 * ⚠️ **偏移不做高亮**：规范化文本的偏移**无法直接**映射到阅读器坐标
 *    （清洗会删页眉页脚，两者坐标系对不上）。所以「坐标优先、文本兜底」。
 *
 * ⚠️ 两个实测坑（spike 记录）：
 *    ① highlights 必须在**挂载之前**备齐 —— 挂载后再喂会读到一个还没初始化的
 *       viewer，直接崩
 *    ② 库的默认 worker 是 CDN 地址，取不到时**静默退回主线程假 worker**，
 *       页面照常渲染但只有翻 performance 列表才看得出来 —— 必须显式指本地 worker
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { Highlight, PdfHighlighter, PdfLoader } from 'react-pdf-highlighter'
import type { IHighlight } from 'react-pdf-highlighter'
import 'react-pdf-highlighter/dist/style.css'
import { request, requestBlob } from '../api/client'
import type { Box, Citation } from '../api/types'

type Level = 'L1' | 'L2' | 'L3' | 'L4'

interface Located {
  level: Level
  page: number
  note: string
  /** PDF 用户空间坐标（左下角原点） */
  rects: Array<{ x1: number; y1: number; x2: number; y2: number }>
}

export interface DrawerProps {
  citation: Citation
  /** admin 提权时带上（非 admin 传了会被后端按未传处理） */
  includeRestricted?: boolean
  onClose: () => void
}

export function DocumentDrawer({ citation, includeRestricted = false, onClose }: DrawerProps) {
  const target = citation.jump_target
  const documentId = target?.document_id ?? ''
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [located, setLocated] = useState<Located | null>(null)

  useEffect(() => {
    let revoked: string | null = null
    let cancelled = false
    setBlobUrl(null); setError(null); setLocated(null)

    void (async () => {
      try {
        const blob = await requestBlob(
          `/documents/${documentId}/file${includeRestricted ? '?include_restricted=true' : ''}`,
        )
        if (cancelled) return
        revoked = URL.createObjectURL(blob)
        setBlobUrl(revoked)
      } catch (e) {
        if (!cancelled) setError((e as Error).message || '原文打开失败')
      }
    })()

    return () => {
      cancelled = true
      if (revoked) URL.revokeObjectURL(revoked)
    }
  }, [documentId, includeRestricted])

  // Esc 关闭（§4.2.2.1）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const openInNewTab = async () => {
    // pptx 等不做在线预览的格式：给「下载原文 + 提示」（§4.2.2.6）
    // ⚠️ 提权标记要跟抽屉里那条路径**一致** —— 漏了的话，
    //    admin 在抽屉里看得到、点下载却 404
    const blob = await requestBlob(
      `/documents/${documentId}/file${includeRestricted ? '?include_restricted=true' : ''}`,
    )
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }

  return (
    <aside className="drawer" role="dialog" aria-label="原文">
      <header className="drawer-head">
        <div>
          <strong>{citation.document_name}</strong>
          <div className="muted small">
            第 {citation.page} 页{citation.chapter ? ` · ${citation.chapter}` : ''}
            {citation.escalated && <span className="escalated">（越权查看）</span>}
          </div>
        </div>
        <div className="drawer-actions">
          <button className="link" onClick={() => void openInNewTab()}>下载原文</button>
          <button className="link" onClick={onClose}>关闭</button>
        </div>
      </header>

      {located && (
        <div className={`locate-note level-${located.level}`} title={located.note}>
          {located.level === 'L4' ? '未能精确定位（已跳到该页）' : `定位方式：${located.note}`}
        </div>
      )}
      {error && <div className="drawer-error">{error}</div>}

      <div className="drawer-body">
        {blobUrl && (
          <PdfLoader url={blobUrl} workerSrc={workerSrc}
                     beforeLoad={<div className="muted small">加载原文…</div>}>
            {(pdfDocument) => (
              <LocatedPdf pdfDocument={pdfDocument} citation={citation}
                          onLocated={setLocated} />
            )}
          </PdfLoader>
        )}
      </div>
    </aside>
  )
}

// ---- PDF 渲染 + 四级定位 ---------------------------------------------------

function LocatedPdf({ pdfDocument, citation, onLocated }: {
  pdfDocument: PDFDocumentProxy
  citation: Citation
  onLocated: (located: Located | null) => void
}) {
  const [highlights, setHighlights] = useState<IHighlight[]>([])
  const [ready, setReady] = useState(false)
  const scrollTo = useRef<((highlight: IHighlight) => void) | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const located = await locate(pdfDocument, citation)
      if (cancelled) return

      const highlight: IHighlight | null = located.rects.length
        ? {
            id: `cite-${citation.marker}`,
            position: {
              pageNumber: located.page,
              boundingRect: rect(located.rects[0], located.page),
              rects: located.rects.map((r) => rect(r, located.page)),
              usePdfCoordinates: true,
            },
            content: { text: citation.snippet },
            comment: { text: '', emoji: '' },
          }
        : null

      setHighlights(highlight ? [highlight] : [])
      setReady(true)
      onLocated(located)
      // 跳到目标页（L4 也跳）。页面是异步渲染的，布局稳定前后各滚一次，
      // 否则会停在「第一次滚动时还没渲染完」的位置上。
      if (highlight) {
        for (const delay of [150, 700]) {
          setTimeout(() => scrollTo.current?.(highlight), delay)
        }
      }
    })()
    return () => { cancelled = true }
  }, [pdfDocument, citation, onLocated])

  // ⚠️ 必须等高亮备齐再挂载（见文件头「两个实测坑」①）
  if (!ready) return <div className="muted small">正在定位…</div>

  return (
    <PdfHighlighter
      pdfDocument={pdfDocument}
      highlights={highlights}
      pdfScaleValue="auto"
      enableAreaSelection={() => false}
      onSelectionFinished={() => null}
      onScrollChange={() => {}}
      scrollRef={(fn) => { scrollTo.current = fn }}
      highlightTransform={(highlight, _index, _setTip, _hideTip, _v2s, _shot, isScrolledTo) => (
        <Highlight isScrolledTo={isScrolledTo} position={highlight.position}
                   comment={highlight.comment} />
      )}
    />
  )
}

function rect(r: { x1: number; y1: number; x2: number; y2: number }, page: number) {
  return { x1: r.x1, y1: r.y1, x2: r.x2, y2: r.y2, width: 0, height: 0, pageNumber: page }
}

/** 把 PyMuPDF 的框（左上角原点、y 向下）换成 PDF 用户空间（左下角原点、y 向上）。 */
function boxToPdf(box: Box, pageHeight: number) {
  return { x1: box.x0, y1: pageHeight - box.bottom, x2: box.x1, y2: pageHeight - box.top }
}

/** 四级定位（§4.2.2.4）。**每一级都可能失败，L4 是常态兜底**。 */
export async function locate(pdfDocument: PDFDocumentProxy, citation: Citation): Promise<Located> {
  const target = citation.jump_target
  const page = Math.max(1, target?.page ?? 1)
  const boxes = (target?.boxes ?? []).filter((box) => box.page === page)

  // L1：坐标（主路径）
  if (boxes.length > 0) {
    const viewport = (await pdfDocument.getPage(page)).getViewport({ scale: 1 })
    // ⚠️ 必须按**阅读顺序**排（top 小的在前）：第一个框会被当成滚动锚点，
    //    而 Chroma 里存的 bbox 第一项往往是页脚（页码行）。
    //    锚在页脚上会把整页往上滚过去，高亮全跑到视口外面 —— 实测踩过。
    const ordered = [...boxes].sort((a, b) => a.top - b.top)
    return {
      level: 'L1', page,
      note: `坐标高亮（${boxes.length} 个框）`,
      rects: ordered.map((box) => boxToPdf(box, viewport.height)),
    }
  }

  // L2：该页文本层里找 snippet
  const probe = (citation.snippet ?? '').replace(/\s+/g, '').slice(0, 10)
  if (probe) {
    const rects = await findTextRects(pdfDocument, page, probe)
    if (rects.length > 0) {
      return { level: 'L2', page, note: `文本匹配「${probe}…」`, rects }
    }
  }

  // L3：章节标题（可能不在同一页 —— 往后翻找）
  const chapter = (citation.chapter ?? '').trim()
  if (chapter) {
    for (let candidate = page; candidate <= pdfDocument.numPages; candidate += 1) {
      const rects = await findTextRects(pdfDocument, candidate, chapter.replace(/\s+/g, ''))
      if (rects.length > 0) {
        return { level: 'L3', page: candidate, note: `章节匹配「${chapter}」`, rects }
      }
      if (candidate - page >= 20) break // 别为一条引用翻遍整份文档
    }
  }

  // L4：只跳页
  return { level: 'L4', page, note: '四级定位都未命中', rects: [] }
}

/** 在某一页的文本层里找 needle，返回命中项的坐标（PDF 用户空间）。 */
async function findTextRects(pdfDocument: PDFDocumentProxy, pageNumber: number, needle: string) {
  if (pageNumber > pdfDocument.numPages) return []
  const page = await pdfDocument.getPage(pageNumber)
  const content = await page.getTextContent()

  const items = content.items as Array<{ str: string; transform: number[]; width: number; height: number }>
  let text = ''
  const spans: Array<{ start: number; end: number; item: typeof items[number] }> = []
  for (const item of items) {
    spans.push({ start: text.length, end: text.length + item.str.length, item })
    text += item.str
  }
  // 换行/空格在 PDF 文本层里是分项的 —— 去掉空白再找
  const flat = text.replace(/\s+/g, '')
  if (!flat.includes(needle)) return []
  const hitAt = flat.indexOf(needle)

  // 把「去空白后的下标」映射回原文下标，再取相交的项
  const map: number[] = []
  text.split('').forEach((ch, index) => { if (!/\s/.test(ch)) map.push(index) })
  const from = map[hitAt]
  const to = map[Math.min(hitAt + needle.length - 1, map.length - 1)]

  return spans
    .filter((span) => span.start <= to && span.end > from)
    .map((span) => {
      const item = span.item
      const x = item.transform[4]
      const y = item.transform[5]
      const height = item.height || Math.abs(item.transform[3]) || 10
      return { x1: x, y1: y, x2: x + (item.width || 10), y2: y + height }
    })
}

// ---- 图片缩略图（§4.2.3.3）-------------------------------------------------

/**
 * 按**文件名**现取签名 URL。
 *
 * ⚠️ `images` 里存的是文件名不是 URL：citations 会落库，存 URL 的话
 *    刷新历史会话时图片全部裂图（签名 5 分钟就过期）。
 */
export function CitationImages({ documentId, names }: { documentId: string; names: string[] }) {
  const [urls, setUrls] = useState<Record<string, string>>({})

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const next: Record<string, string> = {}
      for (const name of names) {
        try {
          const data = await request<{ url: string }>(
            `/documents/${documentId}/images/${encodeURIComponent(name)}`,
          )
          next[name] = data.url
        } catch {
          /* 取不到就少一张图，不影响别的 */
        }
      }
      if (!cancelled) setUrls(next)
    })()
    return () => { cancelled = true }
  }, [documentId, names])

  const ready = useMemo(() => names.filter((name) => urls[name]), [names, urls])
  if (ready.length === 0) return null

  return (
    <div className="thumbs">
      {ready.map((name) => (
        <img key={name} src={urls[name]} alt={name} loading="lazy" />
      ))}
    </div>
  )
}
