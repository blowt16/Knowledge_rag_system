import { useRef, useState } from 'react'
import { streamSse } from '../../api/sse'
import { getAccessToken } from '../../api/client'
import { useAuth } from '../../stores/auth'
import type { AssistantDraft, Citation } from '../../api/types'

interface Turn {
  id: string
  question: string
  draft: AssistantDraft
}

function emptyDraft(): AssistantDraft {
  return {
    route: null,
    clarifyFacets: [],
    resolvedQuery: '',
    decision: null,
    text: '',
    citations: [],
    verify: null,
    refusedReason: null,
    refusedText: '',
    refusedHint: null,
    error: null,
    latencyMs: null,
    done: false,
  }
}

/** token 必须每次请求现生成 —— 双击去重靠它（§3.2.4 的同会话幂等）。 */
function newRequestId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export default function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const abortRef = useRef<(() => void) | null>(null)
  const { user, logout } = useAuth()

  function patchTurn(id: string, patch: Partial<AssistantDraft>) {
    setTurns((prev) =>
      prev.map((t) => (t.id === id ? { ...t, draft: { ...t.draft, ...patch } } : t)),
    )
  }

  function send(question: string) {
    if (!question.trim() || busy) return
    const id = newRequestId()
    setTurns((prev) => [...prev, { id, question, draft: emptyDraft() }])
    setInput('')
    setBusy(true)

    abortRef.current = streamSse({
      url: '/api/chat/stream',
      token: getAccessToken(),
      body: {
        query: question,
        session_id: sessionId,
        request_id: id,
      },
      handlers: {
        onEvent(event, data) {
          switch (event) {
            case 'session_created':
              setSessionId(data.session_id)
              break
            case 'resolved':
              patchTurn(id, { resolvedQuery: data.resolved_query })
              break
            case 'route':
              // ⚠️ 读的是 clarify_facets（不是 facets）
              patchTurn(id, {
                route: data.route,
                clarifyFacets: data.clarify_facets ?? [],
              })
              break
            case 'decision':
              patchTurn(id, { decision: data.decision })
              break
            case 'token':
              // 逐字追加。⚠️ 服务端保证 text 是 JSON 解码后的纯文本，
              //    按序拼接必须逐字等于服务端校验时用的文本
              setTurns((prev) =>
                prev.map((t) =>
                  t.id === id ? { ...t, draft: { ...t.draft, text: t.draft.text + data.text } } : t,
                ),
              )
              break
            case 'citations':
              patchTurn(id, { citations: data.citations ?? [] })
              break
            case 'verify':
              patchTurn(id, { verify: data })
              break
            case 'refused':
              patchTurn(id, {
                refusedReason: data.reason,
                refusedText: data.text,
                refusedHint: data.hint ?? null,
              })
              break
            case 'done':
              patchTurn(id, { latencyMs: data.latency_ms, done: true })
              break
            case 'error':
              // ⚠️ error 是**终止事件**：发完不再发 done。
              //    前端保留已流出的正文（不整条清空）
              patchTurn(id, { error: data, done: true })
              break
          }
        },
        onClose: () => {
          setBusy(false)
          setTurns((prev) =>
            prev.map((t) => (t.id === id ? { ...t, draft: { ...t.draft, done: true } } : t)),
          )
        },
        onError: (e) => {
          // ⚠️ 断线**不自动重连** —— SSE 没有 event id / 重放机制，
          //    重连拿不到内容（§3.2.4）
          patchTurn(id, { error: { code: 'network', message: e.message }, done: true })
          setBusy(false)
        },
      },
    })
  }

  return (
    <div className="chat-layout">
      <header className="chat-header">
        <strong>校园 RAG 检索问答</strong>
        <span className="muted">
          {user?.username}（{user?.role}）
        </span>
        <button
          className="link"
          onClick={() => {
            // 新建对话只是清空本地 session_id —— 不调 POST /api/conversations，
            // 否则会话列表里会堆积没有消息的空会话（§3.7.2）
            setSessionId(null)
            setTurns([])
          }}
        >
          新建对话
        </button>
        <button className="link" onClick={() => void logout()}>
          退出
        </button>
      </header>

      <main className="chat-main">
        {turns.length === 0 && (
          <p className="muted center">
            试着问：「应届毕业班学生参军入伍有什么优待政策？」
          </p>
        )}

        {turns.map((turn) => (
          <div key={turn.id} className="turn">
            <div className="bubble user">{turn.question}</div>

            {turn.draft.resolvedQuery && turn.draft.resolvedQuery !== turn.question && (
              <div className="resolved">我理解你在问：{turn.draft.resolvedQuery}</div>
            )}

            <div className="bubble assistant">
              {turn.draft.text && <div className="answer">{turn.draft.text}</div>}

              {!turn.draft.text && !turn.draft.done && <span className="muted">正在检索…</span>}

              {turn.draft.refusedText && (
                <div className="refused">
                  {turn.draft.refusedText}
                  {turn.draft.refusedHint && <div className="muted">{turn.draft.refusedHint}</div>}
                </div>
              )}

              {turn.draft.clarifyFacets.length > 0 && (
                <div className="facets">
                  {turn.draft.clarifyFacets.map((f) => (
                    <button key={f} className="facet" onClick={() => send(f)}>
                      {f}
                    </button>
                  ))}
                </div>
              )}

              {turn.draft.error && (
                <div className="error">
                  {turn.draft.error.message}
                  {turn.draft.error.code === 'session_busy' && '（本会话正在生成中）'}
                </div>
              )}

              <Citations citations={turn.draft.citations} />

              {turn.draft.verify && turn.draft.verify.uncited_claims.length > 0 && (
                <div className="verify">
                  本回答含 {turn.draft.verify.uncited_claims.length} 处未证实内容
                </div>
              )}

              {turn.draft.done && turn.draft.latencyMs != null && (
                <div className="muted small">{turn.draft.latencyMs} ms</div>
              )}
            </div>
          </div>
        ))}
      </main>

      <footer className="chat-input">
        <input
          value={input}
          placeholder={busy ? '正在生成中…' : '输入你的问题，回车发送'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(input)
            }
          }}
          // ⚠️ 同一会话同时只允许一个请求：前端禁用发送按钮（§3.2.4）
          disabled={busy}
        />
        <button onClick={() => send(input)} disabled={busy || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  )
}

function Citations({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null
  // 按文档聚合去重（§4.2.3.2）—— 归组键用 document_id，不用 document_name
  const grouped = new Map<string, Citation[]>()
  for (const c of citations) {
    const key = c.jump_target?.document_id ?? c.chunk_id
    grouped.set(key, [...(grouped.get(key) ?? []), c])
  }

  return (
    <div className="citations">
      <div className="muted small">📚 引用</div>
      {[...grouped.entries()].map(([docId, items]) => (
        <div key={docId} className="citation-group">
          <div>
            {items[0].document_name}
            {items.length > 1 && `（${items.length} 处）`}
            {items.some((c) => c.escalated) && (
              // 越权取得必须显式标注「此内容你本无权限」
              <span className="escalated">（越权查看）</span>
            )}
          </div>
          <div className="muted small">
            {items.map((c) => `[${c.marker}] 第 ${c.page} 页${c.chapter ? ` · ${c.chapter}` : ''}`).join('　')}
          </div>
        </div>
      ))}
    </div>
  )
}
