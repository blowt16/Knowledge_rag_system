/**
 * SSE 客户端 —— **fetch + ReadableStream 手工解析**。
 *
 * ⚠️⚠️ **不能用浏览器的 `EventSource`**（方案 §3.2.2 钉死）：
 *
 * | 事实 | 后果 |
 * |---|---|
 * | 原生 `EventSource` **只能发 GET、且无法设置请求头** | 带不上 `Authorization: Bearer <JWT>` → 只能 401，或被逼把 token 塞进查询串 |
 * | 把 token 塞查询串 | 与「身份只能来自 JWT」冲突，且 **token 会进入访问日志与浏览器历史** |
 *
 * 所以一律用 `fetch` + `ReadableStream` 手工解析 `data:` 行 —— 可带请求头、可用 POST。
 *
 * 这条是**前后端并行开工第一天就会撞上的**问题：后端按 Bearer 头实现鉴权，
 * 前端用 `EventSource` 就永远连不上。
 */

export interface SseHandlers {
  /** 收到一个命名事件。event 是事件名，data 是解析后的 JSON。 */
  onEvent: (event: string, data: any) => void
  /** 流正常结束 */
  onClose?: () => void
  /** 网络层错误（HTTP 非 200、连接中断等） */
  onError?: (error: Error) => void
}

export interface SseOptions {
  url: string
  body: unknown
  token: string | null
  handlers: SseHandlers
  /** 外部中断信号（组件卸载 / 用户点「停止」） */
  signal?: AbortSignal
}

/**
 * 发起一次 SSE 请求并逐事件回调。
 *
 * 返回一个 abort 函数；调用它会中断连接。
 */
export function streamSse({ url, body, token, handlers, signal }: SseOptions): () => void {
  const controller = new AbortController()
  // 把外部 signal 接到内部 controller 上
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  void (async () => {
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!resp.ok) {
        // 错误响应是 JSON（统一异常格式），不是 SSE
        let detail = `HTTP ${resp.status}`
        try {
          const j = await resp.json()
          detail = j.message || j.code || detail
        } catch {
          /* 保持默认 */
        }
        throw new Error(detail)
      }
      if (!resp.body) throw new Error('响应没有 body，无法流式读取')

      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      // 逐块读取，按「空行」切分事件帧
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE 帧以空行结束；兼容 \n\n 与 \r\n\r\n
        let sep = findFrameEnd(buffer)
        while (sep !== null) {
          const raw = buffer.slice(0, sep.index)
          buffer = buffer.slice(sep.index + sep.length)
          dispatchFrame(raw, handlers)
          sep = findFrameEnd(buffer)
        }
      }
      // 收尾：缓冲区里可能还剩最后一帧（有些实现末尾不发空行）
      if (buffer.trim()) dispatchFrame(buffer, handlers)

      handlers.onClose?.()
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        handlers.onClose?.()
        return
      }
      handlers.onError?.(err as Error)
    }
  })()

  return () => controller.abort()
}

function findFrameEnd(buf: string): { index: number; length: number } | null {
  const a = buf.indexOf('\n\n')
  const b = buf.indexOf('\r\n\r\n')
  if (a === -1 && b === -1) return null
  // 取更靠前的分隔符（\r\n\r\n 的起点可能早于 \n\n）
  if (b !== -1 && (a === -1 || b < a)) return { index: b, length: 4 }
  return { index: a, length: 2 }
}

/** 解析一帧：`event: xxx` + 若干 `data:` 行。 */
function dispatchFrame(raw: string, handlers: SseHandlers): void {
  let event = 'message'
  const dataLines: string[] = []

  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue // 注释/心跳
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      // 规范：data 后可选一个空格，其余原样（可能多行拼接）
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (dataLines.length === 0) return

  const payload = dataLines.join('\n')
  try {
    handlers.onEvent(event, JSON.parse(payload))
  } catch {
    // 非 JSON 的 data 不致命，原样透传
    handlers.onEvent(event, payload)
  }
}
