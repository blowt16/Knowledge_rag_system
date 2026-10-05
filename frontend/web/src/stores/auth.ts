/** 认证状态（zustand）。 */
import { create } from 'zustand'
import { login as apiLogin, logout as apiLogout, me, setUnauthorizedHandler, loadTokens } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  loading: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  bootstrap: () => Promise<void>
  clearError: () => void
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  loading: false,
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
    }
  },

  clearError: () => set({ error: null }),
}))
