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

const Loading = () => <div className="p-8 text-sm text-muted-foreground">加载中…</div>

/** 路由守卫：未登录跳登录页；角色不符重定向到问答页（§4.1 单工程双端）。 */
function RequireAuth({ children, roles }: { children: React.ReactNode; roles?: string[] }) {
  const { user, bootstrapped } = useAuth()
  const location = useLocation()

  // ⚠️ `bootstrap()` 在 `useEffect` 里跑，也就是**首帧之后**才发起 ——
  //    首帧时 user 一定是 null。这时候判「未登录」就会把刷新 /admin 的人
  //    弹走（实测：先跳 /login，登录页又按已登录跳到 /chat）。
  if (!bootstrapped) return <Loading />

  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  if (roles && !roles.includes(user.role)) return <Navigate to="/chat" replace />
  return <>{children}</>
}

export default function App() {
  const { user, bootstrap } = useAuth()

  useEffect(() => {
    void bootstrap()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/chat" replace /> : <LoginPage />} />
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
      </Route>
      <Route path="*" element={<Navigate to={user ? '/chat' : '/login'} replace />} />
    </Routes>
  )
}
