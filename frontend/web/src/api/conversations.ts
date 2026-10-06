/**
 * 会话接口封装（§3.7.2）。
 *
 * ⚠️ 类型来自 `schema.d.ts`（`npm run gen:api` 生成），不手写。
 */
import { request } from './client'
import type { components } from './schema'

type S = components['schemas']

export type ConversationItem = S['ConversationItem']
export type MessageItem = S['MessageItem']
export type ConversationListResponse = S['ConversationListResponse']
export type MessagesResponse = S['MessagesResponse']

/** 排序固定 `is_top DESC, last_chat_time DESC`；软删的不出现也不计数（§3.7.2）。 */
export const listConversations = (offset = 0, limit = 20) =>
  request<ConversationListResponse>(
    `/conversations?offset=${offset}&limit=${limit}`,
  )

/**
 * 单会话消息，**按时间升序、不分页**。
 *
 * ⚠️ 越权 / 不存在 / 已软删一律 **404**（M4-D4）——
 *    原实现对越权静默返回 `[]`，前端分不清「空会话」与「不是你的会话」。
 */
export const getMessages = (sessionId: string) =>
  request<MessagesResponse>(`/conversations/${sessionId}/messages`)

export const createConversation = (title?: string) =>
  request<ConversationItem>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(title ? { title } : {}),
  })

export const patchConversation = (sessionId: string,
                                  body: { title?: string; is_top?: boolean }) =>
  request<ConversationItem>(`/conversations/${sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const deleteConversation = (sessionId: string) =>
  request<{ message: string }>(`/conversations/${sessionId}`, { method: 'DELETE' })
