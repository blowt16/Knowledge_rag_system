/**
 * 一次性 spike：实测 react-pdf-highlighter@8.0.0-rc.0 在 React 19 + Vite 7 下
 * 能不能用「我们 chunk metadata 里的 bbox」直接高亮。
 *
 * 高亮坐标取自 doc 03（643d38c8…）chunk 0 在 Chroma 里的真实 bbox —— 不是编的。
 * 测完即删。
 *
 * ⚠️ 坐标系：PyMuPDF 给的是**左上角原点、y 向下**（points）；pdf.js 的
 *    `convertToViewportRectangle` 要的是 **PDF 用户空间（左下角原点、y 向上）**。
 *    所以这里做了 y 翻转：y_pdf = page_height − y_tld。
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { Highlight, PdfHighlighter, PdfLoader, Popup } from 'react-pdf-highlighter'
import type { IHighlight } from 'react-pdf-highlighter'
import 'react-pdf-highlighter/dist/style.css'
// ⚠️ 库的默认 workerSrc 是 unpkg.com 上的 CDN 地址（内网/答辩现场必然取不到）。
//    实测：取不到时会静默退回「主线程假 worker」，页面照样渲染，所以不容易发现。
//    这里显式换成随包构建的本地 worker —— 走 workerSrc 属性传进去，
//    因为 PdfLoader.load() 会用 props.workerSrc 覆盖 GlobalWorkerOptions。
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

// spike 期间放在 public/ 下（Vite 的 /@fs/ 被 fs.allow 挡了 403）；
// 真实实现走 GET /api/documents/{id}/file，不依赖这个文件
const PDF_URL = '/_spike_doc03.pdf'

/** doc 03 · chunk 0 的真实 bbox（左上角原点，单位 points） */
const BOXES = [
  { page: 1, x0: 495.1, x1: 530.2, top: 758.0, bottom: 772.1 },
  { page: 1, x0: 230.8, x1: 378.7, top: 318.0, bottom: 339.5 },
  { page: 1, x0: 106.8, x1: 502.7, top: 374.7, bottom: 400.8 },
  { page: 1, x0: 139.8, x1: 469.7, top: 406.6, bottom: 432.8 },
  { page: 1, x0: 79.4, x1: 207.3, top: 464.5, bottom: 480.5 },
  { page: 1, x0: 111.4, x1: 530.0, top: 491.9, bottom: 507.9 },
]

function Inner({ pdfDocument }: { pdfDocument: PDFDocumentProxy }) {
  const [highlights, setHighlights] = useState<IHighlight[]>([])
  const [info, setInfo] = useState('取页面尺寸…')

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const page = await pdfDocument.getPage(1)
      const viewport = page.getViewport({ scale: 1 })
      if (cancelled) return
      const H = viewport.height
      setInfo(`page1 = ${Math.round(viewport.width)}×${Math.round(H)} pt（y 已翻转）`)
      setHighlights(
        BOXES.map((b, i) => ({
          id: `h${i}`,
          position: {
            pageNumber: b.page,
            boundingRect: {
              x1: b.x0, y1: H - b.bottom, x2: b.x1, y2: H - b.top,
              width: 0, height: 0, pageNumber: b.page,
            },
            rects: [
              {
                x1: b.x0, y1: H - b.bottom, x2: b.x1, y2: H - b.top,
                width: 0, height: 0, pageNumber: b.page,
              },
            ],
            usePdfCoordinates: true,
          },
          content: { text: `chunk0 bbox #${i}` },
          comment: { text: '', emoji: '' },
        })),
      )
    })()
    return () => {
      cancelled = true
    }
  }, [pdfDocument])

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '6px 10px', background: '#f2f2f2', fontSize: 13 }}>
        react-pdf-highlighter <b>8.0.0-rc.0</b> · React 19 · {info} · 高亮 {highlights.length} 个
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        {highlights.length === 0 ? (
          // ⚠️ 实测：highlights 在挂载之后才到达的话，componentDidUpdate 里
          //    this.viewer 还是 undefined → 崩。必须等高亮备齐再挂载。
          <div style={{ padding: 16 }}>准备高亮…</div>
        ) : (
        <PdfHighlighter
          pdfDocument={pdfDocument}
          highlights={highlights}
          pdfScaleValue="auto"
          enableAreaSelection={() => false}
          onSelectionFinished={() => null}
          onScrollChange={() => {}}
          scrollRef={() => {}}
          highlightTransform={(highlight, _i, setTip, hideTip, _v2s, _shot, isScrolledTo) => (
            <Popup
              key={highlight.id}
              popupContent={<div style={{ padding: 6, fontSize: 12 }}>{highlight.content.text}</div>}
              onMouseOver={(popupContent) => setTip(highlight, () => popupContent)}
              onMouseOut={hideTip}
            >
              <Highlight
                isScrolledTo={isScrolledTo}
                position={highlight.position}
                comment={highlight.comment}
              />
            </Popup>
          )}
        />
        )}
      </div>
    </div>
  )
}

function App() {
  return (
    <PdfLoader
      url={PDF_URL}
      workerSrc={workerSrc}
      beforeLoad={<div style={{ padding: 16 }}>加载 PDF…</div>}
    >
      {(pdfDocument) => <Inner pdfDocument={pdfDocument} />}
    </PdfLoader>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
