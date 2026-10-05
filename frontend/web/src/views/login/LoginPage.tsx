import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../stores/auth'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const { login, loading, error, clearError } = useAuth()
  const navigate = useNavigate()

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    await login(username, password)
    // 登录成功由 App 的路由守卫跳转，这里不直接 navigate，
    // 避免在失败时也跳走
    if (useAuth.getState().user) navigate('/chat', { replace: true })
  }

  return (
    <div className="center-page">
      <form className="card login-card" onSubmit={onSubmit}>
        <h1>校园 RAG 检索问答系统</h1>
        <p className="muted">请使用管理员分配的账号登录</p>

        <label>
          用户名
          <input
            value={username}
            autoComplete="username"
            onChange={(e) => {
              setUsername(e.target.value)
              clearError()
            }}
            required
          />
        </label>

        <label>
          口令
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(e) => {
              setPassword(e.target.value)
              clearError()
            }}
            required
          />
        </label>

        {error && <div className="error">{error}</div>}

        <button type="submit" disabled={loading || !username || !password}>
          {loading ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  )
}
