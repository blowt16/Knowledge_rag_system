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
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
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
  getEvalCase,
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
    <div className="flex h-full min-h-0 flex-col gap-6">
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
      <Card className="flex min-h-0 flex-1 flex-col gap-0">
        {/* 工具栏单独一块，底部分隔线与下面的表格分开 */}
        <div className="border-b border-border px-4 pb-4">
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
          {msg && <p className="mt-3 text-sm text-muted-foreground">{msg}</p>}
        </div>

        <CardContent className="flex min-h-0 flex-1 flex-col gap-4 pt-4">
          {!current ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              先在上面的下拉里选一个评测集，或者新建一个
            </p>
          ) : cases.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {query || sourceFilter ? '没有符合条件的用例' : '这个评测集还没有用例'}
            </p>
          ) : (
            <div className="-mx-4 min-h-40 flex-1 overflow-auto [&_[data-slot=table-container]]:overflow-visible">
              {/* 表格去掉外框、直接平铺在卡片内（-mx-4 抵消 CardContent 的左右内边距）。
                  外层只负责纵向滚动；内层 shadcn Table 自带的 overflow-x-auto 要改回 visible，
                  否则吸顶表头会被那个内层滚动容器挡住。 */}
              <Table>
                <TableHeader className="sticky top-0 z-10 bg-card">
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="pl-3">问题</TableHead>
                    <TableHead>标准答案</TableHead>
                    <TableHead>来源</TableHead>
                    <TableHead>参与评测</TableHead>
                    <TableHead>备注</TableHead>
                    <TableHead>操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="[&_td]:align-top">
                  {cases.map(c => (
                    <TableRow key={c.id}>
                      <TableCell className="max-w-md whitespace-normal! pl-3">
                        {c.question}
                        {(c.turn_count ?? 0) >= 2 && (
                          <span className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs
                                           text-blue-800 dark:bg-blue-900/40
                                           dark:text-blue-300">
                            多轮 {c.turn_count} 轮
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="max-w-xs whitespace-normal! text-muted-foreground">
                        {(c.turn_count ?? 0) >= 2
                          ? `逐轮答案（${c.turn_count} 轮）`
                          : (c.ground_truth || '—')}
                      </TableCell>
                      <TableCell>{SOURCE_LABEL[c.source] ?? c.source}</TableCell>
                      <TableCell>
                        <span className={`rounded px-1.5 py-0.5 text-xs ${
                          c.in_eval
                            ? 'bg-success/10 text-success'
                            : 'bg-muted text-muted-foreground'}`}>
                          {c.in_eval ? '是' : '否'}
                        </span>
                      </TableCell>
                      <TableCell className="max-w-xs whitespace-normal! text-muted-foreground">{c.note || ''}</TableCell>
                      <TableCell className="space-x-1">
                        <SourceButton item={c} busy={busy}
                                      onOpen={setSourceOf}
                                      onError={m => setMsg(m)} />
                        <Button variant="info" size="xs" disabled={busy}
                                onClick={() => setCaseDialog({ mode: 'edit', item: c })}>编辑</Button>
                        <Button variant="destructive-solid" size="xs" disabled={busy}
                                onClick={() => setPendingDelete({ kind: 'case', item: c })}>
                          删除
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
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
                        onFinished={async () => {
                          setMsg('生成完成，列表已刷新')
                          await reload()
                        }} />
      )}

      <SourceDialog payload={sourceOf} onClose={() => setSourceOf(null)} />
    </div>
  )
}

/** 「看原文」—— 判据是 `source_snippet`，不是 `source`（见文件头）。 */
function SourceButton({ item, busy, onOpen, onError }: {
  item: EvalCaseItem
  busy: boolean
  onOpen: (p: EvalCaseSource) => void
  onError: (msg: string) => void
}) {
  const hasSnippet = Boolean(item.source_chunk_id || item.source_page)
  // 老 90 条迁移后 source='generated' 却没有来源片段 —— 提示语要分清这两种，
  // 否则会出现「列表里标着『文档生成』、点开被告知『这是手工录入』」的自相矛盾
  const hint = item.source === 'manual'
    ? '手工录入的用例没有来源片段'
    : '这条是早期导入的题库，没有留下来源片段'
  return (
    <Button variant="success" size="xs" disabled={busy || !hasSnippet}
            title={hasSnippet ? undefined : hint}
            onClick={async () => {
              // ⚠️ 必须自己报错：全局没有任何错误提示，沉默的按钮
              //    在用户眼里就是「点了没反应」= 坏了
              try { onOpen(await evalCaseSource(item.id)) }
              catch (e) { onError(`打开原文失败：${(e as Error).message}`) }
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

interface TurnDraft {
  question: string
  ground_truth: string
}

function CaseDialog({ state, setId, onClose, onDone }: {
  state: { mode: 'create' | 'edit'; item?: EvalCaseItem }
  setId: string
  onClose: () => void
  onDone: (msg: string) => void
}) {
  const editing = state.mode === 'edit'
  const [caseType, setCaseType] = useState<'factual' | 'multi_turn'>(
    (state.item?.turn_count ?? 0) >= 2 ? 'multi_turn' : 'factual')
  const [question, setQuestion] = useState(state.item?.question ?? '')
  const [groundTruth, setGroundTruth] = useState(state.item?.ground_truth ?? '')
  const [turns, setTurns] = useState<TurnDraft[]>([])
  const [inEval, setInEval] = useState(state.item?.in_eval ?? true)
  const [note, setNote] = useState(state.item?.note ?? '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)

  // 列表不返回整段 turns，编辑多轮题时按 id 拉详情
  useEffect(() => {
    if (!editing || !state.item) return
    setLoading(true)
    getEvalCase(state.item.id)
      .then((d) => {
        setCaseType(d.case_type === 'multi_turn' ? 'multi_turn' : 'factual')
        if (d.turns?.length) {
          setTurns(d.turns.map((t) => ({
            question: t.question,
            ground_truth: t.ground_truth ?? '',
          })))
        }
      })
      .catch((e: Error) => setErr(e.message))
      .finally(() => setLoading(false))
  }, [editing, state.item])

  const switchType = (next: 'factual' | 'multi_turn') => {
    if (next === caseType) return
    if (next === 'multi_turn') {
      const first = { question, ground_truth: groundTruth }
      setTurns((prev) => (prev.length >= 2
        ? prev
        : [first, { question: '', ground_truth: '' }]))
    } else if (turns[0]) {
      setQuestion(turns[0].question)
      setGroundTruth(turns[0].ground_truth)
    }
    setCaseType(next)
  }

  const submit = async () => {
    setBusy(true); setErr('')
    try {
      if (caseType === 'multi_turn') {
        if (turns.length < 2) throw new Error('多轮用例至少需要 2 轮')
        if (turns.some((t) => !t.question.trim())) throw new Error('每一轮都要填问题')
        const body = {
          in_eval: inEval,
          note: note || null,
          turns: turns.map((t) => ({
            question: t.question.trim(),
            ground_truth: t.ground_truth || null,
          })),
        }
        if (editing && state.item) await patchEvalCase(state.item.id, body)
        else await createEvalCase(setId, { ...body, case_type: 'multi_turn' })
      } else {
        if (!question.trim()) throw new Error('问题不能为空')
        const body = {
          question: question.trim(),
          ground_truth: groundTruth || null,
          in_eval: inEval,
          note: note || null,
        }
        if (editing && state.item) await patchEvalCase(state.item.id, body)
        else await createEvalCase(setId, { ...body, case_type: 'factual' })
      }
      onDone(editing ? '已保存' : '已录入一条用例')
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent className="max-h-[88vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader><DialogTitle>{editing ? '编辑用例' : '评测用例'}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">类型</span>
            {editing ? (
              <span className="rounded bg-muted px-2 py-1 text-xs">
                {caseType === 'multi_turn' ? '多轮用例（不可修改）' : '单轮用例'}
              </span>
            ) : (
              <>
                <Button size="sm" variant={caseType === 'factual' ? 'default' : 'outline'}
                        onClick={() => switchType('factual')}>单轮</Button>
                <Button size="sm" variant={caseType === 'multi_turn' ? 'default' : 'outline'}
                        onClick={() => switchType('multi_turn')}>多轮</Button>
              </>
            )}
          </div>

          {caseType === 'multi_turn' ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>轮次（至少 2 轮）</Label>
                <Button variant="outline" size="xs"
                        onClick={() => setTurns((v) => [...v, {
                          question: '', ground_truth: '',
                        }])}>
                  增加一轮
                </Button>
              </div>
              {loading && <p className="text-sm text-muted-foreground">加载轮次中…</p>}
              {turns.map((turn, i) => (
                <div key={i} className="space-y-1 rounded-md border border-border p-2">
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>第 {i + 1} 轮</span>
                    {turns.length > 2 && (
                      <button type="button" className="link"
                              onClick={() => setTurns((v) => v.filter((_, j) => j !== i))}>
                        删除
                      </button>
                    )}
                  </div>
                  <Textarea rows={2} placeholder="问题" value={turn.question}
                            onChange={e => setTurns((v) => v.map((x, j) =>
                              j === i ? { ...x, question: e.target.value } : x))} />
                  <Textarea rows={2} placeholder="标准答案（可为空）"
                            value={turn.ground_truth}
                            onChange={e => setTurns((v) => v.map((x, j) =>
                              j === i ? { ...x, ground_truth: e.target.value } : x))} />
                  {!turn.ground_truth.trim() && (
                    <p className="text-xs text-amber-600">
                      第 {i + 1} 轮没有标准答案，该轮上下文指标不会计入
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <>
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
            </>
          )}

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
          <Button disabled={busy || loading} onClick={submit}>
            {busy ? '保存中…' : '保存'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============================================================
// 弹窗三：从文档自动生成用例（§8）
// ============================================================

/**
 * 从文档自动生成用例（§8）—— **三段式**：配置 → 进度 → 结果。
 *
 * ⚠️ 为什么拆成两个弹窗而不是在同一个里显示「生成中…」：
 *    一轮最长 120 秒（决策 18 的服务端硬超时），而弹窗里点完按钮之后
 *    整块界面是**纹丝不动**的 —— 那和按钮坏了看不出区别。分成独立进度弹窗之后，
 *    至少时间在动、界面明确在「等」，而不是卡在配置表单上。
 *
 * ⚠️ **进度弹窗不给关**（没有 ×、Esc 也拦掉），**也不给「取消」**：
 *    后端 `POST /generate` 是同步接口，取消前端请求**不会停掉服务端**，
 *    题照样入库。给一个按了没用的取消键是骗人。等它出结果就行（最长 120 秒）。
 *
 * ⚠️ 失败也走**结果弹窗**，不静默关掉 —— 关掉了用户不知道刚才发生了什么。
 */
type GenPhase =
  | { step: 'config' }
  | { step: 'running'; requested: number; startedAt: number }
  | { step: 'result'; result: EvalGenerateResponse | null; error: string }

function GenerateDialog({ setId, setName, onClose, onFinished }: {
  setId: string
  setName: string
  onClose: () => void
  onFinished: () => Promise<void>
}) {
  // ⚠️ 弹窗里**没有「知识库」这一级**（决策 5）—— 项目没有知识库概念，直接选文档
  const [docs, setDocs] = useState<DocumentItem[]>([])
  const [docId, setDocId] = useState('')
  const [count, setCount] = useState(5)
  const [mode, setMode] = useState<'single' | 'multi'>('single')
  const [instruction, setInstruction] = useState('')
  const [turns, setTurns] = useState(3)
  const [windowSize, setWindowSize] = useState(3)
  const [err, setErr] = useState('')
  const [phase, setPhase] = useState<GenPhase>({ step: 'config' })
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    listDocuments({ status: 'active', page: 1, page_size: 100 })
      .then(r => {
        const items = r.items ?? []
        setDocs(items)
        if (items.length) setDocId(items[0].id)
      })
      .catch((e: Error) => setErr(`加载文档失败：${e.message}`))
  }, [])

  // 跑秒。一个不动的「正在生成中」分不出「在跑」和「卡死」，时间在跳才看得出在等。
  useEffect(() => {
    if (phase.step !== 'running') return
    setElapsed(0)
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - phase.startedAt) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [phase])

  const submit = async () => {
    if (!docId) { setErr('先选一份文档'); return }
    const requested = Math.min(MAX_GENERATE, Math.max(1, count))
    // 配置弹窗立刻关掉、换成进度弹窗
    setPhase({ step: 'running', requested, startedAt: Date.now() })
    try {
      const result = await generateEvalCases(setId, {
        document_id: docId, count: requested, mode,
        instruction: instruction.trim() || null,
        ...(mode === 'multi' ? { turns, window: windowSize } : {}),
      })
      setPhase({ step: 'result', result, error: '' })
    } catch (e) {
      setPhase({ step: 'result', result: null, error: (e as Error).message })
    }
  }

  const finish = async () => {
    onClose()
    await onFinished()
  }

  // ---- ① 配置 ----
  if (phase.step === 'config') {
    return (
      <Dialog open onOpenChange={v => { if (!v) onClose() }}>
        <DialogContent>
          <DialogHeader><DialogTitle>从文档自动生成用例</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              生成到「{setName}」。模型照着文档片段出题，
              <b>标准答案的每个实质分句都必须是原文</b>、问句不带代词 ——
              过不了校验的那条就不生成。
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
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">模式</span>
              <Button size="sm" variant={mode === 'single' ? 'default' : 'outline'}
                      onClick={() => setMode('single')}>单轮</Button>
              <Button size="sm" variant={mode === 'multi' ? 'default' : 'outline'}
                      onClick={() => setMode('multi')}>多轮</Button>
            </div>
            <div className="space-y-1">
              <Label htmlFor="gen-count">生成条数（最多 {MAX_GENERATE}）</Label>
              <Input id="gen-count" type="number" min={1} max={MAX_GENERATE} value={count}
                     onChange={e => setCount(Number(e.target.value))} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="gen-instruction">生成提示词（可选）</Label>
              <Textarea id="gen-instruction" rows={3} maxLength={500}
                        value={instruction}
                        placeholder={mode === 'single'
                          ? '只出条件/时限类事实题，问法贴近学生口语'
                          : '围绕转专业的申请条件与材料，生成连续追问'}
                        onChange={e => setInstruction(e.target.value)} />
              <p className="text-xs text-muted-foreground">
                {instruction.length}/500 · 只影响选题与问法，不会放宽逐句原文校验
              </p>
            </div>
            {mode === 'multi' && (
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label htmlFor="gen-turns">每例轮数（2~4）</Label>
                  <Input id="gen-turns" type="number" min={2} max={4} value={turns}
                         onChange={e => setTurns(Number(e.target.value))} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="gen-window">窗口大小（2~3 个 chunk）</Label>
                  <Input id="gen-window" type="number" min={2} max={3} value={windowSize}
                         onChange={e => setWindowSize(Number(e.target.value))} />
                </div>
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              {mode === 'multi'
                ? '多轮生成按相邻 chunk 窗口出题，耗时更长；标准答案严格取自窗口原文。'
                : '单轮生成的标准答案同样要求逐句原文，最多 3 个分句。'}
            </p>
            {err && <p className="text-sm text-destructive">{err}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>取消</Button>
            <Button disabled={!docId} onClick={submit}>开始生成</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- ② 进度（不可关闭、无取消）----
  if (phase.step === 'running') {
    return (
      <Dialog open onOpenChange={() => { /* 生成中不给关：见组件头 */ }}>
        <DialogContent showCloseButton={false}>
          <DialogHeader><DialogTitle>正在生成</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <p className="text-sm">
              正在为「{setName}」生成 {phase.requested} 条{mode === 'multi' ? '多轮' : ''}用例…
            </p>
            <p className="text-2xl font-semibold tabular-nums">已用 {elapsed} 秒</p>
            <p className="text-xs text-muted-foreground">
              要逐条问模型并校验，单轮最多等 2 分钟，多轮最多 3 分钟。
              <b>这一步不能取消</b> —— 请求发出后题就已经在生成了，
              中途关掉页面它们照样会入库。
            </p>
          </div>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- ③ 结果 ----
  const r = phase.result
  const short = r ? r.created < r.requested : false
  const title = phase.error
    ? '生成失败'
    : short ? `生成完成（${r!.created}/${r!.requested}）` : '生成完成'
  const notes: string[] = []
  if (r) {
    if (r.failed > 0) {
      notes.push(mode === 'multi'
        ? `${r.failed} 条没通过校验（轮数 2~4、首轮无代词、每轮答案逐句原文、答案不重复）`
        : `${r.failed} 条没通过校验（标准答案不是逐句原文，或问句里带了代词）`)
    }
    if (r.timeout) notes.push('等超时了，已先生成的都留着；剩下的可以再点一次「开始生成」')
    if (r.reason && r.created === 0) notes.push(r.reason)
  }

  return (
    <Dialog open onOpenChange={v => { if (!v) void finish() }}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          {phase.error && <p className="text-sm text-destructive">{phase.error}</p>}
          {r && (
            <p className="text-sm">
              要了 {r.requested} 条，实际生成 <b>{r.created}</b> 条。
            </p>
          )}
          {notes.map((n, i) => (
            <p key={i} className="text-xs text-amber-700 dark:text-amber-500">{n}</p>
          ))}
          {r && r.cases.length > 0 && (
            <div className="max-h-72 space-y-3 overflow-y-auto rounded-md border border-border p-3">
              {r.cases.map((c, i) => (
                <div key={c.id} className="text-sm">
                  <p className="font-medium">
                    {i + 1}. {c.question}
                    {(c.turn_count ?? 1) > 1 && (
                      <span className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs
                                       text-blue-800 dark:bg-blue-900/40
                                       dark:text-blue-300">
                        多轮 {c.turn_count} 轮
                      </span>
                    )}
                  </p>
                  {(c.turn_count ?? 1) <= 1 && (
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      标准答案：{c.ground_truth || '—'}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
          {/* ⚠️ 只有**真的生成了**才说「已入库」——
              失败时（比如文档不存在）一条都没建，写上去就是骗人。
              「没生成够」也照样适用这句：已经生成的那几条确实入库了。 */}
          {r && r.created > 0 && (
            <p className="text-xs text-muted-foreground">
              这些用例已经入库。质量参差，可以逐条点「看原文」核验，不满意的删掉。
            </p>
          )}
        </div>
        <DialogFooter>
          <Button onClick={() => void finish()}>确定</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============================================================
// 弹窗四：核对标准答案（§6.5）
// ============================================================

/** Chroma 的 chunk id 形如 `<文档 id>:<片段序号>` —— 取序号那一截；取不到就退回 `—`。 */
function segNo(chunkId: string | null | undefined): string {
  if (!chunkId) return '—'
  const i = chunkId.lastIndexOf(':')
  return i >= 0 && i < chunkId.length - 1 ? `#${chunkId.slice(i + 1)}` : '—'
}

function SourceDialog({ payload, onClose }: {
  payload: EvalCaseSource | null
  onClose: () => void
}) {
  if (!payload) return null
  const snippet = payload.source_snippet ?? ''
  const turns = payload.turns ?? []
  const turnHighlights = payload.turn_highlights ?? []
  // 多轮题没有用例级标准答案；用第一轮高亮定位片段
  const primary = payload.highlight ?? turnHighlights[0] ?? null
  const [start, end] = primary ?? [-1, -1]
  // 「第 X 段」不存库，按片段内段落序号现算（片段内第几段）
  const para = start >= 0 ? snippet.slice(0, start).split(/\n\s*\n/).length : null

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader><DialogTitle>核对标准答案</DialogTitle></DialogHeader>
        <div className="space-y-4 text-sm">
          {/* 三个小标题加粗 —— 负责人明确要求（§6.5） */}
          <div>
            <p className="font-semibold">问题</p>
            <p className="mt-1">{payload.question}</p>
          </div>
          {turns.length > 0 ? (
            <div>
              <p className="font-semibold">逐轮答案（{turns.length} 轮）</p>
              <div className="mt-1 space-y-2">
                {turns.map((t, i) => {
                  const span = turnHighlights[i]
                  return (
                    <div key={i} className="rounded-md border border-border p-2">
                      <p className="font-medium">第 {i + 1} 轮：{t.question}</p>
                      <p className="mt-1 text-muted-foreground">
                        {t.ground_truth || '—'}
                      </p>
                      {span ? (
                        <p className="mt-1 text-xs text-muted-foreground">
                          原文高亮：<mark className="bg-yellow-200 dark:bg-yellow-900">
                            {snippet.slice(span[0], span[1])}
                          </mark>
                        </p>
                      ) : (
                        <p className="mt-1 text-xs text-amber-700 dark:text-amber-500">
                          这一轮的答案在来源片段里对不上
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ) : (
            <div>
              <p className="font-semibold">标准答案</p>
              <p className="mt-1">{payload.ground_truth || '—'}</p>
            </div>
          )}
          <div>
            <p className="font-semibold">来源片段原文</p>
            <p className="mt-1 text-xs text-muted-foreground">
              模型就是照着这段出的题，对不上说明这条标准答案不能用
            </p>
            <div className="mt-2 rounded-md border border-border bg-muted/40 p-3">
              {/* ⚠️ 只显示**片段序号**，不铺整串 chunk id。
                  Chroma 的 id 是 `<文档 id>:<片段序号>`（34 个字符），原样打在
                  定位行上会把真正有用的「第几段 / 第几页」挤到中间，整行全是噪声。
                  完整 id 放悬停里 —— 真要拿它去核对分块预览的人也拿得到。*/}
              <p className="mb-2 text-xs text-muted-foreground"
                 title={payload.source_chunk_id ?? undefined}>
                片段 {segNo(payload.source_chunk_id)}
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
