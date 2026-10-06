/**
 * 版本管理（§4.3 页面表）：按 `doc_group_id` 折叠展示，展开看历次版本，
 * **当前生效版本高亮**。
 *
 * ⚠️ 「当前生效」的口径与检索期折叠完全一致（§3.3.1）：组内 `active`
 *    且 `effective_date ≤ 今天` 中 version 最大的那一版 —— 由后端算好
 *    （`is_current`），前端不自己推。
 */
import { useCallback, useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { listDocuments, listVersions } from '@/api/admin'
import type { DocumentItem } from '@/api/admin'

export default function VersionsPage() {
  const [groups, setGroups] = useState<DocumentItem[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [versions, setVersions] = useState<DocumentItem[]>([])
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      // ⚠️ **必须翻页取全**（评审 I-2）：后端每页上限 100 行，而返回的是
      //    **文档行**（含各组的历史版本、disabled/failed 行）——
      //    只取第一页的话，排序落在 100 行之后的文档组会**整组静默消失**，
      //    页面上没有任何迹象。
      const byGroup = new Map<string, DocumentItem>()
      let page = 1
      for (;;) {
        const r = await listDocuments({ page, page_size: 100 })
        for (const d of r.items ?? []) {
          const cur = byGroup.get(d.doc_group_id)
          if (!cur || d.is_current || (!cur.is_current && d.version > cur.version)) {
            byGroup.set(d.doc_group_id, d)
          }
        }
        if (!r.has_more) break
        page += 1
        if (page > 50) break   // 兜底：5000 行还取不完就不再往上翻
      }
      setGroups([...byGroup.values()])
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  async function toggle(gid: string) {
    if (open === gid) { setOpen(null); return }
    setOpen(gid)
    try {
      setVersions((await listVersions(gid)).versions ?? [])
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">版本管理</h1>
      {error && <p className="text-sm text-destructive">{error}</p>}

      <Card>
        <CardContent className="divide-y divide-border pt-6">
          {groups.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">暂无文档</p>
          )}
          {groups.map((g) => (
            <div key={g.doc_group_id} className="py-3">
              <div className="flex items-center gap-3">
                <button className="flex-1 text-left" onClick={() => void toggle(g.doc_group_id)}>
                  <span className="font-medium">{g.title}</span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    {open === g.doc_group_id ? '▾' : '▸'} 版本 v{g.version}
                  </span>
                </button>
                <Badge variant={g.is_current ? 'default' : 'secondary'}>
                  {g.is_current ? `当前生效 v${g.version}` : `最新记录 v${g.version}`}
                </Badge>
              </div>

              {open === g.doc_group_id && (
                <table className="mt-2 w-full text-sm">
                  <thead className="text-xs text-muted-foreground">
                    <tr>
                      <th className="py-1 text-left">版本</th>
                      <th className="text-left">状态</th>
                      <th className="text-left">生效日</th>
                      <th className="text-left">分块</th>
                      <th className="text-left">上传</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((v) => (
                      <tr key={v.id}
                          className={v.is_current ? 'bg-primary/5 font-medium' : ''}>
                        <td className="py-1">
                          v{v.version}
                          {v.is_current && <span className="ml-2 text-xs text-primary">← 当前生效</span>}
                        </td>
                        <td>{v.status}</td>
                        <td>{v.effective_date ?? '—'}</td>
                        <td>{v.chunk_count}</td>
                        <td>{v.created_at?.slice(0, 10) ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        提示：停用当前生效版本会让**同组上一版恢复生效**（§3.3.1 的折叠规则）——
        操作入口在「文档管理」页，那里有显式提示。
      </p>
      <Button variant="outline" size="sm" onClick={() => void load()}>刷新</Button>
    </div>
  )
}
