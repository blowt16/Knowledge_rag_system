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

/**
 * 引导态（问 `/auth/me`「我是谁」）的超时上限。
 *
 * 取 8 秒：正常局域网内这个请求是毫秒级；超过 8 秒基本可以判死，
 * 而用户还能接受「等 8 秒后落到登录页」这种程度的等待。
 */
const BOOTSTRAP_TIMEOUT_MS = 8000

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
      // ⚠️ **必须带超时**（评审 M4-K5）：`/api/auth/me` 若既不返回也不断开
      //    （代理接了连接不回包、连接半死），`bootstrapped` 会永远为 false ——
      //    路由守卫于是永远停在「加载中…」，没有任何重试入口。
      //    超时就按未登录处理：跳登录页，用户重新登录即可。
      set({ user: await me(AbortSignal.timeout(BOOTSTRAP_TIMEOUT_MS)) })
    } catch {
      set({ user: null })
    } finally {
      set({ bootstrapped: true })
    }
  },

  clearError: () => set({ error: null }),
}))
