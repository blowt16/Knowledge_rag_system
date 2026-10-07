/**
 * 用户管理（§4.3 页面表 / §3.7.3）。
 *
 * ⚠️ 三处写操作（建号 / 改角色 / 重置口令）后端都令 `token_version += 1` ——
 *    被改的人**旧令牌立刻失效**。界面上要讲清楚，否则管理员会以为
 *    「改完他还能用」。
 */
import { useCallback, useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
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
import {
  createUser, listRoles, listUsers, patchUser, resetPassword,
} from '@/api/admin'
import type { RoleItem, UserItem } from '@/api/admin'

export default function UsersPage() {
  const [items, setItems] = useState<UserItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [role, setRole] = useState('all')
  const [roles, setRoles] = useState<RoleItem[]>([])
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [resetting, setResetting] = useState<UserItem | null>(null)
  const [pendingDisable, setPendingDisable] = useState<UserItem | null>(null)
  const pageSize = 20

  const load = useCallback(async () => {
    try {
      const r = await listUsers({
        page, page_size: pageSize, ...(role === 'all' ? {} : { role }),
      })
      setItems(r.items ?? [])
      setTotal(r.total)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [page, role])

  useEffect(() => { void load() }, [load])
  useEffect(() => { listRoles().then(setRoles).catch(() => setRoles([])) }, [])

  async function run(fn: () => Promise<unknown>) {
    setError('')
    try {
      await fn()
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const pages = Math.max(1, Math.ceil(total / pageSize))
  // ⚠️ 下拉**触发器**默认显示原始枚举值（实测：`all` / `student` / `admin`）——
  //    只有展开后的选项才带中文。角色中文来自服务端的角色清单，不硬编码。
  const roleLabel = (v: string) =>
    v === 'all' ? '全部' : (roles.find((r) => r.value === v)?.label ?? v)

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">用户管理</h1>
        <Button className="ml-auto" onClick={() => setCreating(true)}>+ 建号</Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex items-center gap-3">
        <Label>角色</Label>
        <Select value={role} onValueChange={(v) => { setPage(1); setRole(v ?? 'all') }}>
          <SelectTrigger className="w-36">
            <SelectValue>{(v) => roleLabel(v as string)}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            {roles.map((r) => (
              <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-sm text-muted-foreground">共 {total} 人</span>
      </div>

      <Card>
        <CardContent className="pt-6">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>用户名</TableHead>
                <TableHead className="w-32">角色</TableHead>
                <TableHead className="w-24">状态</TableHead>
                <TableHead className="w-40">创建时间</TableHead>
                <TableHead className="w-72">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((u) => (
                <TableRow key={u.id}>
                  <TableCell className="font-medium">{u.username}</TableCell>
                  <TableCell>
                    <Select value={u.role}
                            onValueChange={(v) => void run(() => patchUser(u.id, { role: v ?? u.role }))}>
                      <SelectTrigger className="h-8 w-28">
                        <SelectValue>{(v) => roleLabel(v as string)}</SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {roles.map((r) => (
                          <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell>
                    <Badge variant={u.is_active ? 'default' : 'secondary'}>
                      {u.is_active ? '正常' : '已停用'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {u.created_at?.replace('T', ' ').slice(0, 16) ?? '—'}
                  </TableCell>
                  <TableCell className="space-x-1">
                    <Button variant="ghost" size="sm"
                            onClick={() => setResetting(u)}>重置口令</Button>
                    {u.is_active
                      ? <Button variant="ghost" size="sm" className="text-destructive"
                                onClick={() => setPendingDisable(u)}>停用</Button>
                      : <Button variant="ghost" size="sm"
                                onClick={() => void run(() => patchUser(u.id, { is_active: true }))}>
                          启用
                        </Button>}
                  </TableCell>
                </TableRow>
              ))}
              {items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-sm text-muted-foreground">
                    没有用户
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

      <AlertDialog open={!!pendingDisable}
                   onOpenChange={(o) => !o && setPendingDisable(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>停用「{pendingDisable?.username}」？</AlertDialogTitle>
            <AlertDialogDescription>
              停用后该账号**无法再登录**，且已签发的令牌立即失效。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => {
              const u = pendingDisable!
              setPendingDisable(null)
              void run(() => patchUser(u.id, { is_active: false }))
            }}>确认停用</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {creating && (
        <CreateDialog roles={roles} onClose={() => setCreating(false)}
                      onSaved={async () => { setCreating(false); await load() }}
                      onError={setError} />
      )}
      {resetting && (
        <ResetDialog user={resetting} onClose={() => setResetting(null)}
                     onSaved={async () => { setResetting(null); await load() }}
                     onError={setError} />
      )}
    </div>
  )
}

function CreateDialog({
  roles, onClose, onSaved, onError,
}: {
  roles: RoleItem[]
  onClose: () => void
  onSaved: () => void
  onError: (m: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('student')
  const [saving, setSaving] = useState(false)

  async function save() {
    setSaving(true)
    try {
      await createUser({ username, password, role })
      onSaved()
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !saving && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>新建账号</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>用户名</Label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label>初始口令（至少 8 位）</Label>
            <Input type="password" value={password}
                   onChange={(e) => setPassword(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label>角色</Label>
            <Select value={role} onValueChange={(v) => setRole(v ?? 'student')}>
              <SelectTrigger>
                <SelectValue>
                  {(v) => roles.find((r) => r.value === v)?.label ?? String(v)}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {roles.map((r) => (
                  <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={() => void save()}
                    disabled={saving || username.length < 3 || password.length < 8}>
              {saving ? '创建中…' : '创建'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function ResetDialog({
  user, onClose, onSaved, onError,
}: {
  user: UserItem
  onClose: () => void
  onSaved: () => void
  onError: (m: string) => void
}) {
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)

  async function save() {
    setSaving(true)
    try {
      await resetPassword(user.id, password)
      onSaved()
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !saving && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>重置「{user.username}」的口令</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>新口令（至少 8 位）</Label>
            <Input type="password" value={password}
                   onChange={(e) => setPassword(e.target.value)} />
          </div>
          <p className="text-xs text-muted-foreground">
            重置后该用户**所有已签发的令牌立即失效**，需要用新口令重新登录。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button onClick={() => void save()} disabled={saving || password.length < 8}>
              {saving ? '重置中…' : '重置'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
