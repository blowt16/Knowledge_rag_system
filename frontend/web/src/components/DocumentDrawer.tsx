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

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as pdfjs from 'pdfjs-dist'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { Minus, Plus, X } from 'lucide-react'
import { request, requestBlob } from '../api/client'
import type { Box, Citation } from '../api/types'

// ⚠️ 默认 worker 是 CDN 地址，取不到时会**静默退回主线程假 worker** —— 必须显式指本地
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

/** 抽屉宽度（px）与钳制范围 —— 拖拽分栏用，存 localStorage 记住用户选择。 */
const MIN_WIDTH = 360
/** 对话区至少留这么多，别让用户把对话区拖没了 */
const CHAT_MIN_WIDTH = 420
const WIDTH_KEY = 'campus-rag-drawer-width'

/** 钳到 [MIN_WIDTH, 窗口宽 − 对话区保底] —— 拖拽、读存档、窗口变化都走这一个口子。 */
function clampWidth(px: number): number {
  const max = Math.max(MIN_WIDTH, Math.round(window.innerWidth - CHAT_MIN_WIDTH))
  return Math.min(max, Math.max(MIN_WIDTH, Math.round(px)))
}

function readSavedWidth(): number {
  // ⚠️ 包 try：隐私模式 / 禁用存储时 localStorage 会直接抛
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY))
    if (Number.isFinite(raw) && raw > 0) return clampWidth(raw)
  } catch { /* 取不到就用默认 */ }
  return clampWidth(Math.min(720, window.innerWidth * 0.46))
}

function saveWidth(px: number): void {
  try { localStorage.setItem(WIDTH_KEY, String(px)) } catch { /* 存不了就存不了 */ }
}

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
  // 拖拽分栏
  const [width, setWidth] = useState(() => readSavedWidth())
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

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
      if (!cancelled) setLocated(result)
    })()
    return () => { cancelled = true }
  }, [pdf, citation])

  // 页面尺寸由渲染层量完回报（那里本来就为撑高度逐页量过一遍，不重复量）
  const handlePageSize = useCallback(
    (size: { w: number; h: number }) => setPageSize(size),
    [],
  )

  // Esc 关闭（§4.2.2.1）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // ---- 拖拽分栏：宽度由用户定，记住选择 ----
  //
  // ⚠️ 抽屉贴在**右边缘**，所以往左拖是变宽：新宽 = 起始宽 + (起点 x − 当前 x)。
  //    写成「加上差值」是最容易搞反的一步（第一版就反了）。
  //
  // ⚠️ 当前宽度除了 state 还存一份 ref：`onDragEnd` 里读 `width` 会读到
  //    **闭包生成那一刻的旧值**（拖完存进去的还是拖动前的宽度 —— 实测到过）。
  const widthRef = useRef(width)
  widthRef.current = width

  // 窗口变窄时把抽屉收回来，别把对话区挤没了（只加不减的话，在宽屏拖大、
  // 换到窄屏就再也回不来）
  useEffect(() => {
    const onResize = () => {
      setWidth((w) => clampWidth(w))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onDragStart = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    // ⚠️ 指针捕获只是「拖出元素范围后仍能收到事件」的**优化**，不是正确性的前提 ——
    //    所以先立好拖动状态，捕获再单独包 try。它对「不是活动指针」的 id 会抛，
    //    抛在 handler 里会把后面整段带崩，而拖动本身其实还能正常走。
    dragRef.current = { startX: event.clientX, startWidth: width }
    setDragging(true)
    try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* 见上 */ }
  }

  const onDragMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    const next = clampWidth(drag.startWidth + (drag.startX - event.clientX))
    // ⚠️ 这里必须**同步**写 ref，不能只靠渲染时那句 `widthRef.current = width`：
    //    React 会把这些 pointer 事件里的 setState 一起批处理，拖动全程可能不发生
    //    渲染 —— 于是 onDragEnd 读到的还是拖动**之前**的宽度，存进 localStorage 的
    //    也就是旧值（实测：拖到 880 却存了 720，下次打开又弹回去）。
    widthRef.current = next
    setWidth(next)
  }

  const onDragEnd = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return
    dragRef.current = null
    setDragging(false)
    // 这里同理包 try：没捕获成功时 release 会抛「不是活动指针」
    try { event.currentTarget.releasePointerCapture(event.pointerId) } catch { /* 没捕获过就算了 */ }
    saveWidth(widthRef.current) // ⚠️ 用 ref，不是闭包里的 width
  }

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
    //    ⚠️ 宽度由用户拖拽决定（flex-basis + flex:none），所以这里要显式给宽度，
    //       不能再由 CSS 里的 width 规则说了算。
    <aside className={`drawer${dragging ? ' dragging' : ''}`} role="dialog" aria-label="原文"
           style={{ flexBasis: width, width }}>
      {/* 拖拽分隔条：贴左边缘 —— 往左拖 = 预览变宽 */}
      <div className="drawer-resizer" role="separator" aria-orientation="vertical"
           aria-label="拖动调整预览宽度" title="拖动调整宽度"
           onPointerDown={onDragStart} onPointerMove={onDragMove}
           onPointerUp={onDragEnd} onPointerCancel={onDragEnd} />

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
        {pdf && located ? (
          <PdfPages pdf={pdf} initialPage={located.page} rects={located.rects}
                    zoom={zoom} onRendered={handlePageSize} />
        ) : (
          !error && <div className="muted small pdf-loading">加载原文…</div>
        )}
      </div>
    </aside>
  )
}

// ---- 整份原文渲染 + 高亮叠加 -----------------------------------------------

/**
 * 把**整份 PDF** 按页连续渲染成一块可滚动的阅读区，并按**比例**叠加高亮框。
 *
 * ⚠️ 高亮框用百分比定位（相对页容器），所以缩放 / 容器拖拽 / 窗口变化都只需要
 *    重画 canvas，框自己跟着走 —— 不必重算每个框的像素值。
 *
 * ⚠️ **页面容器先全部建出来、canvas 再按需渲染**：整份一次画完，遇到几十页的
 *    文件会明显卡（每页都要解码 + 渲染）。所以先用 `aspect-ratio` 占好位、
 *    把滚动高度撑对，再用 IntersectionObserver 只画视口附近的页 —— 滚动条
 *    从一开始就是准的，不会边滚边跳。
 */
function PdfPages({ pdf, initialPage, rects, zoom, onRendered }: {
  pdf: PDFDocumentProxy
  /** 引用所在页 —— 打开时滚过去 */
  initialPage: number
  /** 该页要高亮的框（比例换算需要它所在页的尺寸） */
  rects: Located['rects']
  zoom: number
  onRendered: (pageSize: { w: number; h: number }) => void
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map())
  const [hostWidth, setHostWidth] = useState(0)
  // 每个页面容器的高度 / 宽度比（逐页量，因为**同一份文件里页尺寸可以不同** ——
  // 实测这份文档第 1 页是 595×358pt，其余是 595×842pt）
  const [ratios, setRatios] = useState<Record<number, number>>({})
  // 引用页的真实尺寸（高亮框换算的比例基准），由下面量比例的循环回填
  const [citedSize, setCitedSize] = useState<{ w: number; h: number } | null>(null)

  // 容器宽度 → 渲染分辨率。⚠️ 直接量 getBoundingClientRect，不要只信 RO ——
  //    实测只挂 RO 时拿到的是 0，canvas 会停在默认 300×150 一片空白。
  //    用 useLayoutEffect：要在绘制前量出来，否则首次会留一帧空 canvas。
  useLayoutEffect(() => {
    const host = innerRef.current
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

  // 逐页量比例（撑高度用），并把引用所在页的尺寸回报给上层（高亮框换算要用）
  useEffect(() => {
    let cancelled = false
    void (async () => {
      const next: Record<number, number> = {}
      for (let n = 1; n <= pdf.numPages; n += 1) {
        const vp = (await pdf.getPage(n)).getViewport({ scale: 1 })
        if (cancelled) return
        next[n] = vp.height / vp.width
        if (n === initialPage) {
          setCitedSize({ w: vp.width, h: vp.height })
          onRendered({ w: vp.width, h: vp.height })
        }
      }
      if (!cancelled) setRatios(next)
    })()
    return () => { cancelled = true }
  }, [pdf, initialPage, onRendered])

  // 只画视口附近的页（前后各一页的余量）。
  //
  // ⚠️ 这里**不用 IntersectionObserver** —— 实测它在这个抽屉里一次回调都不触发
  //    （20 个节点都 observe 上了，回调却始终不来），而且不报错、查不出原因。
  //    滚动事件 + 量位置虽然土，但行为确定、能测、不依赖浏览器的隐性前提
  //    （根元素是不是真的滚动祖先、回调会不会被节流）。
  //
  //    页数只有几十，每次滚动量一遍它们的相对位置，代价可以忽略。
  const [visible, setVisible] = useState<number[]>([1])
  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    let frame = 0
    const measure = () => {
      const rootTop = root.getBoundingClientRect().top
      const hits: number[] = []
      pageRefs.current.forEach((el, n) => {
        const rel = el.getBoundingClientRect().top - rootTop
        // 视口上下各留一页的余量
        if (rel < root.clientHeight * 2 && rel + el.offsetHeight > -root.clientHeight) {
          hits.push(n)
        }
      })
      hits.sort((a, b) => a - b)
      setVisible((prev) =>
        prev.length === hits.length && prev.every((n, i) => n === hits[i]) ? prev : hits)
    }
    const onScroll = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }
    // ⚠️ 只在比例就位后才量：比例没到时所有页高都是 0，会被一次性判成「全都可见」，
    //    懒渲染直接失效（实测 20 页全渲染）。
    if (Object.keys(ratios).length === 0) return
    measure()
    root.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      cancelAnimationFrame(frame)
      root.removeEventListener('scroll', onScroll)
    }
    // ratios 就位后页面才有高度，那时再量一次
  }, [ratios])

  // 打开就滚到引用页。
  useEffect(() => {
    const root = scrollRef.current
    const el = pageRefs.current.get(initialPage)
    if (!root || !el) return
    // ⚠️ 必须等 `ratios` 就位（页高定下来）再滚：之前页高是 0，滚完布局一变
    //    位置全错（实测引用页停在下方 1500~2400px 处，等于没跳）。
    if (Object.keys(ratios).length === 0) return
    const go = () => {
      // ⚠️ 用相对 rect 算，不要用 offsetTop —— 页容器的 offsetParent 是它自己
      //    （position: relative），offsetTop 拿到的是错的数
      const delta = el.getBoundingClientRect().top - root.getBoundingClientRect().top
      root.scrollTop += delta - 8
    }
    const raf = requestAnimationFrame(go)
    // 抽屉展开动画（~220ms）期间布局还在位移，落定后再校一次
    const timer = setTimeout(go, 320)
    return () => { cancelAnimationFrame(raf); clearTimeout(timer) }
    // ⚠️ 依赖里**不能放 `visible`** —— 滚动会改变 visible，那就是「滚完再滚」的死循环。
    //    它只该在「布局定下来」或「换了引用页」时跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPage, ratios])

  // 该画哪些页 = 可见的 ± 1
  const rendered = useMemo(() => {
    const set = new Set<number>()
    for (const n of visible) { set.add(n); set.add(n - 1); set.add(n + 1) }
    return set
  }, [visible])

  const cssWidth = hostWidth * zoom
  // 比例框：PDF 用户空间（左下原点、pt）→ 0-1 比例（左上原点）。
  // ⚠️ 基准是**实测的**引用页宽高，不按 A4 猜 —— 这份文档第 1 页就是 595×358pt，
  //    猜出来的框会整体偏。
  const boxes: Ratio[] = useMemo(() => {
    if (!citedSize) return []
    return rects.map((r) => ({
      left: r.x1 / citedSize.w,
      top: 1 - r.y2 / citedSize.h,
      width: (r.x2 - r.x1) / citedSize.w,
      height: (r.y2 - r.y1) / citedSize.h,
    }))
  }, [rects, citedSize])

  return (
    <div className="pdf-scroll" ref={scrollRef}>
      <div className="pdf-inner" ref={innerRef} style={{ width: `${zoom * 100}%` }}>
        {Array.from({ length: pdf.numPages }, (_, i) => i + 1).map((n) => (
          <div
            key={n}
            data-page={n}
            ref={(el) => { if (el) pageRefs.current.set(n, el) }}
            className={`pdf-page${n === initialPage ? ' cited' : ''}`}
            style={ratios[n] ? { aspectRatio: `1 / ${ratios[n]}` } : undefined}
          >
            <PageCanvas pdf={pdf} pageNumber={n} cssWidth={cssWidth} enabled={rendered.has(n)} />
            {n === initialPage && boxes.map((b, i) => (
              <span key={i} className="pdf-highlight" style={{
                left: `${b.left * 100}%`, top: `${b.top * 100}%`,
                width: `${b.width * 100}%`, height: `${b.height * 100}%`,
              }} />
            ))}
            <span className="pdf-page-no">{n}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 单页 canvas。`enabled` 为假时只占位不渲染（等滚到附近再画）。 */
function PageCanvas({ pdf, pageNumber, cssWidth, enabled }: {
  pdf: PDFDocumentProxy
  pageNumber: number
  cssWidth: number
  enabled: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    // 滚远了的页把画布放掉：几十页的位图留着是白占内存
    // （canvas 宽高变 0 时浏览器会释放后备位图；display:block + 100% 让它不占位）
    if (!enabled || cssWidth <= 0) {
      canvas.width = 0
      canvas.height = 0
      return
    }
    let cancelled = false
    let task: { cancel: () => void } | null = null

    void (async () => {
      const page = await pdf.getPage(pageNumber)
      if (cancelled) return
      const base = page.getViewport({ scale: 1 })
      const vp = page.getViewport({ scale: cssWidth / base.width })
      const dpr = window.devicePixelRatio || 1
      canvas.width = Math.floor(vp.width * dpr)
      canvas.height = Math.floor(vp.height * dpr)
      canvas.style.width = '100%'
      canvas.style.height = '100%'
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.scale(dpr, dpr)
      task = page.render({ canvasContext: ctx, viewport: vp })
      await (task as unknown as { promise: Promise<void> }).promise
    })()

    return () => { cancelled = true; task?.cancel() }
  }, [pdf, pageNumber, cssWidth, enabled])

  return <canvas ref={canvasRef} className="pdf-canvas" />
}

// ---- 四级定位（§4.2.2.4）----------------------------------------------------

/** 把 PyMuPDF 的框（左上角原点、y 向下）换成 PDF 用户空间（左下角原点、y 向上）。 */
function boxToPdf(box: Box, pageHeight: number) {
  return { x1: box.x0, y1: pageHeight - box.bottom, x2: box.x1, y2: pageHeight - box.top }
}

/** 四级定位。**每一级都可能失败，L4 是常态兜底**。 */
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
