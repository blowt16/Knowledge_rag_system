/**
 * 前端类型 —— 与后端 Pydantic 契约一一对应（backend/app/schemas/chat.py）。
 *
 * ⚠️ 方案 §3.1 的约定：最终由 **FastAPI 的 OpenAPI → `openapi-typescript`** 生成
 *    `src/api/schema.d.ts`（`npm run gen:api`），生成物提交进仓库，
 *    这样前端在没起后端时也能编译，且 diff 里能看见接口变化。
 *
 * M0 先手写最小集（后端契约刚钉死，生成链路要等 M4 一起接）。
 */

export type RouteName = 'chat' | 'clarify' | 'knowledge'
export type RefusalReason = 'no_candidate' | 'insufficient_evidence'
export type Decision = 'ANSWERED' | 'REFUSED_NO_EVIDENCE'
/** SSE `stage` 的取值（§3.7.2，封闭词表） */
export type StageName =
  | 'routing' | 'resolving' | 'retrieving' | 'reranking' | 'generating' | 'verifying'
export type SseErrorCode =
  | 'timeout' | 'upstream_error' | 'context_length_exceeded' | 'internal' | 'session_busy'

export interface UserInfo {
  id: string
  username: string
  role: 'student' | 'staff' | 'admin'
}

export interface Box {
  page: number
  x0: number
  x1: number
  top: number
  bottom: number
}

export interface JumpTarget {
  /** documents 表主键 —— 调 /api/documents/{id}/file 需要它 */
  document_id: string
  page: number
  char_start: number
  char_end: number
  /** 主定位依据（坐标高亮）；非 PDF / 扫描件 / 旧索引为空数组 */
  boxes: Box[]
}

export interface Citation {
  marker: number
  document_name: string
  chapter: string
  page: number
  snippet: string
  chunk_id: string
  /** 越权取得（admin 提权检索时）—— 5.3 的 ACL 对照实验直接读它 */
  escalated: boolean
  /**
   * ⚠️ 是**文件名**，不是 URL。
   * 签名 URL 只有 5 分钟有效，而 citations 会落库、用于刷新后重渲染 ——
   * 存 URL 的话刷新历史会话时图片全部裂图。渲染时按 name 现取签名 URL。
   */
  images: string[]
  jump_target: JumpTarget | null
}

export interface UncitedClaim {
  /** ⚠️ sentence 不是冗余字段：它是前端「校验 A」的输入 */
  sentence: string
  char_start: number
  char_end: number
}

export interface InvalidMarker {
  marker: number
  char_start: number
  char_end: number
}

export interface VerifyReport {
  total_claims: number
  cited_claims: number
  invalid_markers: InvalidMarker[]
  uncited_claims: UncitedClaim[]
}

/** 一条助手消息在流式过程中的累积状态。 */
export interface AssistantDraft {
  /** 当前阶段（§3.7.2）；用于填充首个 token 到达前的静默期 */
  stage: StageName | null
  /** 要显示的阶段文案。**以 stage 为准** —— label 只兜底（§4.2.4.2） */
  stageHint: string
  route: RouteName | null
  /** ⚠️ 字段名是 clarify_facets，不是 facets —— 写成 evt.facets 会恒为 undefined */
  clarifyFacets: string[]
  resolvedQuery: string
  decision: Decision | null
  text: string
  citations: Citation[]
  verify: VerifyReport | null
  refusedReason: RefusalReason | null
  refusedText: string
  refusedHint: string | null
  error: { code: SseErrorCode | string; message: string } | null
  latencyMs: number | null
  done: boolean
}
