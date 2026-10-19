/**
 * 仪表盘（§4.4）。
 *
 * **两块数据源，别混**：
 * - **业务指标读 PostgreSQL**（核心指标卡 / 问答量趋势 / 拒答分布 / 高频问题 / 降级次数）
 * - **运行指标读 Langfuse Metrics API v2**（节点耗时 / 端到端延迟 / 错误率 / token 用量
 *   / 成本 / 告警状态）
 *
 * ⚠️ **运行指标不可用时，业务指标区块照常渲染**（§4.4）——
 *    所以 `stats/retrieval` 不可用只影响它自己那一块，且它返回的是
 *    `available:false` + HTTP 200，不是错误。
 * ⚠️ 「降级次数」读的是 `stats/overview` 的 `degradation_counts`（PG），
 *    **不是** `stats/retrieval` —— 后者读 Langfuse，两处说法原本打架，
 *    已按 §4.4 统一。
 */
import { useEffect, useState } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import EChart from '@/components/EChart'
import {
  statsHotQuestions, statsOverview, statsRefusals, statsRetrieval, statsTrend,
} from '@/api/admin'
import type {
  HotQuestionsResponse, OverviewResponse, RefusalStatsResponse,
  RetrievalMetricsResponse, TrendResponse,
} from '@/api/admin'

const REASON_LABEL: Record<string, string> = {
  no_candidate: '候选为空',
  insufficient_evidence: '证据不足',
}

export default function AdminDashboard() {
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [trend, setTrend] = useState<TrendResponse | null>(null)
  const [refusals, setRefusals] = useState<RefusalStatsResponse | null>(null)
  const [hot, setHot] = useState<HotQuestionsResponse | null>(null)
  const [retrieval, setRetrieval] = useState<RetrievalMetricsResponse | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    // 业务指标：一个失败不影响其它块
    statsOverview().then(setOverview).catch((e) => setError((e as Error).message))
    statsTrend(30).then(setTrend).catch(() => setTrend(null))
    statsRefusals().then(setRefusals).catch(() => setRefusals(null))
    statsHotQuestions(10).then(setHot).catch(() => setHot(null))
    // 运行指标：独立一块，链路上恒成功（后端保证 200）
    statsRetrieval().then(setRetrieval).catch(() => setRetrieval(null))
  }, [])

  const degradation = overview?.degradation_counts ?? {}

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">仪表盘</h1>
      {error && <p className="text-sm text-destructive">{error}</p>}

      {/* ---- 业务指标（PostgreSQL） ---- */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat title="文档数" value={overview?.document_count} hint="当前生效版本" />
        <Stat title="片段数" value={overview?.chunk_count} hint="当前生效版本" />
        <Stat title="问答量" value={overview?.qa_count} hint="不含评测轮次" />
        <Stat title="拒答率"
              value={overview ? `${(overview.refusal_rate * 100).toFixed(1)}%` : undefined}
              hint="分母为知识型轮次" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle className="text-base">问答量趋势（近 30 天）</CardTitle></CardHeader>
          <CardContent>
            <EChart height={240} option={{
              tooltip: { trigger: 'axis' },
              // 图例放到底部，并给 x 轴刻度留出空间 —— 原来图例和日期挤在同一条线上
              legend: { data: ['问答量', '拒答数'], bottom: 0 },
              grid: { left: 40, right: 16, top: 40, bottom: 50 },
              xAxis: {
                type: 'category',
                data: (trend?.days ?? []).map((d) => d.date.slice(5)),
              },
              yAxis: { type: 'value', minInterval: 1 },
              series: [
                { name: '问答量', type: 'line', smooth: true,
                  data: (trend?.days ?? []).map((d) => d.qa_count) },
                { name: '拒答数', type: 'line', smooth: true,
                  data: (trend?.days ?? []).map((d) => d.refusal_count),
                  itemStyle: { color: '#f59e0b' } },
              ],
            }} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="text-base">拒答原因分布</CardTitle></CardHeader>
          <CardContent>
            <EChart height={240} option={{
              tooltip: { trigger: 'item' },
              series: [{
                type: 'pie', radius: ['40%', '70%'],
                data: (refusals?.by_reason ?? []).map((r) => ({
                  name: REASON_LABEL[r.reason] ?? r.reason, value: r.count,
                })),
              }],
            }} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="text-base">高频问题 Top 10</CardTitle></CardHeader>
          <CardContent>
            <EChart height={280} option={{
              tooltip: { trigger: 'axis' },
              grid: { left: 140, right: 24, top: 10, bottom: 30 },
              xAxis: { type: 'value', minInterval: 1 },
              yAxis: {
                type: 'category', inverse: true,
                data: (hot?.items ?? []).map((q) =>
                  q.question.length > 14 ? `${q.question.slice(0, 14)}…` : q.question),
              },
              series: [{ type: 'bar', data: (hot?.items ?? []).map((q) => q.count) }],
            }} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="text-base">降级次数</CardTitle></CardHeader>
          <CardContent>
            {Object.keys(degradation).length === 0
              ? <p className="py-16 text-center text-sm text-muted-foreground">没有降级记录</p>
              : <EChart height={280} option={{
                  tooltip: { trigger: 'axis' },
                  grid: { left: 60, right: 24, top: 10, bottom: 30 },
                  xAxis: { type: 'category', data: Object.keys(degradation) },
                  yAxis: { type: 'value', minInterval: 1 },
                  series: [{
                    type: 'bar',
                    data: Object.values(degradation),
                    itemStyle: { color: '#f97316' },
                  }],
                }} />}
          </CardContent>
        </Card>
      </div>

      {/* ---- 运行指标（Langfuse）——独立降级，不影响上面 ---- */}
      <Card>
        <CardHeader><CardTitle className="text-base">运行指标</CardTitle></CardHeader>
        <CardContent>
          {retrieval?.available ? (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Stat title="延迟 p50（ms）" value={retrieval.latency_p50 ?? '—'} />
              <Stat title="延迟 p95（ms）" value={retrieval.latency_p95 ?? '—'} />
              <Stat title="错误率" value={retrieval.error_rate ?? '—'} />
              <Stat title="token 用量" value={retrieval.token_usage ?? '—'}
                    hint={`输入 ${retrieval.token_input ?? '—'} / 输出 ${retrieval.token_output ?? '—'}`} />
              {/* —— §8.2 新增：Langfuse 白捡的三项 —— */}
              <Stat title="LLM 成本" value={retrieval.cost ?? '—'} hint="按模型自动折算" />
              <AlertCard alerts={retrieval.alerts} />
              <NodeLatencyCard rows={retrieval.node_latency ?? []} />
              <p className="col-span-2 text-xs text-muted-foreground lg:col-span-4">
                下钻到 Langfuse（<code>{'https://cloud.langfuse.com'}</code>）看单次请求
                的具体环节：搜这条回答响应头里的 <code>x-trace-id</code> 即可。
              </p>
            </div>
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">
              运行指标暂不可用
              {retrieval?.status ? `（${retrieval.status}）` : ''} ——
              业务指标不受影响。可观测性由 Langfuse 云提供。
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/** 告警状态卡（§4.4）：**有 firing 即标红**。
 *
 * ⚠️ 状态由**后端拿指标自算**（【方案 §8.3】方案 A：Langfuse 没有可读的告警
 *    API），本项目不接 Alertmanager（无人值班、没有接收方，见方案 3.2.3.3）。
 */
function AlertCard({ alerts }: {
  alerts?: { firing: number; pending: number; rules: { name: string; state: string }[] } | null
}) {
  if (!alerts) {
    return <Stat title="告警状态" value="—" hint="未读到规则" />
  }
  const red = alerts.firing > 0
  return (
    <Card className={red ? 'border-red-500 bg-red-50' : ''}>
      <CardContent className="pt-6">
        <p className="text-sm text-muted-foreground">告警状态</p>
        <p className={`mt-1 text-2xl font-semibold ${red ? 'text-red-600' : ''}`}>
          {red ? `⚠ ${alerts.firing} 条触发` : '正常'}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {red
            ? alerts.rules.filter(r => r.state === 'firing').map(r => r.name).join('、')
            : `共 ${alerts.rules.length} 条规则`}
        </p>
      </CardContent>
    </Card>
  )
}

function Stat({ title, value, hint }: {
  title: string
  value: number | string | undefined
  hint?: string
}) {
  return (
    <Card>
      <CardContent className="pt-6">
        <p className="text-sm text-muted-foreground">{title}</p>
        <p className="mt-1 text-2xl font-semibold">
          {value === undefined || value === null ? '—' : value}
        </p>
        {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
      </CardContent>
    </Card>
  )
}

/** 按节点的 p95 耗时（【方案 §8.2】新增 —— §4.4 要求「按节点分组」，改造前从未实现）。 */
function NodeLatencyCard({ rows }: { rows: { node: string; p95: number }[] }) {
  if (!rows.length) {
    return <Stat title="节点耗时 p95" value="—" hint="暂无数据" />
  }
  const worst = rows[0]
  return (
    <Card className="col-span-2 lg:col-span-4">
      <CardContent className="pt-6">
        <p className="text-sm text-muted-foreground">节点耗时 p95（ms）</p>
        <div className="mt-2 space-y-1">
          {rows.slice(0, 8).map(r => (
            <div key={r.node} className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{r.node}</span>
              <span className={r === worst ? 'font-semibold' : ''}>
                {Math.round(r.p95)}
              </span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
