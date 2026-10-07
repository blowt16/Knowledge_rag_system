/**
 * 消融对比（§6.4）—— 原评测页的「起一轮 / 历史轮次 / 对比矩阵」，加六个消融开关。
 *
 * **分两部分，别混为一谈**（§6.4）：
 *   ① 原样搬：角色 / 提权 / 跑全量 / 跑校准小集 / 历史轮次表 / 消融对比矩阵。
 *   ② **新增：六个消融开关**（决策 23）—— 本仓库原本**没有**这个界面。
 *
 * ⚠️ 为什么必须补这个 UI（§1.1④）：前端**从来就产不出非基线的 run** ——
 *    老的 `EvalPage.tsx` 里没有开关、`startEvalRun` 也不传 `config`，
 *    所有消融行都是 `run_ablation.py` 脚本打出来的。光把页面搬过去，
 *    它仍然只能产出纯向量基线。
 *
 * ⚠️ 这个页面的 run **仍然走 `suite` 路径**（不是 `set_id`），所以 config 里照旧带
 *    `suite` 键 —— 这正是它被判成消融运行的原因，**也是它该有的行为**（§5.4）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  type EvalCompareResponse,
  type EvalCompareRow,
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
  refused_count: '拒答题数',
  scored_with_ground_truth: '有参考答案题数',
  faithfulness: 'Faithfulness',
  answer_relevancy: 'Answer Relevancy',
  context_precision: 'Context Precision',
  context_recall: 'Context Recall',
}

/** 四项 ragas 的 key —— 与 `METRIC_LABEL` 里的保持一致（服务端定的名字）。 */
const RAGAS_KEYS = ['faithfulness', 'answer_relevancy', 'context_precision', 'context_recall']

/**
 * 六个消融开关。**键名必须与 `eval_config.SWITCH_KEYS` 一致**（§6.4）——
 * 拼错一个，服务端 `switches()` 就当它没开，跑出来的行与标签对不上。
 */
const SWITCHES: { key: string; label: string }[] = [
  { key: 'bm25', label: 'BM25' },
  { key: 'rrf', label: 'RRF' },
  { key: 'rerank', label: '精排' },
  { key: 'expand_verbatim', label: 'verbatim 查询' },
  { key: 'expand_keywords', label: 'keywords 查询' },
  { key: 'expand_hyde', label: 'hyde 查询' },
]

/** 这一轮 ragas 为什么是空的 —— 诊断是行字段，不占矩阵列（见后端 EvalCompareRow）。 */
function ragasIssue(row: EvalCompareRow): string | null {
  const errs = row.ragas_errors?.join('；')
  if (row.ragas_available === false) {
    return errs ? `ragas 没跑起来：${errs}` : 'ragas 没跑起来'
  }
  if (errs) return `部分指标没算出来：${errs}`
  // ⚠️ 还有第三种「空」：环境没问题、也没报错，四项却全是「—」——
  //    通常是没有可评分样本（缺参考答案/上下文）。不说明的话，
  //    看表的人只能看到四个「—」，照样得去查库。
  if (RAGAS_KEYS.every(k => row.values[k] === null || row.values[k] === undefined)) {
    return 'ragas 没产出分数（这一轮没有可评分的样本）'
  }
  return null
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

export default function EvalAblationPage() {
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [compare, setCompare] = useState<EvalCompareResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [role, setRole] = useState<'student' | 'staff' | 'admin'>('student')
  const [restricted, setRestricted] = useState(false)
  const [switches, setSwitches] = useState<Record<string, boolean>>({})
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    const data = await evalRuns({ page: 1, page_size: 30 })
    setRuns(data.items ?? [])
    return data.items ?? []
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
    }, 1500)
  }, [refresh])

  /** 勾了的开关组装成 config；**全不勾就是纯向量检索**（现在的「跑全量」行为）。 */
  const config = () =>
    Object.fromEntries(SWITCHES.filter(s => switches[s.key]).map(s => [s.key, true]))

  const start = async (suite: 'full' | 'refusal_calib') => {
    setBusy(true)
    setMsg('')
    try {
      const cfg = config()
      const r = await startEvalRun({
        name: suite === 'refusal_calib' ? `校准小集（${role}）` : `消融（${role}）`,
        suite,
        role,
        include_restricted: restricted,
        config: cfg,
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

  //: 勾选的轮次里 ragas 没算全的 —— 这些行的四项指标是「—」，不说明原因就成了哑谜
  const ragasIssues = (compare?.runs ?? []).flatMap(row => {
    const why = ragasIssue(row)
    return why ? [{ row, why }] : []
  })

  const anyOn = Object.values(switches).some(Boolean)

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader><CardTitle className="text-base">起一轮消融评测</CardTitle></CardHeader>
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
            <Button disabled={busy} variant="outline"
                    onClick={() => start('refusal_calib')}>跑校准小集</Button>
            <Button disabled={busy} onClick={() => start('full')}>跑全量</Button>
          </div>

          {/* 六个开关（决策 23）。全不勾 = 纯向量检索，就是原来的「跑全量」（§6.4） */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-border px-3 py-2 text-sm">
            <span className="text-muted-foreground">检索链路开关</span>
            {SWITCHES.map(s => (
              <label key={s.key} className="flex items-center gap-1">
                <input type="checkbox" checked={Boolean(switches[s.key])}
                       onChange={e => setSwitches(v => ({ ...v, [s.key]: e.target.checked }))} />
                {s.label}
              </label>
            ))}
            <span className="text-xs text-muted-foreground">
              {anyOn ? '这一轮是按开关跑的消融' : '全不勾 = 纯向量检索（基线）'}
            </span>
          </div>

          {msg && <p className="text-sm text-muted-foreground">{msg}</p>}
          <p className="text-xs text-muted-foreground">
            评测在后台跑，进度看下面列表的状态；同一时刻只允许一轮（要占 GPU）。
            这两条入口走的是 <code>suite</code> 路径 —— 与批量评测页不同，它们
            <b>不看</b>用例的「参与评测」开关，题集与 CI 用的一致。
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
                      <td className="py-2">{fmt(ragasValue(r.metrics, 'faithfulness'))}</td>
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
          {/* ⚠️ 指标全集有十几列，一屏放不下必须横向滚动：表头 `whitespace-nowrap`
              不然文字被挤成竖排；「配置」列 sticky 固定，滚到右边时不丢行标识。 */}
          <CardContent className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="sticky left-0 z-10 bg-card py-2 pr-3 whitespace-nowrap">配置</th>
                  {compare.metrics.map(m => (
                    <th key={m} className="py-2 pr-3 whitespace-nowrap">{METRIC_LABEL[m] ?? m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {compare.runs.map(row => (
                  <tr key={row.run_id} className="border-t border-border">
                    <td className="sticky left-0 z-10 bg-card py-2 pr-3 whitespace-nowrap">
                      {row.config_label}
                    </td>
                    {compare.metrics.map(m => (
                      <td key={m} className="py-2 pr-3 whitespace-nowrap">{fmt(row.values[m])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted-foreground">
              值全部来自服务端；缺指标的格子显示「—」。
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              ⚠️ ragas 有跑间随机性：同一样本两次跑实测能差到 <b>0.18</b>。
              比这还小的差值，不要当成结论。
            </p>
            {ragasIssues.length > 0 && (
              <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs">
                <p className="font-medium">这些轮次的 ragas 没算全，四项指标是空的：</p>
                <ul className="mt-1 space-y-0.5 text-muted-foreground">
                  {ragasIssues.map(({ row, why }) => (
                    <li key={row.run_id}>
                      {row.config_label}（{row.run_id.slice(0, 8)}）—— {why}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
