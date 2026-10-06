/** 评测接口客户端（§3.7.3 / §4.3.1.4）。 */
import { request } from './client'
import type { components } from './schema'

type S = components['schemas']

export type EvalRunCreated = S['EvalRunCreated']
export type EvalRunDetail = S['EvalRunDetail']
export type EvalRunList = S['EvalRunList']
export type EvalCompareResponse = S['EvalCompareResponse']
export type EvalRunSummary = S['EvalRunSummary']

export interface StartRunBody {
  name?: string
  suite: 'full' | 'refusal_calib'
  role: 'student' | 'staff' | 'admin'
  include_restricted: boolean
  config?: Record<string, boolean> | null
}

/** 起一轮评测。**立即返回**（202），跑完要轮询 `evalRun`。 */
export const startEvalRun = (body: StartRunBody) =>
  request<EvalRunCreated>('/admin/eval/run', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const evalRuns = (limit = 20) =>
  request<EvalRunList>(`/admin/eval/runs?limit=${limit}`)

export const evalRun = (runId: string) =>
  request<EvalRunDetail>(`/admin/eval/runs/${runId}`)

/** 对比表 —— **服务端**产出配置与对齐后的指标矩阵，前端不拼（§4.3.1.4）。 */
export const evalCompare = (runIds: string[]) =>
  request<EvalCompareResponse>(`/admin/eval/compare?run_ids=${runIds.join(',')}`)
