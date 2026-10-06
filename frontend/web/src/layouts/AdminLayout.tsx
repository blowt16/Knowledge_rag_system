/**
 * 管理端布局（§4.3 的侧栏结构）。
 *
 * 「评测」页由 M5 补上（M4 时还没有数据源，§7 里程碑表）。
 *
 * ⚠️ 这里用 Tailwind + shadcn —— 与登录/聊天页的手写 CSS 并存（M4-D1）。
 *    Tailwind 的 preflight 是全局重置，接入后已用 bsk 回归过那两页的版式。
 */
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/stores/auth'

const NAV = [
  { to: '/admin', label: '仪表盘', end: true },
  { to: '/admin/documents', label: '文档管理', end: false },
  { to: '/admin/versions', label: '版本管理', end: false },
  { to: '/admin/refusals', label: '拒答分析', end: false },
  { to: '/admin/users', label: '用户管理', end: false },
  { to: '/admin/eval', label: '评测', end: false },
]

export default function AdminLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-card">
        <div className="px-4 py-5">
          <Link to="/admin" className="text-base font-semibold">
            管理后台
          </Link>
          <p className="mt-1 text-xs text-muted-foreground">校园 RAG 系统</p>
        </div>
        <nav className="flex flex-1 flex-col gap-1 px-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded-md px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-border p-3">
          <p className="mb-2 truncate text-xs text-muted-foreground">
            {user?.username}（{user?.role}）
          </p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" className="flex-1"
                    onClick={() => navigate('/chat')}>
              去问答
            </Button>
            {/* ⚠️ 回登录页**必须先登出**：登录状态下直接访问 /login 会被登录路由
                再送回 /admin（它看到你是管理员），点上去像没反应。
                登出后落在 /login?next=/admin —— 再登录还回得来。 */}
            <Button variant="ghost" size="sm" className="flex-1"
                    onClick={() => void logout()}>
              退出登录
            </Button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 flex-1 p-6">
        <Outlet />
      </main>
    </div>
  )
}
