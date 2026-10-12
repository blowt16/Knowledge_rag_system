import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Moon, MoreHorizontal, Plus, Search, Sun, X } from 'lucide-react'
import { streamSse } from '../../api/sse'
import { getAccessToken } from '../../api/client'
import {
  deleteConversation, getMessages, listConversations,
  patchConversation,
} from '../../api/conversations'
import type { ConversationItem, MessageItem } from '../../api/conversations'
import { useAuth } from '../../stores/auth'
import type { AssistantDraft, Citation } from '../../api/types'
import { MarkdownAnswer } from '../../markdown/MarkdownAnswer'
import { StreamingAnswer } from '../../markdown/StreamingAnswer'
import { CitationImages, DocumentDrawer } from '../../components/DocumentDrawer'
import { AssistantAvatar, UserAvatar } from '../../components/Avatar'
import { useTheme } from '../../stores/theme'

interface Turn {
  id: string
  question: string
  draft: AssistantDraft
}

function emptyDraft(): AssistantDraft {
  return {
    stage: null,
    stageHint: '',
    clarifySkippedText: '',
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

/**
 * 阶段文案（§4.2.4.2 的映射表）。
 *
 * ⚠️ **展示文案以 `stage` 为准**，`label` 只作兜底与调试（§3.7.2）——
 *    两个文案源并存必然不一致：服务端改了 label，前端还显示自己那份。
 * ⚠️ `routing` / `verifying` **不单独提示**（源文：过快或非阻塞展示）——
 *    表里没有它们，于是保留上一条提示，而不是把提示清空。
 */
const STAGE_TEXT: Record<string, string> = {
  resolving: '正在理解你的问题…',
  retrieving: '正在检索知识库…',
  reranking: '正在筛选最相关的资料…',
  generating: '正在组织答案…',
}

/**
 * **有取值、但不单独提示**的阶段（源文：过快或非阻塞展示）。
 *
 * ⚠️ 实测（E2E 发现）：只写「表里没有就退回 `label`」是不够的 ——
 *    服务端给 `routing` 也配了 label（「正在判断问题类型…」），
 *    于是它照着自己那条 label 显示了 4ms，正好是代码注释里警告的
 *    「两个文案源并存必然不一致」。这里显式把它们钉成「不提示」。
 */
const STAGE_SILENT = new Set(['routing', 'verifying'])

/** token 必须每次请求现生成 —— 双击去重靠它（§3.2.4 的同会话幂等）。 */
function newRequestId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

const PAGE_SIZE = 20

export default function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [opened, setOpened] = useState<Citation | null>(null)
  // 提权开关（M3 的 K-7：前端从来没发过 include_restricted，UI 上是死代码）
  const [escalate, setEscalate] = useState(false)
  // 会话列表（§4.2.4.3 侧栏：分页 + 滚动到底加载下一页）
  const [convs, setConvs] = useState<ConversationItem[]>([])
  const [convTotal, setConvTotal] = useState(0)
  const [convLoading, setConvLoading] = useState(false)
  const [convSearch, setConvSearch] = useState('')
  // 重命名中的会话 id（内联编辑）
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameText, setRenameText] = useState('')
  const abortRef = useRef<(() => void) | null>(null)
  const { user, logout } = useAuth()
  const { theme, toggle: toggleTheme } = useTheme()
  const openCitation = useCallback((citation: Citation) => setOpened(citation), [])

  // ---- 滚动：贴底 + 用户上翻后不再被拉回（§4.2.4.3 的必须项）--------
  //
  // ⚠️ 这里曾经挂 `@tanstack/react-virtual`，**已拆除** —— 实测根因：
  //    首轮在「正在检索…」时高度只有 125px，虚拟滚动把它当成最终高度缓存住，
  //    答案进来后那一轮实际长到 315px，但虚拟滚动不再重量 →
  //    撑高的容器 `scrollHeight === clientHeight`，**整个对话区一点都滚不动**
  //    （实测 `scrollTop = 99999` 后仍为 0），长答案被画在滚动容器外面。
  //    收益本就不成立：一轮会话撑死几十轮，几十个 DOM 节点对浏览器不构成压力，
  //    而流式渲染的性能问题已经由 `StreamingAnswer` 的分块渲染解决了。
  const mainRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  function onStreamScroll() {
    const el = mainRef.current
    if (!el) return
    // 留 40px 余量：贴底时的微小抖动不该被当成「用户上翻了」
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  // 每次渲染后（含每个 token）把视口贴到底 —— 但**只在我们仍然贴着底时**。
  // 用户一开始上翻，stickRef 就是 false，内容再涨也不动他的视口。
  useEffect(() => {
    const el = mainRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  })

  const loadConversations = useCallback(async (reset: boolean) => {
    setConvLoading(true)
    try {
      const offset = reset ? 0 : convs.length
      const r = await listConversations(offset, PAGE_SIZE)
      setConvs((prev) => (reset ? r.items ?? [] : [...prev, ...(r.items ?? [])]))
      setConvTotal(r.total)
    } catch {
      /* 列表拉不到不该挡住问答 */
    } finally {
      setConvLoading(false)
    }
  }, [convs.length])

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadConversations(true) }, [])

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
    // 新的一轮：重新贴到底（用户可能正翻着旧消息）
    stickRef.current = true

    abortRef.current = streamSse({
      url: '/api/chat/stream',
      token: getAccessToken(),
      body: {
        query: question,
        session_id: sessionId,
        request_id: id,
        // 仅 admin 会带上；非 admin 服务端按 false 处理，不报错（§3.7.2）
        ...(user?.role === 'admin' ? { include_restricted: escalate } : {}),
      },
      handlers: {
        onEvent(event, data) {
          switch (event) {
            case 'session_created':
              setSessionId(data.session_id)
              break
            case 'stage': {
              const hint = STAGE_TEXT[data.stage]
                ?? (STAGE_SILENT.has(data.stage) ? '' : (data.label ?? ''))
              // 映射表里没有的阶段（routing / verifying）：保留上一条提示
              patchTurn(id, hint
                ? { stage: data.stage, stageHint: hint }
                : { stage: data.stage })
              break
            }
            case 'resolved':
              patchTurn(id, { resolvedQuery: data.resolved_query })
              break
            case 'clarify_skipped':
              // 澄清到顶：服务端不再反问，改为按最可能的理解作答，
              // 并给一句说明 —— 文案是服务端给的，前端原样显示
              patchTurn(id, { clarifySkippedText: data.text })
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
              // 这一轮可能让会话列表多出一条 —— 重新拉第一页
              void loadConversations(true)
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
        onError: () => {
          // ⚠️ 断线**不自动重连** —— SSE 没有 event id / 重放机制，
          //    重连拿不到内容（§3.2.4）。提示用户重新发送即可。
          patchTurn(id, {
            error: { code: 'network', message: '连接中断，请重新发送' },
            done: true,
          })
          setBusy(false)
        },
      },
    })
  }

  /** 打开历史会话。消息里的 `citations` 是刷新后重渲染引用角标的**唯一**来源。
   *  ⚠️ `verify_report` 只存在 `qa_logs`，所以历史会话里**灰标不再显示** ——
   *     这是有意为之（§3.7.2），不是缺陷。 */
  async function openConversation(id: string) {
    if (busy) return
    try {
      const r = await getMessages(id)
      setSessionId(id)
      setTurns(toTurns(r.messages ?? []))
      setOpened(null)
      stickRef.current = true
    } catch (e) {
      // 越权 / 已删会话在这里是 404（M4-D4）
      console.warn('打开会话失败', e)
    }
  }

  /** 新建对话只是清空本地 session_id —— 不调 POST /api/conversations，
   *  否则会话列表里会堆积没有消息的空会话（§3.7.2）。 */
  function newConversation() {
    if (busy) return
    setSessionId(null)
    setTurns([])
    setOpened(null)
  }

  /** 删除会话：不可撤销，先确认。删的是当前会话就顺带清空对话区。 */
  async function removeConversation(id: string, title: string) {
    if (busy) return
    if (!window.confirm(`删除会话「${title || '（无标题）'}」？此操作不可撤销。`)) return
    try {
      await deleteConversation(id)
      if (id === sessionId) { setSessionId(null); setTurns([]); setOpened(null) }
      // 本地先摘掉，再整体重拉（删除会让分页偏移错位）
      setConvs((prev) => prev.filter((c) => c.id !== id))
      setConvTotal((n) => Math.max(0, n - 1))
      void loadConversations(true)
    } catch (e) {
      console.warn('删除会话失败', e)
    }
  }

  /** 置顶 / 取消置顶。排序由服务端按 `is_top DESC` 决定，改完重拉第一页。 */
  async function toggleTop(id: string, isTop: boolean) {
    try {
      await patchConversation(id, { is_top: isTop ? 0 : 1 })
      void loadConversations(true)
    } catch (e) {
      console.warn('置顶失败', e)
    }
  }

  async function commitRename(id: string) {
    const title = renameText.trim()
    setRenaming(null)
    if (!title) return
    try {
      await patchConversation(id, { title })
      setConvs((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)))
    } catch (e) {
      console.warn('重命名失败', e)
    }
  }

  const shown = convSearch.trim()
    ? convs.filter((c) => (c.title || '').toLowerCase().includes(convSearch.trim().toLowerCase()))
    : convs

  return (
    <div className="app">
      {/* ---- 侧栏（§4.2.4.3）：新建对话 + 列表 + 逐条管理 ---- */}
      <aside className="sidebar">
        <div className="sidebar-head">
          <span className="brand">校园 RAG</span>
          <button className="btn-new" onClick={newConversation}
                  disabled={busy} title="新建对话">
            <Plus size={16} /> 新建对话
          </button>
        </div>

        <div className="conv-search">
          <Search size={14} />
          <input value={convSearch} placeholder="搜索会话"
                 onChange={(e) => setConvSearch(e.target.value)} />
          {convSearch && (
            <button className="icon-btn" onClick={() => setConvSearch('')} title="清除">
              <X size={13} />
            </button>
          )}
        </div>

        <div
          className="conv-list"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 40
                && !convLoading && !convSearch && convs.length < convTotal) {
              void loadConversations(false)
            }
          }}
        >
          {shown.map((c) => (
            <div key={c.id}
                 className={`conv-item${c.id === sessionId ? ' active' : ''}`}>
              {renaming === c.id ? (
                <input
                  className="conv-rename"
                  autoFocus
                  value={renameText}
                  onChange={(e) => setRenameText(e.target.value)}
                  onBlur={() => void commitRename(c.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void commitRename(c.id)
                    if (e.key === 'Escape') setRenaming(null)
                  }}
                />
              ) : (
                <button className="conv-open" onClick={() => void openConversation(c.id)}
                        title={c.title || '（无标题）'}>
                  {c.is_top ? <span className="pin">📌</span> : null}
                  <span className="conv-title">{c.title || '（无标题）'}</span>
                </button>
              )}

              {renaming !== c.id && (
                <div className="conv-actions">
                  <button className="icon-btn" title={c.is_top ? '取消置顶' : '置顶'}
                          onClick={() => void toggleTop(c.id, Boolean(c.is_top))}>
                    <MoreHorizontal size={14} />
                  </button>
                  <button className="icon-btn" title="重命名"
                          onClick={() => { setRenaming(c.id); setRenameText(c.title || '') }}>
                    ✎
                  </button>
                  <button className="icon-btn danger" title="删除"
                          onClick={() => void removeConversation(c.id, c.title)}>
                    <X size={14} />
                  </button>
                </div>
              )}
            </div>
          ))}
          {shown.length === 0 && (
            <p className="muted small center">
              {convSearch ? '没有匹配的会话' : '还没有会话'}
            </p>
          )}
          {!convSearch && convs.length < convTotal && (
            <p className="muted small center">滚动加载更多…</p>
          )}
        </div>

        <div className="sidebar-foot">
          <UserAvatar name={user?.username} size={26} />
          <span className="who">{user?.username}</span>
          <span className="muted small">{user?.role}</span>
          <button className="icon-btn" onClick={toggleTheme}
                  title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
            {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          </button>
          {user?.role === 'admin' && <Link className="link small" to="/admin">管理后台</Link>}
          <button className="link small" onClick={() => void logout()}>退出</button>
        </div>
      </aside>

      <div className="chat-area">
        <header className="chat-header">
          <strong>校园 RAG 检索问答</strong>
          {user?.role === 'admin' && (
            <label className="escalate-toggle"
                   title="越权检索受限文档，引用会标记「越权查看」">
              <input type="checkbox" checked={escalate}
                     onChange={(e) => setEscalate(e.target.checked)} />
              提权检索
            </label>
          )}
          <span className="muted small">
            {sessionId ? '已保存的会话' : '新对话'}
          </span>
        </header>

        {/* ⚠️ 滚动容器与「限宽内容列」必须**分成两层**：
            合在一起时 `max-width: 860px` 会把滚动条拉到 860px 的右边缘
            （页面中间），而不是窗口最右侧。 */}
        <main className="chat-scroll" ref={mainRef} onScroll={onStreamScroll}>
          <div className="chat-col">
            {turns.length === 0 && (
              <div className="empty">
                <div className="empty-logo"><AssistantAvatar size={44} /></div>
                <h2>有什么可以帮你？</h2>
                <p className="muted">试着问：「应届毕业班学生参军入伍有什么优待政策？」</p>
              </div>
            )}

            {turns.map((turn) => (
              <div className="turn" key={turn.id}>
                {/* 问句：头像在右 */}
                <div className="msg user">
                  <div className="bubble user">{turn.question}</div>
                  <UserAvatar name={user?.username} size={30} />
                </div>

                {/* 答句：头像在左 */}
                <div className="msg assistant">
                  <AssistantAvatar size={30} />
                  <div className="bubble assistant">
                    {turn.draft.resolvedQuery
                      && turn.draft.resolvedQuery !== turn.question && (
                      <div className="resolved">我理解你在问：{turn.draft.resolvedQuery}</div>
                    )}

                    {turn.draft.clarifySkippedText && (
                      <div className="resolved">{turn.draft.clarifySkippedText}</div>
                    )}

                    {turn.draft.text && (
                      // 偏移的参照系是**原始文本** —— 传进去的必须是未渲染的 draft.text
                      //
                      // ⚠️ 流式进行中走按块渲染（每个 token 只重渲染尾部那一块），
                      //    收尾后再整段渲染一次 —— 因为置灰标注的偏移是相对**整段**
                      //    原始答案的，按块渲染就得逐块平移偏移，而那种映射错一格
                      //    只会「标歪」，肉眼极难发现（§4.2.1.2）。
                      //    流式期间 verify 还没到，本来就没有偏移可错。
                      turn.draft.done ? (
                        <MarkdownAnswer
                          text={turn.draft.text}
                          citations={turn.draft.citations}
                          verify={turn.draft.verify}
                          onCitationClick={openCitation}
                        />
                      ) : (
                        <StreamingAnswer
                          text={turn.draft.text}
                          citations={turn.draft.citations}
                          verify={turn.draft.verify}
                          onCitationClick={openCitation}
                        />
                      )
                    )}

                    {!turn.draft.text && !turn.draft.done && (
                      <span className="thinking">
                        <span className="dot" /><span className="dot" /><span className="dot" />
                        {turn.draft.stageHint || '正在检索…'}
                      </span>
                    )}

                    {turn.draft.refusedText && (
                      <div className="refused">
                        {turn.draft.refusedText}
                        {turn.draft.refusedHint && (
                          <div className="muted">{turn.draft.refusedHint}</div>
                        )}
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

                    <Citations citations={turn.draft.citations} onOpen={openCitation} />

                    {turn.draft.done && turn.draft.latencyMs != null && (
                      <div className="muted small">{turn.draft.latencyMs} ms</div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </main>

        <footer className="chat-input">
          <div className="chat-col">
            <div className="input-row">
              <textarea
                rows={1}
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
              <button className="send" onClick={() => send(input)}
                      disabled={busy || !input.trim()}>
                发送
              </button>
            </div>
          </div>
        </footer>
      </div>

      {opened && (
        <DocumentDrawer
          citation={opened}
          includeRestricted={user?.role === 'admin' && escalate}
          onClose={() => setOpened(null)}
        />
      )}
    </div>
  )
}

/** 历史消息 → 轮次。一问一答配成一组；`citations` 原样搬过来（刷新后角标的唯一来源）。 */
function toTurns(messages: MessageItem[]): Turn[] {
  const out: Turn[] = []
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (m.role !== 'user') continue
    const next = messages[i + 1]
    const answer = next && next.role === 'assistant' ? next : null
    out.push({
      id: m.id,
      question: m.content,
      draft: {
        ...emptyDraft(),
        text: answer?.content ?? '',
        // 后端那一列是落库的 Citation 列表（含 [n] 的正文 + 结构化引用）。
        // OpenAPI 里它是裸 list，生成的类型是 unknown[] —— 形状由后端保证，这里收一下。
        citations: (answer?.citations ?? []) as Citation[],
        // 历史会话一律当作已结束：不会再追加 token
        done: true,
      },
    })
  }
  return out
}

function Citations({ citations, onOpen }: {
  citations: Citation[]
  onOpen: (citation: Citation) => void
}) {
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
            {items.map((c) => (
              <button key={c.chunk_id} className="link small"
                      onClick={() => onOpen(c)}>
                [{c.marker}] 第 {c.page} 页{c.chapter ? ` · ${c.chapter}` : ''}
              </button>
            ))}
          </div>
          {items[0].images.length > 0 && (
            <CitationImages documentId={docId} names={items[0].images} />
          )}
        </div>
      ))}
    </div>
  )
}
