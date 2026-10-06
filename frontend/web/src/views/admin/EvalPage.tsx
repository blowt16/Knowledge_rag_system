/**
 * 评测页（§4.3.1.4 消融对比表 + §3.7.3 评测接口）—— **M5 交付**。
 *
 * 三块：
 *   1. 起一轮评测（全量 / 校准小集；角色；是否提权）
 *   2. 历史轮次（轮询到 `done`）
 *   3. 消融对比表：勾几轮 → 服务端给对齐后的矩阵
 *
 * ⚠️ **对比表不由前端拼**：行是配置变体、列是指标全集，而指标全集由服务端掌握
 *    （不同 run 可能缺指标、指标名会演进）。`config_label` 也由服务端派生，
 *    前端自己从 config 拼标签会因开关命名演进导致同一次运行换个名字（§4.3.1.4）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  type EvalCompareResponse,
  type EvalRunSummary,
  evalCompare,
  evalRuns,
  startEvalRun,
} from '@/api/eval'

/** 指标的中文名（**展示用**，值一律来自服务端）。 */
const METRIC_LABEL: Record<string, string> = {
  recall_at_k: 'Recall@5',
  mrr: 'MRR',
  avg_ms: '平均耗时(ms)',
  refusal_rate: '拒答率',
  unauthorized_hits: '越权返回',
  uncited_ratio: '无依据结论占比',
  citation_invalid: '引用越界',
  cases: '题数',
  failed: '失败题数',
  answered_count: '作答题数',
  knowledge_count: 'knowledge 轮次',
  faithfulness: 'Faithfulness',
  answer_relevancy: 'Answer Relevancy',
  context_precision: 'Context Precision',
  context_recall: 'Context Recall',
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4)
  return String(v)
}

/** `metrics.ragas.<key>` —— metrics 是自由 JSON，取值要收窄。 */
function ragasValue(metrics: Record<string, unknown> | null | undefined, key: string) {
  const ragas = metrics?.ragas as Record<string, unknown> | undefined
  return ragas?.[key]
}

export default function EvalPage() {
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [compare, setCompare] = useState<EvalCompareResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [role, setRole] = useState<'student' | 'staff' | 'admin'>('student')
  const [restricted, setRestricted] = useState(false)
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    const data = await evalRuns(30)
    setRuns(data.items)
    return data.items
  }, [])

  useEffect(() => {
    refresh().catch(() => setMsg('加载历史失败'))
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [refresh])

  /** 有跑着的轮次就轮询 —— 服务端不做进度流，轮询足够（§3.7.3）。 */
  const ensurePolling = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = window.setInterval(async () => {
      const items = await refresh().catch(() => null)
      if (items && !items.some(r => r.status === 'pending' || r.status === 'running')) {
        window.clearInterval(pollRef.current!)
        pollRef.current = null
      }
    }, 5000)
  }, [refresh])

  const start = async (suite: 'full' | 'refusal_calib') => {
    setBusy(true)
    setMsg('')
    try {
      const r = await startEvalRun({
        name: suite === 'refusal_calib' ? `校准小集（${role}）` : `全量（${role}）`,
        suite,
        role,
        include_restricted: restricted,
      })
      setMsg(`已起：${r.config_label}（${r.run_id.slice(0, 8)}）`)
      await refresh()
      ensurePolling()
    } catch (e) {
      setMsg(`起评测失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const doCompare = async () => {
    if (selected.length < 2) {
      setMsg('至少勾两轮才能对比')
      return
    }
    try {
      setCompare(await evalCompare(selected))
      setMsg('')
    } catch (e) {
      setMsg(`对比失败：${(e as Error).message}`)
    }
  }

  const toggle = (id: string) =>
    setSelected(prev => (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]))

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader><CardTitle className="text-base">起一轮评测</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-1">
              角色
              <select className="rounded border border-border bg-background px-2 py-1"
                      value={role} onChange={e => setRole(e.target.value as typeof role)}>
                <option value="student">学生</option>
                <option value="staff">教职工</option>
                <option value="admin">管理员</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={restricted}
                     onChange={e => setRestricted(e.target.checked)} />
              提权（admin 才有效）
            </label>
            <Button disabled={busy} onClick={() => start('full')}>跑全量</Button>
            <Button disabled={busy} variant="outline"
                    onClick={() => start('refusal_calib')}>跑校准小集</Button>
          </div>
          {msg && <p className="text-sm text-muted-foreground">{msg}</p>}
          <p className="text-xs text-muted-foreground">
            评测在后台跑，进度看下面列表的状态；同一时刻只允许一轮（要占 GPU）。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">历史轮次</CardTitle></CardHeader>
        <CardContent>
          {runs.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">还没有评测记录</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="w-8 py-2"></th>
                    <th className="py-2">配置</th>
                    <th className="py-2">角色</th>
                    <th className="py-2">状态</th>
                    <th className="py-2">Recall@5</th>
                    <th className="py-2">MRR</th>
                    <th className="py-2">Faithfulness</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map(r => (
                    <tr key={r.run_id} className="border-t border-border">
                      <td className="py-2">
                        <input type="checkbox" checked={selected.includes(r.run_id)}
                               onChange={() => toggle(r.run_id)} />
                      </td>
                      <td className="py-2">{r.config_label}</td>
                      <td className="py-2">{r.role}</td>
                      <td className="py-2">
                        {r.status === 'running' ? '运行中…' : r.status}
                      </td>
                      <td className="py-2">{fmt(r.metrics?.recall_at_k)}</td>
                      <td className="py-2">{fmt(r.metrics?.mrr)}</td>
                      <td className="py-2">{fmt(ragasValue(r.metrics, "faithfulness"))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="mt-3">
            <Button variant="outline" onClick={doCompare}>对比勾选的轮次</Button>
          </div>
        </CardContent>
      </Card>

      {compare && (
        <Card>
          <CardHeader><CardTitle className="text-base">消融对比表</CardTitle></CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-2">配置</th>
                  {compare.metrics.map(m => (
                    <th key={m} className="py-2">{METRIC_LABEL[m] ?? m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {compare.runs.map(row => (
                  <tr key={row.run_id} className="border-t border-border">
                    <td className="py-2">{row.config_label}</td>
                    {compare.metrics.map(m => (
                      <td key={m} className="py-2">{fmt(row.values[m])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted-foreground">
              值全部来自服务端；缺指标的格子显示「—」。
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
