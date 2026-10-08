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

const RAGAS_KEYS = ['faithfulness', 'answer_relevancy', 'context_precision', 'context_recall']

/**
 * 这一轮 ragas 出没出问题 —— 没问题返回 `null`。
 *
 * ⚠️ **状态是「已完成」不等于报告是完整的。** 一轮评测分两段：
 *    ① 逐题跑链路（检索 + 生成）→ 有成有败，这部分决定 `status`；
 *    ② 整批算 ragas 四项 → 它挂了**不会**让整轮变 `failed`，四项只是空着。
 *    这是有意的：这轮确实跑完了、逐题明细是能用的，把整轮标成「失败」是撒谎。
 *    但**界面上必须看得出来** —— 原来任务表完全不看 ragas 状态，
 *    于是一轮四项指标全空、综合得分是「—」的评测，在列表里和正常的一模一样，
 *    点进报告才知道。这就是 §7.1 说的「做哑谜」。
 */
type RagasFlag = { kind: 'warn' | 'info'; text: string; why: string }

function ragasIssue(metrics: Record<string, unknown> | null | undefined): RagasFlag | null {
  if (!metrics || metrics.cases === undefined) return null   // 老轮次没这些字段
  const errs = (metrics.ragas_errors as string[] | undefined) ?? []
  const ragas = (metrics.ragas as Record<string, unknown> | undefined) ?? {}
  const scored = RAGAS_KEYS.some(k => typeof ragas[k] === 'number')
  if (scored && errs.length === 0) return null               // 四项有数 → 不打扰

  // ① 环境不在 / ② 跑了但有指标没算出来 —— 这两种是**出问题了**
  if (metrics.ragas_available === false) {
    return { kind: 'warn', text: '指标未算出',
             why: errs.length ? `ragas 隔离环境不可用：${errs[0]}` : 'ragas 隔离环境不可用' }
  }
  if (errs.length) return { kind: 'warn', text: '指标未算出', why: errs[0] }

  // ③ 环境好、没报错、四项却都空 —— 这一轮**本来就没有可评分的样本**。
  //    ⚠️ 这不是故障（比如校准小集里 12 条拒答题都没有标准答案），
  //       标成和上面一样的黄色告警就是「狼来了」，看两天就没人当回事了。
  return { kind: 'info', text: '无可评分样本',
           why: '这一轮没有可评分的样本（缺标准答案或检索上下文），所以四项指标都是空的' }
}

/** 「已完成」旁边的小标记。**不改状态**，只是把「报告不完整」摆到台面上。 */
function RagasMark({ metrics }: { metrics: Record<string, unknown> | null | undefined }) {
  const flag = ragasIssue(metrics)
  if (!flag) return null
  const tone = flag.kind === 'warn'
    ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
    : 'bg-muted text-muted-foreground'
  return (
    <span className={`ml-2 whitespace-nowrap rounded px-1.5 py-0.5 text-xs ${tone}`}
          title={`四项 ragas 指标是空的：${flag.why}`}>
      {flag.text}
    </span>
  )
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

  // ⚠️ 定时器要取**最新**的 refresh，不能抱住启动那一刻的闭包 ——
  //    它绑着当时的 `page`，用户在评测期间翻页会被每 1.5 秒打回上一页。
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh

  /**
   * 有跑着的轮次就轮询。
   * ⚠️ 用 **1.5 秒**而不是老的 5 秒：进度条要逐题动起来才看得出「没卡住」，
   *    5 秒一跳会让人以为死了（§6.3 定的 1~2 秒）。
   */
  const ensurePolling = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = window.setInterval(async () => {
      const items = await refreshRef.current().catch(() => null)
      if (items && !items.some(r => r.status === 'pending' || r.status === 'running')) {
        window.clearInterval(pollRef.current!)
        pollRef.current = null
      }
    }, 1500)
  }, [])

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

  useEffect(() => {
    refresh()
      .then(items => {
        // ⚠️ 一进页面就发现有跑着的轮次 → 也得开始轮询。
        //    只在「本页点过开始评测」时才起定时器的话，**刷一下浏览器**
        //    进度条就冻在那一刻 —— 而这正是「看进度」最常见的用法。
        if (items.some(r => r.status === 'pending' || r.status === 'running')) {
          ensurePolling()
        }
      })
      .catch(() => setMsg('加载任务失败'))
  }, [refresh, ensurePolling])

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
                        <td className="py-2 pr-3">
                          {STATUS_LABEL[r.status] ?? r.status}
                          <RagasMark metrics={r.metrics} />
                        </td>
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
                          {/* 运行中不给删：删行不会停掉后台任务，它还在占 GPU ——
                              服务端也会 409，这里先挡住，别让用户白点一次 */}
                          <span title={hint ?? '删除这一轮（逐题结果会一起删）'}>
                            <Button variant="link" disabled={Boolean(hint)}
                                    onClick={() => setPendingDelete(r)}>删除</Button>
                          </span>
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
