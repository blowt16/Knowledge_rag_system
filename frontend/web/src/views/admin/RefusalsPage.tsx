/**
 * 拒答分析（§4.3 页面表 / §4.3.1.3）。
 *
 * 这张清单就是「学生问了但知识库答不上来」的清单 —— 反哺知识库补文档的入口。
 * 「时间分布」用 `stats/trend` 的 `refusal_count` 画（聚合），
 * 明细与标注走 `GET/POST /api/admin/refusals*`。
 *
 * ⚠️ 标注两个字段**至少填一个**，否则后端 400（§4.3.1.3）——
 *    前端也照此拦一道，别让用户白跑一趟。
 */
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import EChart from '@/components/EChart'
import { annotateRefusal, listRefusals, statsTrend } from '@/api/admin'
import type { RefusalItem, TrendResponse } from '@/api/admin'

const REASON_LABEL: Record<string, string> = {
  no_candidate: '候选为空',
  insufficient_evidence: '证据不足',
}

export default function RefusalsPage() {
  const [items, setItems] = useState<RefusalItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [trend, setTrend] = useState<TrendResponse | null>(null)
  const [editing, setEditing] = useState<RefusalItem | null>(null)
  const [error, setError] = useState('')
  const pageSize = 20

  const load = useCallback(async () => {
    try {
      const r = await listRefusals({ page, page_size: pageSize })
      setItems(r.items ?? [])
      setTotal(r.total)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [page])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    // 时间分布：只取拒答那一列（聚合口径，不是明细）
    statsTrend(30).then(setTrend).catch(() => setTrend(null))
  }, [])

  const pages = Math.max(1, Math.ceil(total / pageSize))

  const chartOption = {
    tooltip: { trigger: 'axis' as const },
    grid: { left: 40, right: 16, top: 20, bottom: 30 },
    xAxis: { type: 'category' as const, data: (trend?.days ?? []).map((d) => d.date.slice(5)) },
    yAxis: { type: 'value' as const, minInterval: 1 },
    series: [{
      name: '拒答数', type: 'bar' as const,
      data: (trend?.days ?? []).map((d) => d.refusal_count),
      itemStyle: { color: '#f59e0b' },
    }],
  }

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">拒答分析</h1>
      {error && <p className="text-sm text-destructive">{error}</p>}

      <Card>
        <CardHeader><CardTitle className="text-base">近 30 天拒答分布</CardTitle></CardHeader>
        <CardContent>
          {trend ? <EChart option={chartOption} height={200} />
                 : <p className="text-sm text-muted-foreground">加载中…</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">拒答明细（共 {total} 条）</CardTitle></CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>问题</TableHead>
                <TableHead className="w-32">原因</TableHead>
                <TableHead className="w-40">时间</TableHead>
                <TableHead className="w-64">标注</TableHead>
                <TableHead className="w-20">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="max-w-md">{r.question}</TableCell>
                  <TableCell className="text-sm">
                    {REASON_LABEL[r.refusal_reason ?? ''] ?? r.refusal_reason ?? '—'}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {r.created_at?.replace('T', ' ').slice(0, 16) ?? '—'}
                  </TableCell>
                  <TableCell className="text-sm">
                    {r.annotation
                      ? <span>{r.annotation.note || '（仅指定了建议文档）'}</span>
                      : <span className="text-muted-foreground">未标注</span>}
                  </TableCell>
                  <TableCell>
                    <Button variant="ghost" size="sm"
                            onClick={() => setEditing(r)}>
                      {r.annotation ? '改标注' : '标注'}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-sm text-muted-foreground">
                    没有拒答记录
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>

          <div className="mt-4 flex items-center gap-3 text-sm">
            <Button variant="outline" size="sm" disabled={page <= 1}
                    onClick={() => setPage((p) => p - 1)}>上一页</Button>
            <span>{page} / {pages}</span>
            <Button variant="outline" size="sm" disabled={page >= pages}
                    onClick={() => setPage((p) => p + 1)}>下一页</Button>
          </div>
        </CardContent>
      </Card>

      {editing && (
        <AnnotateDialog item={editing} onClose={() => setEditing(null)}
                        onSaved={async () => { setEditing(null); await load() }}
                        onError={setError} />
      )}
    </div>
  )
}

function AnnotateDialog({
  item, onClose, onSaved, onError,
}: {
  item: RefusalItem
  onClose: () => void
  onSaved: () => void
  onError: (m: string) => void
}) {
  const [note, setNote] = useState(item.annotation?.note ?? '')
  const [docId, setDocId] = useState(item.annotation?.suggested_document_id ?? '')
  const [saving, setSaving] = useState(false)

  async function save() {
    if (!note.trim() && !docId.trim()) {
      onError('建议补充的文档与备注至少要填一个')
      return
    }
    setSaving(true)
    try {
      await annotateRefusal(item.id, {
        ...(note.trim() ? { note: note.trim() } : {}),
        ...(docId.trim() ? { suggested_document_id: docId.trim() } : {}),
      })
      onSaved()
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !saving && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>标注拒答问题</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <p className="rounded bg-muted/50 p-2 text-sm">{item.question}</p>
          <div className="space-y-1">
            <Label>建议补充的文档 ID（可空）</Label>
            <Input value={docId} onChange={(e) => setDocId(e.target.value)}
                   placeholder="documents.id" />
          </div>
          <div className="space-y-1">
            <Label>备注（可空）</Label>
            <Textarea value={note} onChange={(e) => setNote(e.target.value)}
                      placeholder="例如：需要补一份《实验室安全管理办法》" />
          </div>
          <p className="text-xs text-muted-foreground">两个字段至少填一个；重复标注只保留最新一次。</p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={() => void save()} disabled={saving}>
              {saving ? '保存中…' : '保存'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
