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
 * ⚠️ 渲染**直接用 pdf.js**，不再用 `react-pdf-highlighter`。
 *    原先那套（`PdfHighlighter` + `PdfLoader`）实测有一串治不好的毛病：
 *      · pdf.js 的 `PDFViewer.setDocument()` 只在**容器里已经有文档**时才
 *        `_resetView()` 清空容器，而库每次都在全新 viewer 上调它 —— 容器从来不清，
 *        StrictMode 把挂载跑两遍就**每页渲染两遍**（实测 20 页 → 40 个 `.page`）；
 *      · 库又给每一页各建一个 `createRoot` 画高亮层，和 React 的挂载/卸载交错，
 *        实测持续报「同一容器二次 createRoot」「render 期间同步卸载 root」，
 *        而它**不弹到界面上** —— 症状是整份文本层缺失、页面只画出一页、其余空白，
 *        且时好时坏（撞不撞上取决于 StrictMode 的时序）。
 *    用最小探针（只用 pdf.js，不经过库、不经过 React）验证过：取文件、载入、
 *    渲染到 canvas、取文本层、坐标映射**全部正常**。所以问题在库的 React 集成层，
 *    而这里要的只是「显示引用的那一页 + 叠高亮框」—— 一个完整翻页阅读器是杀鸡用牛刀。
 *
 *    顺带的好处：不再需要挂载前清容器、不再需要和 StrictMode 抢 DOM，
 *    整个「容器生命周期」问题类别直接消失。
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as pdfjs from 'pdfjs-dist'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { Minus, Plus, X } from 'lucide-react'
import { request, requestBlob } from '../api/client'
import type { Box, Citation } from '../api/types'

// ⚠️ 默认 worker 是 CDN 地址，取不到时会**静默退回主线程假 worker** —— 必须显式指本地
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

type Level = 'L1' | 'L2' | 'L3' | 'L4'

interface Located {
  level: Level
  page: number
  note: string
  /** PDF 用户空间坐标（左下角原点） */
  rects: Array<{ x1: number; y1: number; x2: number; y2: number }>
}

/** 相对页面尺寸的比例框（0-1）—— 用比例而不是像素，缩放时不必重算 */
interface Ratio extends Record<string, number> {
  left: number; top: number; width: number; height: number
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
  const pageNumber = Math.max(1, target?.page ?? 1)
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [located, setLocated] = useState<Located | null>(null)
  const [zoom, setZoom] = useState(1)
  // 实测出来的页面比例基准（1:1 视口下的宽 / 高）——**不用 A4 去猜**，
  // 这份文档的第 1 页就是非标准尺寸（595×358pt），猜了高亮框会整体偏
  const [pageSize, setPageSize] = useState<{ w: number; h: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    let loaded: PDFDocumentProxy | null = null
    setPdf(null); setError(null); setLocated(null)

    void (async () => {
      try {
        const blob = await requestBlob(
          `/documents/${documentId}/file${includeRestricted ? '?include_restricted=true' : ''}`,
        )
        if (cancelled) return
        // 用 arrayBuffer 而不是 blob URL：这里自己管生命周期，不需要 revoke 那套
        const doc = await pdfjs.getDocument({ data: await blob.arrayBuffer() }).promise
        if (cancelled) { void doc.destroy(); return }
        loaded = doc
        setPdf(doc)
      } catch (e) {
        if (!cancelled) setError((e as Error).message || '原文打开失败')
      }
    })()

    return () => {
      cancelled = true
      void loaded?.destroy()
    }
  }, [documentId, includeRestricted])

  // 定位（四级）—— pdf 就绪后跑一次
  useEffect(() => {
    if (!pdf) return
    let cancelled = false
    void (async () => {
      const result = await locate(pdf, citation)
      if (cancelled) return
      setLocated(result)
      const vp = (await pdf.getPage(result.page)).getViewport({ scale: 1 })
      if (!cancelled) setPageSize({ w: vp.width, h: vp.height })
    })()
    return () => { cancelled = true }
  }, [pdf, citation])

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
    // ⚠️ 抽屉是 **flex 兄弟节点**，不再是 `position: fixed` 的浮层 ——
    //    浮层会盖住对话区，而这里要的是「把对话区挤窄」（推拉式）。
    <aside className="drawer" role="dialog" aria-label="原文">
      <header className="drawer-head">
        <div className="drawer-title">
          <strong>{citation.document_name}</strong>
          <div className="muted small">
            第 {citation.page} 页{citation.chapter ? ` · ${citation.chapter}` : ''}
            {citation.escalated && <span className="escalated">（越权查看）</span>}
          </div>
        </div>
        <div className="drawer-actions">
          <button className="icon-btn" onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}
                  title="缩小" aria-label="缩小"><Minus size={16} /></button>
          <span className="muted small zoom-value">{Math.round(zoom * 100)}%</span>
          <button className="icon-btn" onClick={() => setZoom((z) => Math.min(3, z + 0.25))}
                  title="放大" aria-label="放大"><Plus size={16} /></button>
          <button className="link small" onClick={() => void openInNewTab()}>下载原文</button>
          <button className="icon-btn" onClick={onClose} title="关闭（Esc）" aria-label="关闭">
            <X size={17} />
          </button>
        </div>
      </header>

      {located && (
        <div className={`locate-note level-${located.level}`} title={located.note}>
          {located.level === 'L4' ? '未能精确定位（已跳到该页）' : `定位方式：${located.note}`}
        </div>
      )}
      {error && <div className="drawer-error">{error}</div>}

      <div className="drawer-body">
        {pdf && pageSize && located ? (
          <PageCanvas pdf={pdf} pageNumber={located.page}
                      rects={located.rects} zoom={zoom} pageSize={pageSize} />
        ) : (
          !error && <div className="muted small pdf-loading">加载原文…</div>
        )}
      </div>
    </aside>
  )
}

// ---- 单页渲染 + 高亮叠加 ---------------------------------------------------

/**
 * 把**目标页**画进 canvas，按**比例**叠加高亮框。
 *
 * 高亮用百分比定位（相对画布容器），所以缩放/窗口变化只需要重画 canvas，
 * 框会自己跟着走 —— 不必重算每个框的像素值。
 */
function PageCanvas({ pdf, pageNumber, rects, zoom, pageSize }: {
  pdf: PDFDocumentProxy
  pageNumber: number
  rects: Located['rects']
  zoom: number
  pageSize: { w: number; h: number }
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [hostWidth, setHostWidth] = useState(0)

  // 容器宽度 → 决定渲染分辨率（跟随推拉动画与窗口变化）
  //
  // ⚠️ **必须直接量 getBoundingClientRect，不能只信 ResizeObserver 给的值**。
  //    实测踩到：只挂 RO 时回调要么没来、要么给的宽度是 0，`hostWidth` 永远是 0，
  //    渲染 effect 直接 early-return —— 结果 canvas 保持默认的 300×150，
  //    肉眼看着就是「预览一片空白」，而高亮框还在（它们是按比例定位的）。
  //    先同步量一次，RO 只用来触发「再量一次」。
  // ⚠️ 用 useLayoutEffect 而不是 useEffect：要在浏览器绘制**之前**把尺寸量出来。
  //    实测：放在 useEffect 里时，首次渲染量到的是抽屉展开动画前/布局未稳定时的宽度，
  //    于是第一次打开会留一个默认尺寸（300×150）的空 canvas。
  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    let last = 0
    const measure = () => {
      const w = Math.round(host.getBoundingClientRect().width)
      if (w > 0 && w !== last) { last = w; setHostWidth(w) }
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(host)
    return () => ro.disconnect()
  }, [])

  // 比例框：PDF 用户空间（左下原点、pt）→ 0-1 比例（左上原点）。
  // ⚠️ 用**实测的**页宽页高（同一页的 viewport），不要按 A4 猜 —— 这份文档第 1 页
  //    就是 595×358pt 的非标准页，猜出来的框会整体偏离。
  const boxes: Ratio[] = useMemo(() => rects.map((r) => ({
    left: r.x1 / pageSize.w,
    top: 1 - r.y2 / pageSize.h,
    width: (r.x2 - r.x1) / pageSize.w,
    height: (r.y2 - r.y1) / pageSize.h,
  })), [rects, pageSize])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || hostWidth <= 0) return
    let cancelled = false
    let task: { cancel: () => void } | null = null

    void (async () => {
      const page = await pdf.getPage(pageNumber)
      if (cancelled) return
      const base = page.getViewport({ scale: 1 })
      // 页面 CSS 宽 = 容器宽 × 缩放；渲染分辨率再乘 dpr
      const cssWidth = hostWidth * zoom
      const scale = cssWidth / base.width
      const vp = page.getViewport({ scale })
      const dpr = window.devicePixelRatio || 1
      canvas.width = Math.floor(vp.width * dpr)
      canvas.height = Math.floor(vp.height * dpr)
      canvas.style.width = `${vp.width}px`
      canvas.style.height = `${vp.height}px`
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.scale(dpr, dpr)
      task = page.render({ canvasContext: ctx, viewport: vp })
      await (task as unknown as { promise: Promise<void> }).promise
    })()

    return () => { cancelled = true; task?.cancel() }
  }, [pdf, pageNumber, hostWidth, zoom])

  // 容器占位比例：用实测页尺寸，避免 canvas 渲染完成前高度塌成 0
  const aspect = pageSize.h / pageSize.w

  return (
    <div className="pdf-scroll">
      <div className="pdf-page" ref={hostRef}
           style={{ width: `${zoom * 100}%`, aspectRatio: `1 / ${aspect}` }}>
        <canvas ref={canvasRef} className="pdf-canvas" />
        {boxes.map((b, i) => (
          <span key={i} className="pdf-highlight" style={{
            left: `${b.left * 100}%`, top: `${b.top * 100}%`,
            width: `${b.width * 100}%`, height: `${b.height * 100}%`,
          }} />
        ))}
      </div>
    </div>
  )
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
