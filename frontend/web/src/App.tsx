import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './views/login/LoginPage'
import ChatPage from './views/chat/ChatPage'
import { useAuth } from './stores/auth'

/** 路由守卫：未登录跳登录页；非 admin 访问 /admin/* 重定向到 /chat。 */
function RequireAuth({ children, roles }: { children: React.ReactNode; roles?: string[] }) {
  const { user } = useAuth()
  const location = useLocation()

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
      {/* 管理端路由在 M4 补：/admin/* 需 roles={['admin']} */}
      <Route path="*" element={<Navigate to={user ? '/chat' : '/login'} replace />} />
    </Routes>
  )
}
