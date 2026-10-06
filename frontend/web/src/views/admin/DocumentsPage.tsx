/**
 * 文档管理（§4.3 管理端 / §4.3.1 / §4.3.2）。
 *
 * ⚠️ 三条**界面必须做到**的硬规则，都在这个文件里：
 * ① 上传进度**必须同时显示 `current_file`**，不能只画进度条 ——
 *    单文件上传时 `total = 1`，百分比会长时间停在 0% 或 100%（§3.7.2 侧的上传规格）。
 * ② 停用**当前生效版本**时必须显式提示「停用后上一版将恢复生效」（§3.3.1）——
 *    折叠规则取组内 active 的最大 version，停用 v5 之后 v4 会「复活」，
 *    而管理员多半只是想「这条制度先关掉」。
 * ③ 改可见范围 / 生效日期**必须显式告知耗时并给进度**，不做静默即时保存（§4.3.1.2）——
 *    它们冗余在 Chroma 里，保存会重写该文档全部 chunk 的 metadata。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { getAccessToken } from '@/api/client'
import { streamSse } from '@/api/sse'
import {
  listChunks, listDocuments, patchDocument, disableDocument, enableDocument,
  deleteDocument, listRoles,
} from '@/api/admin'
import type { ChunkItem, DocumentItem, RoleItem } from '@/api/admin'

const STATUS_LABEL: Record<string, string> = {
  active: '生效中', disabled: '已停用', indexing: '索引中', failed: '失败',
}

interface UploadProgress {
  done: number
  total: number
  current_file: string
  counts: Record<string, number>
}

export default function DocumentsPage() {
  const [items, setItems] = useState<DocumentItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  // 状态/可见范围的取值域与后端 Query 的 pattern 一致 —— 类型写窄一点，别让错值溜到接口
  const [status, setStatus] = useState<'all' | 'indexing' | 'active' | 'disabled' | 'failed'>('all')
  const [visibility, setVisibility] = useState<'all' | 'public' | 'restricted'>('all')
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const [roles, setRoles] = useState<RoleItem[]>([])
  const [busy, setBusy] = useState('')

  const [editing, setEditing] = useState<DocumentItem | null>(null)
  const [pendingDisable, setPendingDisable] = useState<DocumentItem | null>(null)
  const [pendingDelete, setPendingDelete] = useState<DocumentItem | null>(null)
  const [chunksOf, setChunksOf] = useState<DocumentItem | null>(null)
  const [upload, setUpload] = useState<UploadProgress | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const pageSize = 20

  const load = useCallback(async () => {
    try {
      const r = await listDocuments({ status, visibility, q, page, page_size: pageSize })
      setItems(r.items ?? [])
      setTotal(r.total)
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [status, visibility, q, page])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    // 可见范围选择器的角色清单来自服务端 —— 不许硬编码（§4.3.1.2）
    listRoles().then(setRoles).catch(() => setRoles([]))
  }, [])

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    setError('')
    try {
      await fn()
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  /** 上传：multipart 交出去，然后**跟 SSE 进度流**（§3.7.2 侧的上传规格）。 */
  async function uploadFile(file: File) {
    setError('')
    setUpload({ done: 0, total: 1, current_file: file.name, counts: {} })
    try {
      const form = new FormData()
      form.append('file', file)
      const token = getAccessToken()
      const resp = await fetch('/api/admin/documents/upload', {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
      })
      if (!resp.ok) throw new Error(`上传失败（HTTP ${resp.status}）`)
      const { tasks } = await resp.json() as { tasks: { task_id: string }[] }
      const taskId = tasks[0]?.task_id

      await new Promise<void>((resolve) => {
        // ⚠️ 用共用的 `streamSse`（fetch + ReadableStream），**不用 `EventSource`**：
        //    原生 EventSource 带不上 Authorization 头，只能把 token 塞进查询串 ——
        //    那会让它进访问日志与浏览器历史，与「身份只能来自 JWT」冲突（§3.2.2）。
        streamSse({
          url: `/api/admin/documents/upload/${taskId}/stream`,
          token,
          handlers: {
            onEvent: (event, data) => {
              if (event === 'progress') setUpload(data as UploadProgress)
              if (event === 'done' || event === 'error') resolve()
            },
            onClose: () => resolve(),
            onError: (e) => { setError(e.message); resolve() },
          },
        })
      })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUpload(null)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const pages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">文档管理</h1>
        <div className="ml-auto flex items-center gap-2">
          <input ref={fileInput} type="file" className="hidden"
                 onChange={(e) => {
                   const f = e.target.files?.[0]
                   if (f) void uploadFile(f)
                 }} />
          <Button onClick={() => fileInput.current?.click()} disabled={!!upload}>
            + 上传文档
          </Button>
        </div>
      </div>

      {upload && (
        <Card>
          <CardContent className="space-y-2 pt-6">
            {/* ⚠️ current_file 必须显示 —— 单文件时 total=1，进度条没有信息量 */}
            <p className="text-sm">
              正在处理：<span className="font-medium">{upload.current_file}</span>
            </p>
            <div className="h-2 w-full overflow-hidden rounded bg-muted">
              <div className="h-full bg-primary transition-[width]"
                   style={{ width: `${Math.round((upload.done / Math.max(1, upload.total)) * 100)}%` }} />
            </div>
            <p className="text-xs text-muted-foreground">
              {upload.done}/{upload.total} · 阶段：
              {Object.entries(upload.counts).find(([, v]) => v > 0)?.[0] ?? '等待中'}
            </p>
          </CardContent>
        </Card>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label>状态</Label>
          <Select value={status} onValueChange={(v) => { setPage(1); setStatus((v ?? 'all') as typeof status) }}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="active">生效中</SelectItem>
              <SelectItem value="disabled">已停用</SelectItem>
              <SelectItem value="failed">失败</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label>可见范围</Label>
          <Select value={visibility} onValueChange={(v) => { setPage(1); setVisibility((v ?? 'all') as typeof visibility) }}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="public">公开</SelectItem>
              <SelectItem value="restricted">受限</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label>关键词</Label>
          <Input className="w-56" placeholder="标题或文件名" value={q}
                 onChange={(e) => { setPage(1); setQ(e.target.value) }} />
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标题</TableHead>
                <TableHead className="w-24">状态</TableHead>
                <TableHead className="w-28">可见</TableHead>
                <TableHead className="w-16">版本</TableHead>
                <TableHead className="w-32">生效日</TableHead>
                <TableHead className="w-64">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((d) => (
                <TableRow key={d.id}>
                  <TableCell>
                    <div className="font-medium">{d.title}</div>
                    <div className="text-xs text-muted-foreground">
                      {d.filename} · {d.chunk_count} 块
                      {d.is_current && <span className="ml-2 text-primary">当前生效</span>}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={d.status === 'active' ? 'default' : 'secondary'}>
                      {STATUS_LABEL[d.status] ?? d.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">
                    {d.visibility === 'public'
                      ? '公开'
                      : `受限：${(d.visible_roles ?? []).join('、') || '（无）'}`}
                  </TableCell>
                  <TableCell>v{d.version}</TableCell>
                  <TableCell className="text-sm">{d.effective_date ?? '—'}</TableCell>
                  <TableCell className="space-x-1">
                    <Button variant="ghost" size="sm"
                            onClick={() => setChunksOf(d)}>分块</Button>
                    <Button variant="ghost" size="sm"
                            onClick={() => setEditing(d)}>编辑</Button>
                    {d.status === 'active'
                      ? <Button variant="ghost" size="sm" disabled={busy === d.id}
                                onClick={() => setPendingDisable(d)}>停用</Button>
                      : <Button variant="ghost" size="sm" disabled={busy === d.id}
                                onClick={() => void run(d.id, () => enableDocument(d.id))}>
                          启用
                        </Button>}
                    <Button variant="ghost" size="sm"
                            className="text-destructive"
                            onClick={() => setPendingDelete(d)}>删除</Button>
                  </TableCell>
                </TableRow>
              ))}
              {items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-sm text-muted-foreground">
                    没有符合条件的文档
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>

          <div className="mt-4 flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">共 {total} 条</span>
            <Button variant="outline" size="sm" disabled={page <= 1}
                    onClick={() => setPage((p) => p - 1)}>上一页</Button>
            <span>{page} / {pages}</span>
            <Button variant="outline" size="sm" disabled={page >= pages}
                    onClick={() => setPage((p) => p + 1)}>下一页</Button>
          </div>
        </CardContent>
      </Card>

      {/* ② 停用当前生效版本 —— 必须提示「上一版会复活」（§3.3.1） */}
      <AlertDialog open={!!pendingDisable}
                   onOpenChange={(o) => !o && setPendingDisable(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>停用「{pendingDisable?.title}」？</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              {pendingDisable?.is_current ? (
                <span className="block font-medium text-destructive">
                  ⚠️ 这是当前生效版本。停用后，**同组的上一版将恢复生效** ——
                  检索会返回旧版条款，而不是「这条制度消失」。
                </span>
              ) : (
                <span className="block">该版本停用后不参与检索。</span>
              )}
              <span className="block text-xs">
                停用会重写该文档全部 chunk 的元数据，需要一点时间。
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const d = pendingDisable!
                setPendingDisable(null)
                void run(d.id, () => disableDocument(d.id))
              }}>
              确认停用
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={!!pendingDelete} onOpenChange={(o) => !o && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除「{pendingDelete?.title}」？</AlertDialogTitle>
            <AlertDialogDescription>
              会同时删除向量库与全文索引里的全部片段，以及数据库记录。
              **源文件会保留**。同组的上一版将恢复生效。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const d = pendingDelete!
                setPendingDelete(null)
                void run(d.id, () => deleteDocument(d.id))
              }}>
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {editing && (
        <EditDialog doc={editing} roles={roles} busy={busy === editing.id}
                    onClose={() => setEditing(null)}
                    onSaved={async () => { setEditing(null); await load() }}
                    onError={setError} />
      )}

      {chunksOf && <ChunksDialog doc={chunksOf} onClose={() => setChunksOf(null)} />}
    </div>
  )
}

/** ③ 改可见范围 / 生效日：显式告知耗时 + 保存期间给进度，不静默即时保存。 */
function EditDialog({
  doc, roles, busy, onClose, onSaved, onError,
}: {
  doc: DocumentItem
  roles: RoleItem[]
  busy: boolean
  onClose: () => void
  onSaved: () => void
  onError: (m: string) => void
}) {
  const [title, setTitle] = useState(doc.title)
  const [visibility, setVisibility] = useState(doc.visibility)
  const [picked, setPicked] = useState<string[]>(doc.visible_roles ?? [])
  const [effective, setEffective] = useState(doc.effective_date ?? '')
  const [saving, setSaving] = useState(false)
  const [elapsed, setElapsed] = useState(0)

  // 会触发重索引的字段 —— 与后端 `REINDEX_FIELDS` 同一份口径
  const needsReindex =
    visibility !== doc.visibility
    || effective !== (doc.effective_date ?? '')
    || [...picked].sort().join(',') !== [...(doc.visible_roles ?? [])].sort().join(',')

  useEffect(() => {
    if (!saving) return
    const t = setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => clearInterval(t)
  }, [saving])

  async function save() {
    setSaving(true)
    setElapsed(0)
    try {
      const body: Record<string, unknown> = { title }
      if (visibility !== doc.visibility) {
        body.visibility = visibility
        body.visible_roles = visibility === 'restricted' ? picked : []
      } else if (visibility === 'restricted'
                 && [...picked].sort().join(',') !== [...(doc.visible_roles ?? [])].sort().join(',')) {
        body.visible_roles = picked
      }
      if (effective !== (doc.effective_date ?? '')) body.effective_date = effective
      await patchDocument(doc.id, body)
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
        <DialogHeader>
          <DialogTitle>编辑「{doc.title}」</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1">
            <Label>标题</Label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
            <p className="text-xs text-muted-foreground">改标题**不会**触发重新索引。</p>
          </div>

          <div className="space-y-1">
            <Label>可见范围</Label>
            <Select value={visibility} onValueChange={(v) => setVisibility((v ?? 'public') as typeof visibility)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="public">公开</SelectItem>
                <SelectItem value="restricted">受限</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {visibility === 'restricted' && (
            <div className="space-y-1">
              <Label>可见角色（至少一个）</Label>
              <div className="flex flex-wrap gap-2">
                {roles.map((r) => (
                  <label key={r.value}
                         className="flex items-center gap-1 rounded border border-border px-2 py-1 text-sm">
                    <input type="checkbox" checked={picked.includes(r.value)}
                           onChange={(e) => setPicked((prev) => e.target.checked
                             ? [...prev, r.value]
                             : prev.filter((x) => x !== r.value))} />
                    {r.label}
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="space-y-1">
            <Label>生效日期</Label>
            <Input type="date" value={effective}
                   onChange={(e) => setEffective(e.target.value)} />
            <p className="text-xs text-muted-foreground">
              未来日期表示「暂不参与检索」，旧版继续生效。
            </p>
          </div>

          {needsReindex && (
            <p className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
              ⚠️ 可见范围或生效日期冗余在向量库里，保存会**重写该文档全部片段的元数据**，
              通常需要几秒到几十秒；期间该文档的检索结果可能不一致。
            </p>
          )}
          {saving && needsReindex && (
            <p className="text-xs text-muted-foreground">正在重新索引… 已用 {elapsed}s</p>
          )}

          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={() => void save()} disabled={saving || busy}>
              {saving ? `保存中… ${elapsed}s` : '保存'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

/** 分块预览（§4.3.2）—— 只读；这是唯一能看见 char_start / 章节 / vis_* 的地方。 */
function ChunksDialog({ doc, onClose }: { doc: DocumentItem; onClose: () => void }) {
  const [chunks, setChunks] = useState<ChunkItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [err, setErr] = useState('')
  const pageSize = 10

  useEffect(() => {
    listChunks(doc.id, { page, page_size: pageSize })
      .then((r) => { setChunks(r.chunks ?? []); setTotal(r.total) })
      .catch((e) => setErr((e as Error).message))
  }, [doc.id, page])

  const pages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[80vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>分块预览 · {doc.title}</DialogTitle>
        </DialogHeader>
        {err && <p className="text-sm text-destructive">{err}</p>}
        <div className="space-y-3">
          {chunks.map((c) => (
            <div key={c.chunk_index} className="rounded border border-border p-3">
              <div className="mb-1 text-xs text-muted-foreground">
                #{c.chunk_index} · 第 {c.page} 页 · 字符 {c.char_start}–{c.char_end}
                {c.current_chapter ? ` · ${c.current_chapter}` : ' · （无章节）'}
              </div>
              <p className="whitespace-pre-wrap text-sm">{c.text}</p>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-muted-foreground">共 {total} 块</span>
          <Button variant="outline" size="sm" disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}>上一页</Button>
          <span>{page} / {pages}</span>
          <Button variant="outline" size="sm" disabled={page >= pages}
                  onClick={() => setPage((p) => p + 1)}>下一页</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
