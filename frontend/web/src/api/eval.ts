/**
 * 评测接口客户端（§3.7.3 / §4.3.1.4 / §5）。
 *
 * ⚠️ `config_label` 一律用**服务端返回的**，不由前端从 config 拼 ——
 *    开关命名会演进，前端各拼各的就会出现同一次运行在不同页面叫不同名字
 *    （§4.3.1.4）。
 */
import { request, requestBlobWithName } from './client'
import type { components } from './schema'

type S = components['schemas']

export type EvalRunCreated = S['EvalRunCreated']
export type EvalRunDetail = S['EvalRunDetail']
export type EvalRunList = S['EvalRunList']
export type EvalCompareResponse = S['EvalCompareResponse']
export type EvalCompareRow = S['EvalCompareRow']
export type EvalRunSummary = S['EvalRunSummary']
export type EvalReport = S['EvalReport']
export type EvalReportCase = S['EvalReportCase']
export type EvalSetItem = S['EvalSetItem']
export type EvalCaseItem = S['EvalCaseItem']
export type EvalCasePage = S['EvalCasePage']
export type EvalCaseSource = S['EvalCaseSource']
export type EvalGenerateResponse = S['EvalGenerateResponse']

export interface StartRunBody {
  name?: string
  /** 走**批量评测**就必须给这个，且不要带 `suite` / `config`（§5.4）。 */
  set_id?: string
  suite?: 'full' | 'refusal_calib'
  role: 'student' | 'staff' | 'admin'
  include_restricted?: boolean
  /** 消融开关。**留空 = 线上完整链路**；带上任意键（含 `suite`）= 消融运行。 */
  config?: Record<string, boolean> | null
}

/** 起一轮评测。**立即返回**（202），跑完要轮询 `evalRun`。 */
export const startEvalRun = (body: StartRunBody) =>
  request<EvalRunCreated>('/admin/eval/run', {
    method: 'POST',
    body: JSON.stringify(body),
  })

/** 任务表（§6.3）。分页在服务端做。 */
export const evalRuns = (p: { page?: number; page_size?: number } = {}) =>
  request<EvalRunList>(
    `/admin/eval/runs?page=${p.page ?? 1}&page_size=${p.page_size ?? 20}`)

export const evalRun = (runId: string) =>
  request<EvalRunDetail>(`/admin/eval/runs/${runId}`)

export const deleteEvalRun = (runId: string) =>
  request<void>(`/admin/eval/runs/${runId}`, { method: 'DELETE' })

/** 对比表 —— **服务端**产出配置与对齐后的指标矩阵，前端不拼（§4.3.1.4）。 */
export const evalCompare = (runIds: string[]) =>
  request<EvalCompareResponse>(`/admin/eval/compare?run_ids=${runIds.join(',')}`)

// ---- 评测集（§5.1）----------------------------------------------------

export const listEvalSets = () =>
  request<{ items: EvalSetItem[] }>('/admin/eval/sets')

export const createEvalSet = (body: { name: string; description?: string | null }) =>
  request<EvalSetItem>('/admin/eval/sets', { method: 'POST', body: JSON.stringify(body) })

export const patchEvalSet = (
  setId: string,
  body: { name?: string; description?: string | null },
) => request<EvalSetItem>(`/admin/eval/sets/${setId}`, {
  method: 'PATCH', body: JSON.stringify(body),
})

export const deleteEvalSet = (setId: string) =>
  request<void>(`/admin/eval/sets/${setId}`, { method: 'DELETE' })

/**
 * 导出评测集。
 *
 * ⚠️ 走 `requestBlobWithName` 而**不是** `<a href="/api/...">` ——
 *    后者带不上 `Authorization` 头（见 `lib/download.ts`）。
 */
export const exportEvalSet = (setId: string) =>
  requestBlobWithName(`/admin/eval/sets/${setId}/export`)

// ---- 用例（§5.2）------------------------------------------------------

export const listEvalCases = (
  setId: string,
  p: { source?: string; q?: string; page?: number; page_size?: number } = {},
) => {
  const qs = new URLSearchParams()
  if (p.source) qs.set('source', p.source)
  if (p.q) qs.set('q', p.q)
  qs.set('page', String(p.page ?? 1))
  qs.set('page_size', String(p.page_size ?? 20))
  return request<EvalCasePage>(`/admin/eval/sets/${setId}/cases?${qs}`)
}

export const createEvalCase = (
  setId: string,
  body: { question: string; ground_truth?: string | null; in_eval?: boolean; note?: string | null },
) => request<EvalCaseItem>(`/admin/eval/sets/${setId}/cases`, {
  method: 'POST', body: JSON.stringify(body),
})

export const patchEvalCase = (
  caseId: string,
  body: { question?: string; ground_truth?: string | null; in_eval?: boolean; note?: string | null },
) => request<EvalCaseItem>(`/admin/eval/cases/${caseId}`, {
  method: 'PATCH', body: JSON.stringify(body),
})

export const deleteEvalCase = (caseId: string) =>
  request<void>(`/admin/eval/cases/${caseId}`, { method: 'DELETE' })

/** 「核对标准答案」弹窗的数据（含标准答案在原文里的高亮区间）。 */
export const evalCaseSource = (caseId: string) =>
  request<EvalCaseSource>(`/admin/eval/cases/${caseId}/source`)

/** 从文档自动生成用例（§8）。**同步等待**，最多 120 秒。 */
export const generateEvalCases = (
  setId: string,
  body: { document_id: string; count: number },
) => request<EvalGenerateResponse>(`/admin/eval/sets/${setId}/generate`, {
  method: 'POST', body: JSON.stringify(body),
})
