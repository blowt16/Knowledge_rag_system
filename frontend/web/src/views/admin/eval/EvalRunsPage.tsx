/**
 * 批量评测（§6.3）—— 选评测集 → 跑一轮 → 看报告。
 *
 * 这个页面跑的是**线上完整链路**，不是纯向量基线（§5.4）：建 run 时只带
 * `set_id`，`config` 落库是 `{}`，服务端据此走线上默认。
 *
 * ⚠️ **「看报告」跑完前不给看**（决策 24）：半截的平均分最容易被截图当结论发出去。
 *    **宁可不出数，不出会误导的数。** 置灰的按钮必须配悬停提示、而且提示里要带进度 ——
 *    置灰的按钮点下去没有任何反馈，用户本来就容易以为按钮坏了；
 *    只写「完成后才能看」而不写跑到第几题，他完全不知道要等多久，只会反复来点。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  type EvalRunDetail,
  type EvalRunSummary,
  type EvalSetItem,
  deleteEvalRun,
  evalRun,
  evalRuns,
  listEvalSets,
  startEvalRun,
} from '@/api/eval'
import { ReportDialog } from './ReportDialog'

const PAGE_SIZE = 20

/** 状态中文映射（§6.3）。 */
const STATUS_LABEL: Record<string, string> = {
  pending: '排队中', running: '评测中', done: '已完成', failed: '失败',
}

/** 进度栏文案：跑满但还没 done，说明 ragas 整批算指标的那一段还在跑（§6.3）。 */
function progressText(r: EvalRunSummary): string {
  const total = r.total_cases ?? 0
  const done = r.done_cases ?? 0
  if (r.status === 'running' && total > 0 && done >= total) return '正在计算指标…'
  if (total > 0) return `${done}/${total}`
  return r.status === 'pending' ? '等待开始' : '—'
}

/** 「看报告」为什么不能点 —— 提示语里**要带进度**（决策 24）。 */
function reportHint(r: EvalRunSummary): string | undefined {
  if (r.status === 'pending' || r.status === 'running') {
    const total = r.total_cases ?? 0
    const done = r.done_cases ?? 0
    const at = total > 0 ? `（${done}/${total}）` : ''
    return `评测还在跑${at}，完成后才能看报告`
  }
  return undefined
}

export default function EvalRunsPage() {
  const [sets, setSets] = useState<EvalSetItem[]>([])
  const [setId, setSetId] = useState('')
  const [role, setRole] = useState<'student' | 'staff' | 'admin'>('student')
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState<EvalRunDetail | null>(null)
  const [pendingDelete, setPendingDelete] = useState<EvalRunSummary | null>(null)
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    const r = await evalRuns({ page, page_size: PAGE_SIZE })
    setRuns(r.items ?? [])
    setTotal(r.total ?? 0)
    return r.items ?? []
  }, [page])

  useEffect(() => {
    listEvalSets()
      .then(r => {
        const items = r.items ?? []
        setSets(items)
        if (items.length && !setId) setSetId(items[0].id)
      })
      .catch((e: Error) => setMsg(`加载评测集失败：${e.message}`))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { refresh().catch(() => setMsg('加载任务失败')) }, [refresh])

  /**
   * 有跑着的轮次就轮询。
   * ⚠️ 用 **1.5 秒**而不是老的 5 秒：进度条要逐题动起来才看得出「没卡住」，
   *    5 秒一跳会让人以为死了（§6.3 定的 1~2 秒）。
   */
  const ensurePolling = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = window.setInterval(async () => {
      const items = await refresh().catch(() => null)
      if (items && !items.some(r => r.status === 'pending' || r.status === 'running')) {
        window.clearInterval(pollRef.current!)
        pollRef.current = null
      }
    }, 1500)
  }, [refresh])

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current)
  }, [])

  const start = async () => {
    if (!setId) { setMsg('先选一个评测集'); return }
    setBusy(true); setMsg('')
    try {
      const r = await startEvalRun({ set_id: setId, role })
      setMsg(`已起：${r.config_label}（${r.run_id.slice(0, 8)}）`)
      setPage(1)
      await refresh()
      ensurePolling()
    } catch (e) {
      setMsg(`起评测失败：${(e as Error).message}`)
    } finally { setBusy(false) }
  }

  const openReport = async (runId: string) => {
    setMsg('')
    try { setDetail(await evalRun(runId)) }
    catch (e) { setMsg(`打开报告失败：${(e as Error).message}`) }
  }

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const selected = sets.find(s => s.id === setId) ?? null

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-1">
              评测集
              <select className="min-w-56 rounded border border-border bg-background px-2 py-1"
                      value={setId} onChange={e => setSetId(e.target.value)}>
                {sets.length === 0 && <option value="">（还没有评测集）</option>}
                {sets.map(s => (
                  <option key={s.id} value={s.id}>{s.name}（{s.case_count} 条用例）</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1">
              角色
              <select className="rounded border border-border bg-background px-2 py-1"
                      value={role} onChange={e => setRole(e.target.value as typeof role)}>
                <option value="student">学生</option>
                <option value="staff">教职工</option>
                <option value="admin">管理员</option>
              </select>
            </label>
            <Button disabled={busy || !setId} onClick={start}>开始评测</Button>
          </div>
          {/* ⚠️ 提示文案**不编数字** —— 参考图里的「六次模型」「几十万 token」
              本仓库没有依据（§6.3） */}
          <p className="text-xs text-muted-foreground">
            评测会逐条调用模型和重排，一轮下来是分钟级。建议先用 5~10 条验证方向。
          </p>
          <p className="text-xs text-muted-foreground">
            跑的是<b>线上完整链路</b>；要跑提权对照或开消融开关，去「消融对比」页。
          </p>
          {selected && selected.in_eval_count === 0 && (
            <p className="text-sm text-amber-700 dark:text-amber-500">
              「{selected.name}」里没有勾选「参与评测」的用例，直接跑会失败。
            </p>
          )}
          {msg && <p className="text-sm text-muted-foreground">{msg}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 pt-6">
          {runs.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">还没有评测记录</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-3">任务ID</th>
                    <th className="py-2 pr-3">评测集</th>
                    <th className="py-2 pr-3">状态</th>
                    <th className="py-2 pr-3">进度</th>
                    <th className="py-2 pr-3">失败原因</th>
                    <th className="py-2 pr-3">耗时(ms)</th>
                    <th className="py-2 pr-3">时间</th>
                    <th className="py-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map(r => {
                    const hint = reportHint(r)
                    return (
                      <tr key={r.run_id} className="border-t border-border">
                        <td className="py-2 pr-3 font-mono text-xs">{r.run_id.slice(0, 8)}</td>
                        {/* 老 `suite` 路径的 run 没有评测集，退回配置标签 */}
                        <td className="py-2 pr-3">{r.set_name ?? r.config_label}</td>
                        <td className="py-2 pr-3">{STATUS_LABEL[r.status] ?? r.status}</td>
                        <td className="py-2 pr-3">{progressText(r)}</td>
                        <td className="max-w-xs py-2 pr-3 text-muted-foreground">{r.error || ''}</td>
                        <td className="py-2 pr-3">{r.duration_ms ?? '—'}</td>
                        <td className="py-2 pr-3 whitespace-nowrap text-xs">
                          {r.started_at ? new Date(r.started_at).toLocaleString() : '—'}
                        </td>
                        <td className="whitespace-nowrap py-2">
                          {/* 跑完才可点；提示语带进度（决策 24）。
                              `disabled` 的元素收不到 hover，所以外面的 span 挂在 title 上 */}
                          <span title={hint}>
                            <Button variant="link" disabled={Boolean(hint)}
                                    onClick={() => openReport(r.run_id)}>看报告</Button>
                          </span>
                          <Button variant="link" onClick={() => setPendingDelete(r)}>删除</Button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {pages > 1 && (
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

      <AlertDialog open={!!pendingDelete} onOpenChange={o => !o && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除这一轮评测？</AlertDialogTitle>
            <AlertDialogDescription>
              逐题结果会一起删掉，之后<b>也不能再出现在消融对比表里</b>。删就是删，没法撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={async () => {
              const r = pendingDelete!
              setPendingDelete(null)
              try {
                await deleteEvalRun(r.run_id)
                if (runs.length === 1 && page > 1) setPage(p => p - 1)
                else await refresh()
                setMsg('已删除')
              } catch (e) { setMsg(`删除失败：${(e as Error).message}`) }
            }}>确认删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {detail && <ReportDialog detail={detail} onClose={() => setDetail(null)} />}
    </div>
  )
}
