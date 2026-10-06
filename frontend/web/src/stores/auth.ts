/** 认证状态（zustand）。 */
import { create } from 'zustand'
import { login as apiLogin, logout as apiLogout, me, setUnauthorizedHandler, loadTokens } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  loading: boolean
  /**
   * `bootstrap()` 是否已经跑完（无论成败）。
   *
   * ⚠️ 路由守卫必须用它、**不能**用 `loading` 来判断「还没问出来」：
   *    `bootstrap` 是在 `useEffect` 里调的，也就是**首帧之后**才跑 ——
   *    首帧时 `loading` 还是初始的 false，守卫会立刻判「未登录」并跳走。
   *    实测：直接刷新 `/admin` 会被弹回 `/chat`（先出 /login 再被登录页
   *    重定向到 /chat），刷新等于丢掉当前页。
   */
  bootstrapped: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  bootstrap: () => Promise<void>
  clearError: () => void
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  loading: false,
  bootstrapped: false,
  error: null,

  async login(username, password) {
    set({ loading: true, error: null })
    try {
      const pair = await apiLogin(username, password)
      set({ user: pair.user, loading: false })
    } catch (e) {
      set({ loading: false, error: (e as Error).message })
    }
  },

  async logout() {
    await apiLogout()
    set({ user: null })
  },

  /** 刷新页面后恢复登录态；access_token 可能已过期，client 会自动续期一次。 */
  async bootstrap() {
    loadTokens()
    setUnauthorizedHandler(() => set({ user: null }))
    try {
      set({ user: await me() })
    } catch {
      set({ user: null })
    } finally {
      set({ bootstrapped: true })
    }
  },

  clearError: () => set({ error: null }),
}))
