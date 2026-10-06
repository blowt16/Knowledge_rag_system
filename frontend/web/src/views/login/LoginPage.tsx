import { useState } from 'react'
import { useAuth } from '../../stores/auth'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const { login, loading, error, clearError } = useAuth()

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    await login(username, password)
    // ⚠️ 登录成功**不在这里跳转** —— 交给 `App.tsx` 的登录路由：
    //    它会优先回「你原本想去的地方」（管理端入口靠这个），
    //    否则回 /chat。这里跳的话会把那个目的地盖掉。
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
