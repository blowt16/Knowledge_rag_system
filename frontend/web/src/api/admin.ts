/**
 * 管理端接口封装（§3.7.3 / §4.3 / §4.4）。
 *
 * ⚠️ 类型全部来自 `schema.d.ts` —— 它由 `npm run gen:api` 从后端
 * `openapi.json` 生成（M4 把 `openapi-typescript` 接上了）。
 * **不要在这里手写接口形状**：后端改了字段，这里不跟就会静默对不上
 * （M5 的 CI 有一条 contract job 专门 diff 这两者）。
 */
import { request } from './client'
import type { components } from './schema'

type S = components['schemas']

export type DocumentItem = S['DocumentItem']
export type DocumentListResponse = S['DocumentListResponse']
export type VersionListResponse = S['VersionListResponse']
export type ChunkItem = S['ChunkItem']
export type ChunkListResponse = S['ChunkListResponse']
export type UserItem = S['UserItem']
export type UserListResponse = S['UserListResponse']
export type RoleItem = S['RoleItem']
export type RefusalItem = S['RefusalItem']
export type RefusalListResponse = S['RefusalListResponse']
export type AnnotationItem = S['AnnotationItem']
export type OverviewResponse = S['OverviewResponse']
export type TrendResponse = S['TrendResponse']
export type RefusalStatsResponse = S['RefusalStatsResponse']
export type HotQuestionsResponse = S['HotQuestionsResponse']
export type RetrievalMetricsResponse = S['RetrievalMetricsResponse']

/** 分页请求的公共参数（全站 `page_size ≤ 100`，§4.3.1.1）。 */
export interface PageParams {
  page?: number
  page_size?: number
}

function qs(params: object): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v === undefined || v === null || v === '') continue
    sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

// ---------------- 文档（§4.3.1 / §4.3.2） ----------------

export interface DocumentFilters extends PageParams {
  status?: 'all' | 'indexing' | 'active' | 'disabled' | 'failed'
  visibility?: 'all' | 'public' | 'restricted'
  q?: string
}

export const listDocuments = (f: DocumentFilters = {}) =>
  request<DocumentListResponse>(`/admin/documents${qs(f)}`)

export const listVersions = (groupId: string) =>
  request<VersionListResponse>(`/admin/documents/${groupId}/versions`)

export const listChunks = (documentId: string, p: PageParams = {}) =>
  request<ChunkListResponse>(`/admin/documents/${documentId}/chunks${qs(p)}`)

/**
 * 改可改字段。
 * ⚠️ `visibility` / `visible_roles` / `effective_date` / `status` 会**触发重索引**
 * （它们冗余在 Chroma 里，§4.3.1.2）—— 界面必须**显式告知耗时并给进度**，
 * 不能做成静默的即时保存。
 */
export const patchDocument = (
  documentId: string,
  body: Partial<Pick<DocumentItem, 'title' | 'visibility' | 'visible_roles'
    | 'effective_date' | 'status'>>,
) => request<DocumentItem>(`/admin/documents/${documentId}`, {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const disableDocument = (id: string) =>
  request<DocumentItem>(`/admin/documents/${id}/disable`, { method: 'POST' })

export const enableDocument = (id: string) =>
  request<DocumentItem>(`/admin/documents/${id}/enable`, { method: 'POST' })

export const deleteDocument = (id: string) =>
  request<{ message: string }>(`/admin/documents/${id}`, { method: 'DELETE' })

// ---------------- 用户与角色（§4.3 用户管理） ----------------

export const listRoles = () => request<RoleItem[]>('/admin/roles')

export const listUsers = (p: PageParams & { role?: string } = {}) =>
  request<UserListResponse>(`/admin/users${qs(p)}`)

export const createUser = (body: { username: string; password: string; role: string }) =>
  request<UserItem>('/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const patchUser = (userId: string,
                          body: { role?: string; is_active?: boolean }) =>
  request<UserItem>(`/admin/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const resetPassword = (userId: string, password: string) =>
  request<UserItem>(`/admin/users/${userId}/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })

// ---------------- 拒答分析（§4.3.1.3） ----------------

/** `kind` 默认 `refused`（向后兼容）；`clarify` 列**被反问**的轮次 —— */
export const listRefusals = (p: PageParams & { kind?: 'refused' | 'clarify' | 'all' } = {}) =>
  request<RefusalListResponse>(`/admin/refusals${qs(p)}`)

export const annotateRefusal = (
  logId: string,
  body: { suggested_document_id?: string; note?: string },
) => request<{ annotation: AnnotationItem }>(`/admin/refusals/${logId}/annotate`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

// ---------------- 仪表盘（§4.4） ----------------

export const statsOverview = () => request<OverviewResponse>('/admin/stats/overview')

export const statsTrend = (days = 30) =>
  request<TrendResponse>(`/admin/stats/trend${qs({ days })}`)

export const statsRefusals = () => request<RefusalStatsResponse>('/admin/stats/refusals')

export const statsHotQuestions = (limit = 10) =>
  request<HotQuestionsResponse>(`/admin/stats/hot-questions${qs({ limit })}`)

/** ⚠️ 运行指标不可用时会返回 `{available:false}` 且 **HTTP 200** ——
 * 它不是错误，业务指标区块要照常渲染（M4-D5）。 */
export const statsRetrieval = () =>
  request<RetrievalMetricsResponse>('/admin/stats/retrieval')
