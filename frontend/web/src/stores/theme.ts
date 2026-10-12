/**
 * 明暗主题。
 *
 * ⚠️ 深色变量（`.dark`）本来只有管理端在用，聊天页这套手写 CSS 是硬编码浅色 ——
 *    所以聊天页要深色，必须让它**全部走变量**，不能在规则里写死颜色。
 *
 * 落点：根元素上的 `dark` 类。管理端的 shadcn token 也是认这个类（§M4-D1），
 * 所以一处切换两端都跟着变。
 */
import { create } from 'zustand'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'campus-rag-theme'

function readInitial(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  // 没存过就跟系统走
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function apply(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

/** 在渲染之前调一次 —— 否则深色用户会先看到一帧白屏。 */
export function initTheme(): void {
  apply(readInitial())
}

interface ThemeState {
  theme: Theme
  toggle: () => void
}

export const useTheme = create<ThemeState>((set, get) => ({
  theme: 'light',
  toggle: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem(STORAGE_KEY, next)
    apply(next)
    set({ theme: next })
  },
}))

// store 初始化时把真实值灌进去（上面的 'light' 只是占位，避免 SSR 式闪烁）
useTheme.setState({ theme: readInitial() })
