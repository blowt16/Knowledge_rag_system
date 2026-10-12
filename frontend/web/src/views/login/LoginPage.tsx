import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { useAuth } from '../../stores/auth'
import { useTheme } from '../../stores/theme'
import { loadCredentials, saveCredentials } from '../../lib/credentials'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const { login, loading, error, clearError } = useAuth()
  const { theme, toggle: toggleTheme } = useTheme()

  // 挂载时读一次存档并回填
  useEffect(() => {
    const saved = loadCredentials()
    if (saved) {
      setUsername(saved.username)
      setPassword(saved.password)
      setRemember(true)
    }
  }, [])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    await login(username, password)

    // ⚠️ 落盘必须**紧接着 login 之后、在这个函数里**做，不能放到 effect 里。
    //    登录成功时 store 会在同一次更新里既设 `user` 又清 `loading`，
    //    路由守卫立刻把本页**卸载** —— 依赖 `loading` 的 effect 根本没机会跑
    //    （实测：登录成功但 localStorage 里什么都没有）。
    //    同样地，也不能「一提交就存」：那样输错的口令会被存下来反复回填。
    //    这里登录已 resolve，直接问 store 结果最可靠。
    if (!useAuth.getState().error) {
      saveCredentials(remember ? { username, password } : null)
    }
    // ⚠️ 登录成功**不在这里跳转** —— 交给 `App.tsx` 的登录路由：
    //    它会优先回「你原本想去的地方」（管理端入口靠这个），
    //    否则回 /chat。这里跳的话会把那个目的地盖掉。
  }

  return (
    <div className="center-page">
      {/* 登录页也能切主题 —— 深色用户不该在这页先被闪一下白底 */}
      <button type="button" className="icon-btn theme-fab" onClick={toggleTheme}
              title={theme === 'dark' ? '切换到浅色' : '切换到深色'}
              aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
        {theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}
      </button>

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

        <label className="remember-row">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          <span>记住账号和口令</span>
        </label>
        {/* ⚠️ 这行提示**不要删**：勾上之后口令是**明文**存在浏览器里的，没有加密。
            这是用户能在知情前提下做选择所依赖的全部信息。 */}
        {remember && (
          <p className="muted small remember-hint">
            口令将以明文保存在本机浏览器中，请勿在共用电脑上勾选。
          </p>
        )}

        {error && <div className="error">{error}</div>}

        <button type="submit" disabled={loading || !username || !password}>
          {loading ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  )
}
