import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './views/login/LoginPage'
import ChatPage from './views/chat/ChatPage'
import { useAuth } from './stores/auth'

// ⚠️ 管理端拖进了 echarts / shadcn 一大坨，而它与问答区完全无关 ——
//    懒加载拆包（M3 的 K-3：主包当时已经 811 kB）。问答区自己的拆包留到 4d。
const AdminLayout = lazy(() => import('./layouts/AdminLayout'))
const AdminDashboard = lazy(() => import('./views/admin/AdminDashboard'))
const DocumentsPage = lazy(() => import('./views/admin/DocumentsPage'))
const VersionsPage = lazy(() => import('./views/admin/VersionsPage'))
const RefusalsPage = lazy(() => import('./views/admin/RefusalsPage'))
const UsersPage = lazy(() => import('./views/admin/UsersPage'))
// 效果评测拆成三个子页（§6.1）—— 评价指标、题库、消融实验是三件事，
// 混在一页里点进去先得找。三个都懒加载。
const EvalSetsPage = lazy(() => import('./views/admin/eval/EvalSetsPage'))
const EvalRunsPage = lazy(() => import('./views/admin/eval/EvalRunsPage'))
const EvalAblationPage = lazy(() => import('./views/admin/eval/EvalAblationPage'))

const Loading = () => <div className="p-8 text-sm text-muted-foreground">加载中…</div>

/** 路由守卫：未登录跳登录页；角色不符重定向到问答页（§4.1 单工程双端）。 */
function RequireAuth({ children, roles }: { children: React.ReactNode; roles?: string[] }) {
  const { user, bootstrapped } = useAuth()
  const location = useLocation()

  // ⚠️ `bootstrap()` 在 `useEffect` 里跑，也就是**首帧之后**才发起 ——
  //    首帧时 user 一定是 null。这时候判「未登录」就会把刷新 /admin 的人
  //    弹走（实测：先跳 /login，登录页又按已登录跳到 /chat）。
  if (!bootstrapped) return <Loading />

  // ⚠️ 用**查询串**记目的地，不用 router state：state 存在 history 条目上，
  //    实测经过一次 replace 跳转后就丢了（点「管理端入口」再登录会落回 /chat）。
  //    查询串是幂等的，刷新、重定向都不丢。
  if (!user) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />
  }
  if (roles && !roles.includes(user.role)) return <Navigate to="/chat" replace />
  return <>{children}</>
}

/** 从哪里来回哪里去：`?next=` 只认站内以 /admin 开头的路径（防开放重定向）。 */
function nextPath(search: string): string | null {
  const next = new URLSearchParams(search).get('next')
  return next && next.startsWith('/admin') ? next : null
}

export default function App() {
  const { user, bootstrap } = useAuth()
  const location = useLocation()

  useEffect(() => {
    void bootstrap()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Routes>
      {/* 登录后去哪：① `?next=` 指定的地方（管理员入口用）② 管理员默认进管理端
          ③ 其余回问答页。**管理员别再手敲 /admin 了** —— 那正是这个改动的目的。 */}
      <Route path="/login"
             element={user
               ? <Navigate to={nextPath(location.search)
                              ?? (user.role === 'admin' ? '/admin' : '/chat')} replace />
               : <LoginPage />} />
      <Route
        path="/chat/*"
        element={
          <RequireAuth>
            <ChatPage />
          </RequireAuth>
        }
      />
      {/* 管理端：非 admin 访问 /admin/* 直接被守卫踢回 /chat（§4.1） */}
      <Route
        path="/admin"
        element={
          <RequireAuth roles={['admin']}>
            <Suspense fallback={<Loading />}>
              <AdminLayout />
            </Suspense>
          </RequireAuth>
        }
      >
        <Route index element={<Suspense fallback={<Loading />}><AdminDashboard /></Suspense>} />
        <Route path="documents"
               element={<Suspense fallback={<Loading />}><DocumentsPage /></Suspense>} />
        <Route path="versions"
               element={<Suspense fallback={<Loading />}><VersionsPage /></Suspense>} />
        <Route path="refusals"
               element={<Suspense fallback={<Loading />}><RefusalsPage /></Suspense>} />
        <Route path="users"
               element={<Suspense fallback={<Loading />}><UsersPage /></Suspense>} />
        {/* `/admin/eval` 直接重定向到评测集管理（§6.1）——
            老的平铺页已拆成三个子页，直接落到第一个，别给一个空壳。 */}
        <Route path="eval" element={<Navigate to="/admin/eval/sets" replace />} />
        <Route path="eval/sets"
               element={<Suspense fallback={<Loading />}><EvalSetsPage /></Suspense>} />
        <Route path="eval/runs"
               element={<Suspense fallback={<Loading />}><EvalRunsPage /></Suspense>} />
        <Route path="eval/ablation"
               element={<Suspense fallback={<Loading />}><EvalAblationPage /></Suspense>} />
      </Route>
      <Route path="*" element={<Navigate to={user ? '/chat' : '/login'} replace />} />
    </Routes>
  )
}
