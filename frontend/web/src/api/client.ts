/**
 * HTTP 客户端 —— 两端共用（User 端与管理端）。
 *
 * 口径：
 *  - 身份只来自 JWT；**不往请求体里放 user_id**（方案附录 A 第 2 条）
 *  - access_token 过期时静默续期，**不能把用户踢回登录页**（§3.7.1）
 */

import type { UserInfo } from './types'

const BASE = '/api'

let accessToken: string | null = null
let refreshToken: string | null = null
let onUnauthorized: (() => void) | null = null

export function setTokens(access: string | null, refresh: string | null): void {
  accessToken = access
  refreshToken = refresh
  if (access) sessionStorage.setItem('access_token', access)
  else sessionStorage.removeItem('access_token')
  if (refresh) sessionStorage.setItem('refresh_token', refresh)
  else sessionStorage.removeItem('refresh_token')
}

export function loadTokens(): void {
  accessToken = sessionStorage.getItem('access_token')
  refreshToken = sessionStorage.getItem('refresh_token')
}

export function getAccessToken(): string | null {
  return accessToken
}

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public traceId?: string,
  ) {
    super(message)
  }
}

async function parseError(resp: Response): Promise<ApiError> {
  let code = `http_${resp.status}`
  let message = `请求失败（HTTP ${resp.status}）`
  let traceId: string | undefined
  try {
    const j = await resp.json()
    code = j.code ?? code
    message = j.message ?? message
    traceId = j.trace_id
  } catch {
    /* 保持默认 */
  }
  return new ApiError(resp.status, code, message, traceId)
}

/** 用 refresh_token 换新的 access_token。refresh_token 原样返回、不轮换。 */
export async function refreshAccessToken(): Promise<boolean> {
  if (!refreshToken) return false
  const resp = await fetch(`${BASE}/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  })
  if (!resp.ok) return false
  const data = await resp.json()
  setTokens(data.access_token, data.refresh_token)
  return true
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
  retryOn401 = true,
): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(options.headers ?? {}),
    },
  })

  if (resp.status === 401 && retryOn401) {
    // 静默续期一次；失败才登出
    if (await refreshAccessToken()) return request<T>(path, options, false)
    onUnauthorized?.()
    throw await parseError(resp)
  }
  if (!resp.ok) throw await parseError(resp)
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

// ---- 认证接口 ----------------------------------------------------------

export interface TokenPair {
  access_token: string
  refresh_token: string
  expires_in: number
  user: UserInfo
}

export async function login(username: string, password: string): Promise<TokenPair> {
  const data = await request<TokenPair>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  setTokens(data.access_token, data.refresh_token)
  return data
}

export async function logout(): Promise<void> {
  try {
    await request('/auth/logout', { method: 'POST' })
  } finally {
    setTokens(null, null)
  }
}

export async function me(): Promise<UserInfo> {
  return request<UserInfo>('/auth/me')
}
