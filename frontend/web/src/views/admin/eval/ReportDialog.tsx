/**
 * 评测报告弹窗（§6.6）—— 版式照参考图逐字核过。
 *
 * ⚠️ **算式与达标判断一律来自服务端**（§7.1）：这里只负责显示。
 *    前端拼公式就会出现「同一份数据算出两个分」。
 *
 * ⚠️ **缺项显示「—」，不显示 0**（决策 13）：拿不齐的数凑平均值，
 *    比不给数更误导 —— 它看起来像个结论。服务端把缺项给成 `null`，
 *    这里只需要不把 `null` 当成 0 渲染。
 */
import { Button } from '@/components/ui/button'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import type { EvalReport, EvalReportCase, EvalRunDetail } from '@/api/eval'

/** 卡片的中文名与**逐字**照抄参考图的提示语（§6.6 的表）。 */
const CARD: Record<string, { label: string; en: string; badge: string; hint: string }> = {
  context_recall: {
    label: '上下文召回', en: 'Context Recall', badge: '找资料',
    hint: '标准答案里说的东西，检索找回来了吗',
  },
  context_precision: {
    label: '上下文精度', en: 'Context Precision', badge: '找资料',
    hint: '找回来的资料里，有用的排在前面吗',
  },
  faithfulness: {
    label: '忠实度', en: 'Faithfulness', badge: '写答案',
    hint: '答案里每句话，在资料里有出处吗（有没有编）',
  },
  answer_relevancy: {
    label: '答案相关性', en: 'Answer Relevancy', badge: '写答案',
    hint: '答案是在回答这个问题吗（有没有跑题）',
  },
}

const COLUMNS: { key: keyof EvalReportCase; label: string }[] = [
  { key: 'context_recall', label: '召回' },
  { key: 'context_precision', label: '精度' },
  { key: 'faithfulness', label: '忠实度' },
  { key: 'answer_relevancy', label: '相关性' },
  { key: 'score', label: '均分' },
]

const fmt = (v: unknown) => (v === null || v === undefined ? '—' : String(v))

function statusText(status: string): string {
  return { pending: '排队中', running: '评测中', done: '已完成', failed: '失败' }[status]
    ?? status
}

export function ReportDialog({ detail, onClose }: {
  detail: EvalRunDetail
  onClose: () => void
}) {
  const report: EvalReport | undefined = detail.report ?? undefined
  const cards = report?.metrics ?? []
  const composite = report?.composite_score ?? null

  return (
    <Dialog open onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent className="max-h-[88vh] w-[95vw] max-w-6xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>评测报告</DialogTitle>
        </DialogHeader>

        {/* 头一行：评测集 / 策略 / 用例数 / 综合得分（§6.6） */}
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
          <span>评测集：{detail.set_name ?? '—'}</span>
          <span className="text-muted-foreground">策略：{detail.config_label}</span>
          <span className="text-muted-foreground">用例 {detail.total_cases ?? 0} 条</span>
          {detail.status !== 'done' && (
            <span className="text-amber-700 dark:text-amber-500">
              状态：{statusText(detail.status)}
            </span>
          )}
          <span>
            综合得分{' '}
            <b className="text-base">{composite === null ? '—' : composite.toFixed(4)}</b>
          </span>
        </div>

        {/* 失败轮次：这一页没有分数可看，把原因写在最显眼处 */}
        {detail.error && (
          <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm">
            这一轮失败了：{detail.error}
          </p>
        )}

        {/* 四张卡：浅灰底、圆角；中文名粗体 + 英文名灰色小字压在下面 */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {cards.map(c => {
            const meta = CARD[c.key] ?? { label: c.key, en: c.key, badge: '', hint: '' }
            const missing = c.score === null || c.score === undefined
            return (
              <div key={c.key} className="rounded-lg bg-muted/60 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-semibold">{meta.label}</p>
                    <p className="text-xs text-muted-foreground">{meta.en}</p>
                  </div>
                  {meta.badge && (
                    <span className="rounded bg-background px-1.5 py-0.5 text-xs text-muted-foreground">
                      {meta.badge}
                    </span>
                  )}
                </div>
                {/* 分数大号：达标绿色、未达标红色；缺项是灰色的「—」 */}
                <p className={`mt-3 text-2xl font-semibold ${
                  missing ? 'text-muted-foreground'
                    : c.passed ? 'text-success' : 'text-destructive'
                }`}>
                  {missing ? '—' : c.score!.toFixed(4)}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">{meta.hint}</p>
                <p className={`mt-2 text-xs ${
                  missing ? 'text-muted-foreground'
                    : c.passed ? 'text-success' : 'text-destructive'
                }`}>
                  {missing
                    ? '没有可评分的样本'
                    : `${c.passed ? '达标' : '未达标'}（≥ ${c.threshold}）`}
                </p>
              </div>
            )
          })}
        </div>

        {/* 结论条：浅绿底（服务端给的句子，前端不拼） */}
        {report?.verdict && (
          <p className={`rounded-md px-3 py-2 text-sm ${
            composite === null
              ? 'bg-muted text-muted-foreground'
              : 'bg-success/10 text-success'
          }`}>
            {report.verdict}
          </p>
        )}

        {/* 逐题明细 */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-2 pr-3">问题</th>
                <th className="py-2 pr-3">标准答案</th>
                <th className="py-2 pr-3">生成的答案</th>
                {COLUMNS.map(c => <th key={String(c.key)} className="py-2 pr-3">{c.label}</th>)}
                <th className="py-2">失败原因</th>
              </tr>
            </thead>
            <tbody>
              {(detail.cases ?? []).map((row, i) => (
                <tr key={row.case_id ?? `row-${i}`} className="border-t border-border align-top">
                  <td className="max-w-sm py-2 pr-3">{row.question ?? '—'}</td>
                  <td className="max-w-xs py-2 pr-3 text-muted-foreground">
                    {row.ground_truth || '—'}
                  </td>
                  <td className="max-w-md py-2 pr-3 whitespace-pre-wrap">{row.answer || '—'}</td>
                  {COLUMNS.map(c => (
                    <td key={String(c.key)} className="py-2 pr-3">{fmt(row[c.key])}</td>
                  ))}
                  {/* 这一列大多数时候是空的，**这是对的**：只有真跑挂了才填，
                      拒答不算失败（决策 20）——「系统正确地说了不知道」是正常结果 */}
                  <td className="max-w-xs py-2 text-destructive">{row.error || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(detail.cases ?? []).length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">这一轮没有逐题结果</p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
