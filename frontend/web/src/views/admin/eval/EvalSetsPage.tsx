/**
 * 评测集管理（§6.2）—— 评测集 → 用例 的管理界面。
 *
 * 四件事：选/建/删/导出评测集，增删改查用例，从文档自动生成，核对标准答案。
 *
 * ⚠️ **筛选与搜索走后端**（§6.2）：分页之下前端过滤必然是错的 ——
 *    第 2 页的数据还没加载，前端过滤只能过滤已加载的那 20 条。
 *
 * ⚠️ **「看原文」的置灰判据是 `source_snippet` 是否为空，不是 `source` 字段**（§6.5）：
 *    老 90 条迁移后 `source='generated'` 但**没有** `source_*`。
 *    按 `source` 判就会出现「列表里标着『文档生成』、点开却被告知『这是手工录入』」。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { listDocuments } from '@/api/admin'
import type { DocumentItem } from '@/api/admin'
import {
  type EvalCaseItem,
  type EvalCaseSource,
  type EvalGenerateResponse,
  type EvalSetItem,
  createEvalCase,
  createEvalSet,
  deleteEvalCase,
  deleteEvalSet,
  evalCaseSource,
  exportEvalSet,
  generateEvalCases,
  listEvalCases,
  listEvalSets,
  patchEvalCase,
} from '@/api/eval'
import { filenameFrom, saveBlob } from '@/lib/download'

const PAGE_SIZE = 20
//: 生成条数上限与服务端一致（决策 18）—— 前端也夹一下，别让用户白等一次 422
const MAX_GENERATE = 10

const SOURCE_LABEL: Record<string, string> = { manual: '手工录入', generated: '文档生成' }

export default function EvalSetsPage() {
  const [sets, setSets] = useState<EvalSetItem[]>([])
  const [setId, setSetId] = useState('')
  const [cases, setCases] = useState<EvalCaseItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [sourceFilter, setSourceFilter] = useState('')
  const [search, setSearch] = useState('')       // 输入框里的值
  const [query, setQuery] = useState('')         // 已提交的值（点「查询」才生效）
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const [newSetOpen, setNewSetOpen] = useState(false)
  const [caseDialog, setCaseDialog] = useState<{ mode: 'create' | 'edit'; item?: EvalCaseItem } | null>(null)
  const [genOpen, setGenOpen] = useState(false)
  const [sourceOf, setSourceOf] = useState<EvalCaseSource | null>(null)
  //: 删除确认。⚠️ **不用 `confirm()`** —— 管理端别处（DocumentsPage 的停用）一律是
  //: `AlertDialog`：原生弹窗样式不可控、在嵌入式/无头环境里还会被直接拦掉，
  //: 而且「删了哪些东西、能不能撤销」这种话原生框里根本写不下。
  const [pendingDelete, setPendingDelete] =
    useState<{ kind: 'set' | 'case'; item: EvalSetItem | EvalCaseItem } | null>(null)

  const current = useMemo(() => sets.find(s => s.id === setId) ?? null, [sets, setId])

  // ---- 加载 ----------------------------------------------------------

  const loadSets = useCallback(async () => {
    const r = await listEvalSets()
    setSets(r.items ?? [])
    return r.items ?? []
  }, [])

  const loadCases = useCallback(async () => {
    if (!setId) { setCases([]); setTotal(0); return }
    const r = await listEvalCases(setId, {
      source: sourceFilter || undefined,
      q: query || undefined,
      page, page_size: PAGE_SIZE,
    })
    setCases(r.items ?? [])
    setTotal(r.total ?? 0)
  }, [setId, sourceFilter, query, page])

  useEffect(() => {
    loadSets()
      .then(items => { if (items.length && !setId) setSetId(items[0].id) })
      .catch((e: Error) => setMsg(`加载评测集失败：${e.message}`))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadSets])

  useEffect(() => {
    loadCases().catch((e: Error) => setMsg(`加载用例失败：${e.message}`))
  }, [loadCases])

  /** 增删改用例后统一走这里：列表与下拉里的「（N 条用例）」要一起刷新。 */
  const reload = useCallback(async () => {
    await Promise.all([loadSets(), loadCases()])
  }, [loadSets, loadCases])

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setMsg('')
    try { await fn() } catch (e) { setMsg((e as Error).message) } finally { setBusy(false) }
  }

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  // ---- 导出（决策 21）------------------------------------------------

  const onExport = () => run(async () => {
    if (!current) return
    const { blob, disposition } = await exportEvalSet(current.id)
    // 文件名以服务端 `Content-Disposition` 为准（它消毒过、也用 UTF-8'' 编了码），
    // 读不到才退回本地拼一个
    saveBlob(blob, filenameFrom(disposition) ?? `${current.name}.json`)
    setMsg(`已导出 ${current.case_count} 条用例`)
  })

  return (
    <div className="space-y-6">
      {/* ---- 顶部：评测集选择与三个动作 ---- */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-6">
          <select
            className="min-w-64 rounded border border-border bg-background px-2 py-1.5 text-sm"
            value={setId}
            onChange={e => { setSetId(e.target.value); setPage(1) }}
          >
            {sets.length === 0 && <option value="">（还没有评测集）</option>}
            {sets.map(s => (
              // 括号里的数是该集**全部**用例数（不是只数 in_eval 的）——
              // 它与界面上的行数对得上，用户才不会以为丢了数据（§11.3-4）
              <option key={s.id} value={s.id}>{s.name}（{s.case_count} 条用例）</option>
            ))}
          </select>
          <Button disabled={busy} onClick={() => setNewSetOpen(true)}>新建评测集</Button>
          <Button variant="destructive" disabled={busy || !current}
                  onClick={() => current && setPendingDelete({ kind: 'set', item: current })}>
            删除评测集
          </Button>
          {/* 未选中任何评测集时置灰（决策 21）；**最右边** */}
          <Button variant="outline" disabled={busy || !current} onClick={onExport}>
            导出评测集
          </Button>
        </CardContent>
      </Card>

      {/* ---- 工具栏 + 列表 ---- */}
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Button variant="outline" disabled={busy || !current}
                    onClick={() => setCaseDialog({ mode: 'create' })}>手工录入</Button>
            <Button variant="outline" disabled={busy || !current}
                    onClick={() => setGenOpen(true)}>从文档自动生成</Button>
            {/* 「从点踩沉淀」按决策 3 **不做** */}
            <span className="mx-2 h-5 w-px bg-border" />
            <select className="rounded border border-border bg-background px-2 py-1"
                    value={sourceFilter}
                    onChange={e => { setSourceFilter(e.target.value); setPage(1) }}>
              <option value="">全部来源</option>
              <option value="generated">文档生成</option>
              <option value="manual">手工录入</option>
            </select>
            <Input className="w-56" placeholder="按问题搜索" value={search}
                   onChange={e => setSearch(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') { setQuery(search); setPage(1) } }} />
            <Button variant="outline"
                    onClick={() => { setQuery(search); setPage(1) }}>查询</Button>
          </div>

          {msg && <p className="text-sm text-muted-foreground">{msg}</p>}

          {!current ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              先在上面的下拉里选一个评测集，或者新建一个
            </p>
          ) : cases.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {query || sourceFilter ? '没有符合条件的用例' : '这个评测集还没有用例'}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-3">问题</th>
                    <th className="py-2 pr-3">标准答案</th>
                    <th className="py-2 pr-3">来源</th>
                    <th className="py-2 pr-3">参与评测</th>
                    <th className="py-2 pr-3">备注</th>
                    <th className="py-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {cases.map(c => (
                    <tr key={c.id} className="border-t border-border align-top">
                      <td className="max-w-md py-2 pr-3">{c.question}</td>
                      <td className="max-w-xs py-2 pr-3 text-muted-foreground">
                        {c.ground_truth || '—'}
                      </td>
                      <td className="py-2 pr-3">{SOURCE_LABEL[c.source] ?? c.source}</td>
                      <td className="py-2 pr-3">{c.in_eval ? '是' : '否'}</td>
                      <td className="max-w-xs py-2 pr-3 text-muted-foreground">{c.note || ''}</td>
                      <td className="whitespace-nowrap py-2">
                        <SourceButton item={c} busy={busy} onOpen={setSourceOf} />
                        <Button variant="link" disabled={busy}
                                onClick={() => setCaseDialog({ mode: 'edit', item: c })}>编辑</Button>
                        <Button variant="link" disabled={busy}
                                onClick={() => setPendingDelete({ kind: 'case', item: c })}>
                          删除
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {current && pages > 1 && (
            <div className="flex items-center justify-end gap-2 text-sm">
              <Button variant="outline" size="sm" disabled={page <= 1}
                      onClick={() => setPage(p => p - 1)}>上一页</Button>
              <span className="text-muted-foreground">{page} / {pages}</span>
              <Button variant="outline" size="sm" disabled={page >= pages}
                      onClick={() => setPage(p => p + 1)}>下一页</Button>
            </div>
          )}
        </CardContent>
      </Card>

      <NewSetDialog open={newSetOpen} onClose={() => setNewSetOpen(false)}
                    onDone={async (created) => {
                      setNewSetOpen(false)
                      setMsg(`已新建「${created.name}」`)
                      await loadSets()
                      setSetId(created.id); setPage(1)
                    }} />

      <AlertDialog open={!!pendingDelete} onOpenChange={o => !o && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingDelete?.kind === 'set'
                ? `删除「${(pendingDelete.item as EvalSetItem).name}」？`
                : '删除这条用例？'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingDelete?.kind === 'set'
                ? '它下面的用例会一起删掉。历史评测记录不受影响 —— 那些记录存的是评测集名字的快照。'
                : '跑过的历史评测结果会保留（只是不再指向这条用例），但用例本身删了就没了。'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => run(async () => {
              const target = pendingDelete
              setPendingDelete(null)
              if (!target) return
              if (target.kind === 'set') {
                const s = target.item as EvalSetItem
                await deleteEvalSet(s.id)
                setSetId(''); setPage(1)
                setMsg(`已删除「${s.name}」`)
                await loadSets()
              } else {
                await deleteEvalCase(target.item.id)
                setMsg('已删除该用例（历史评测记录保留）')
                await reload()
              }
            })}>
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {caseDialog && current && (
        <CaseDialog state={caseDialog} setId={current.id}
                    onClose={() => setCaseDialog(null)}
                    onDone={async (what) => {
                      setCaseDialog(null)
                      setMsg(what)
                      await reload()
                    }} />
      )}

      {genOpen && current && (
        <GenerateDialog setId={current.id} setName={current.name}
                        onClose={() => setGenOpen(false)}
                        onDone={async (text) => { setMsg(text); await reload() }} />
      )}

      <SourceDialog payload={sourceOf} onClose={() => setSourceOf(null)} />
    </div>
  )
}

/** 「看原文」—— 判据是 `source_snippet`，不是 `source`（见文件头）。 */
function SourceButton({ item, busy, onOpen }: {
  item: EvalCaseItem
  busy: boolean
  onOpen: (p: EvalCaseSource) => void
}) {
  const hasSnippet = Boolean(item.source_chunk_id || item.source_page)
  // 老 90 条迁移后 source='generated' 却没有来源片段 —— 提示语要分清这两种，
  // 否则会出现「列表里标着『文档生成』、点开被告知『这是手工录入』」的自相矛盾
  const hint = item.source === 'manual'
    ? '手工录入的用例没有来源片段'
    : '这条是早期导入的题库，没有留下来源片段'
  return (
    <Button variant="link" disabled={busy || !hasSnippet} title={hasSnippet ? undefined : hint}
            onClick={async () => {
              try { onOpen(await evalCaseSource(item.id)) } catch { /* 由外层提示 */ }
            }}>
      看原文
    </Button>
  )
}

// ============================================================
// 弹窗一：新建评测集
// ============================================================

function NewSetDialog({ open, onClose, onDone }: {
  open: boolean
  onClose: () => void
  onDone: (s: EvalSetItem) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { if (open) { setName(''); setDescription(''); setErr('') } }, [open])

  const submit = async () => {
    if (!name.trim()) { setErr('评测集名不能为空'); return }
    setBusy(true); setErr('')
    try {
      onDone(await createEvalSet({ name: name.trim(), description: description || null }))
    } catch (e) {
      // 重名是 409，服务端的话已经够清楚（「已有同名评测集」）—— 原样显示，别包一层
      setErr((e as Error).message)
    } finally { setBusy(false) }
  }

  return (
    <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent>
        <DialogHeader><DialogTitle>新建评测集</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="set-name">名称</Label>
            <Input id="set-name" value={name} onChange={e => setName(e.target.value)}
                   placeholder="如：售后问题测评" />
          </div>
          <div className="space-y-1">
            <Label htmlFor="set-desc">说明</Label>
            <Textarea id="set-desc" value={description}
                      onChange={e => setDescription(e.target.value)} rows={3} />
          </div>
          {err && <p className="text-sm text-destructive">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={busy} onClick={submit}>{busy ? '创建中…' : '创建'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============================================================
// 弹窗二：评测用例（手工录入与编辑共用）
// ============================================================

function CaseDialog({ state, setId, onClose, onDone }: {
  state: { mode: 'create' | 'edit'; item?: EvalCaseItem }
  setId: string
  onClose: () => void
  onDone: (msg: string) => void
}) {
  const editing = state.mode === 'edit'
  const [question, setQuestion] = useState(state.item?.question ?? '')
  const [groundTruth, setGroundTruth] = useState(state.item?.ground_truth ?? '')
  const [inEval, setInEval] = useState(state.item?.in_eval ?? true)
  const [note, setNote] = useState(state.item?.note ?? '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!question.trim()) { setErr('问题不能为空'); return }
    setBusy(true); setErr('')
    try {
      const body = {
        question: question.trim(),
        ground_truth: groundTruth || null,
        in_eval: inEval,
        note: note || null,
      }
      if (editing && state.item) await patchEvalCase(state.item.id, body)
      else await createEvalCase(setId, body)
      onDone(editing ? '已保存' : '已录入一条用例')
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent className="max-w-2xl">
        {/* 弹窗里**没有「题目类型」**（参考图就没有）—— `case_type` 由服务端填
            `'factual'`，它是 NOT NULL 且无默认值，不填服务端当场报错（§5.2） */}
        <DialogHeader><DialogTitle>{editing ? '编辑用例' : '评测用例'}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="case-q">问题</Label>
            <Textarea id="case-q" rows={2} value={question}
                      onChange={e => setQuestion(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="case-a">标准答案</Label>
            <Textarea id="case-a" rows={3} value={groundTruth}
                      onChange={e => setGroundTruth(e.target.value)} />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={inEval}
                   onChange={e => setInEval(e.target.checked)} />
            参与评测
          </label>
          <div className="space-y-1">
            <Label htmlFor="case-note">备注</Label>
            <Input id="case-note" value={note} onChange={e => setNote(e.target.value)} />
          </div>
          {err && <p className="text-sm text-destructive">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={busy} onClick={submit}>{busy ? '保存中…' : '保存'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============================================================
// 弹窗三：从文档自动生成用例（§8）
// ============================================================

function GenerateDialog({ setId, setName, onClose, onDone }: {
  setId: string
  setName: string
  onClose: () => void
  onDone: (msg: string) => void
}) {
  // ⚠️ 弹窗里**没有「知识库」这一级**（决策 5）—— 项目没有知识库概念，直接选文档
  const [docs, setDocs] = useState<DocumentItem[]>([])
  const [docId, setDocId] = useState('')
  const [count, setCount] = useState(5)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState<EvalGenerateResponse | null>(null)

  useEffect(() => {
    listDocuments({ status: 'active', page: 1, page_size: 100 })
      .then(r => {
        const items = r.items ?? []
        setDocs(items)
        if (items.length) setDocId(items[0].id)
      })
      .catch((e: Error) => setErr(`加载文档失败：${e.message}`))
  }, [])

  const submit = async () => {
    if (!docId) { setErr('先选一份文档'); return }
    setBusy(true); setErr(''); setResult(null)
    try {
      const r = await generateEvalCases(setId, {
        document_id: docId,
        count: Math.min(MAX_GENERATE, Math.max(1, count)),
      })
      setResult(r)
      // 条数是**目标不是保证**（决策 18）：如实说，不硬凑
      const parts = [`要了 ${r.requested} 条，实际生成 ${r.created} 条`]
      if (r.failed > 0) parts.push(`（${r.failed} 条没通过校验）`)
      if (r.timeout) parts.push('｜超时了，已先生成的保留在库里，剩下的可以再点一次')
      if (r.reason && r.created === 0) parts.push(`｜${r.reason}`)
      onDone(parts.join(''))
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent>
        <DialogHeader><DialogTitle>从文档自动生成用例</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            生成到「{setName}」。模型照着文档片段出题，<b>标准答案必须是原文逐字子串</b>、
            问句不带代词 —— 过不了校验的那条就不生成。
          </p>
          <div className="space-y-1">
            <Label htmlFor="gen-doc">文档</Label>
            <select id="gen-doc"
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                    value={docId} onChange={e => setDocId(e.target.value)}>
              {docs.length === 0 && <option value="">（没有可用的文档）</option>}
              {docs.map(d => <option key={d.id} value={d.id}>{d.title}</option>)}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="gen-count">生成条数（最多 {MAX_GENERATE}）</Label>
            <Input id="gen-count" type="number" min={1} max={MAX_GENERATE} value={count}
                   onChange={e => setCount(Number(e.target.value))} />
          </div>
          {busy && (
            <p className="text-sm text-muted-foreground">
              生成中…（要逐条问模型，5 条约 20~30 秒，最多等 2 分钟）
            </p>
          )}
          {result && !busy && (
            <div className="space-y-1 text-sm">
              {result.cases.map(c => (
                <p key={c.id} className="text-muted-foreground">· {c.question}</p>
              ))}
            </div>
          )}
          {err && <p className="text-sm text-destructive">{err}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{result ? '关闭' : '取消'}</Button>
          <Button disabled={busy || !docId} onClick={submit}>
            {busy ? '生成中…' : '开始生成'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============================================================
// 弹窗四：核对标准答案（§6.5）
// ============================================================

function SourceDialog({ payload, onClose }: {
  payload: EvalCaseSource | null
  onClose: () => void
}) {
  if (!payload) return null
  const snippet = payload.source_snippet ?? ''
  const [start, end] = payload.highlight ?? [-1, -1]
  // 「第 X 段」不存库，按片段内段落序号现算（片段内第几段）
  const para = start >= 0 ? snippet.slice(0, start).split(/\n\s*\n/).length : null

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader><DialogTitle>核对标准答案</DialogTitle></DialogHeader>
        <div className="space-y-4 text-sm">
          {/* 三个小标题加粗 —— 负责人明确要求（§6.5） */}
          <div>
            <p className="font-semibold">问题</p>
            <p className="mt-1">{payload.question}</p>
          </div>
          <div>
            <p className="font-semibold">标准答案</p>
            <p className="mt-1">{payload.ground_truth || '—'}</p>
          </div>
          <div>
            <p className="font-semibold">来源片段原文</p>
            <p className="mt-1 text-xs text-muted-foreground">
              模型就是照着这段出的题，对不上说明这条标准答案不能用
            </p>
            <div className="mt-2 rounded-md border border-border bg-muted/40 p-3">
              <p className="mb-2 text-xs text-muted-foreground">
                片段 {payload.source_chunk_id ? `#${payload.source_chunk_id}` : '—'}
                {para ? ` · 第 ${para} 段` : ''}
                {payload.source_page ? ` · 第 ${payload.source_page} 页` : ''}
                {payload.source_document_title ? ` · ${payload.source_document_title}` : ''}
              </p>
              <div className="max-h-80 overflow-y-auto whitespace-pre-wrap leading-relaxed">
                {snippet ? (
                  start >= 0 ? (
                    <>
                      {snippet.slice(0, start)}
                      <mark className="bg-yellow-200 dark:bg-yellow-900">{snippet.slice(start, end)}</mark>
                      {snippet.slice(end)}
                    </>
                  ) : snippet
                ) : (
                  <span className="text-muted-foreground">这条没有留下来源片段</span>
                )}
              </div>
              {snippet && start < 0 && (
                <p className="mt-2 text-xs text-amber-700 dark:text-amber-500">
                  标准答案与原文对不上 —— 常见原因是文档后来重新索引过（§8.3）
                </p>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
