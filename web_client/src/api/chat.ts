import { apiClient, uploadRequest, getAccessToken, getDeptId, SSE_URL } from './client'
import { fetchEventSource } from '@microsoft/fetch-event-source'

export interface Conversation {
  id: string
  title: string
  is_active: boolean
  agent_id: string
  created_at: string
  updated_at: string
  last_message_at?: string
  message_count?: number
}

export interface PaginatedConversations {
  items: Conversation[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface Attachment {
  file_id: string
  filename: string
  content_type: string
  size_bytes: number
}

export interface Message {
  id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at?: string
  metadata?: {
    run_id?: string
    tokens?: Record<string, number>
    duration_ms?: number
  }
  attachments?: Attachment[]
}

export interface SendMessageResponse {
  content: string
  status: string
  metadata?: {
    run_id?: string
    tokens?: Record<string, number>
    duration_ms?: number
    pending_approvals?: string[]
    tool_calls?: ToolCallInfo[]
    background_tasks?: string[]
    has_active_bg_tasks?: boolean
  }
}

export interface ToolCallInfo {
  tool_name: string
  tool_call_id: string
  status: string
}

export interface SSEEvent {
  type: 'message_start' | 'content_delta' | 'message_end' | 'error'
  data?: string
  metadata?: SendMessageResponse['metadata']
  error?: string
}

export async function listConversations(
  orgId: string,
  page = 1,
  limit = 30,
  activeOnly = true
): Promise<PaginatedConversations> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/chat/conversations`, {
    params: { page, limit, active: activeOnly },
  })
  return response.data
}

export async function getConversation(orgId: string, convId: string): Promise<Conversation> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/chat/conversations/${convId}`)
  return response.data
}

export async function createConversation(
  orgId: string,
  title?: string,
  agentId = 'assistant'
): Promise<Conversation> {
  const payload: Record<string, string> = { agent_id: agentId }
  if (title) payload.title = title
  const response = await apiClient.post(`/v1/orgs/${orgId}/chat/conversations`, payload)
  return response.data
}

export async function updateConversation(
  orgId: string,
  convId: string,
  data: { title?: string; is_active?: boolean }
): Promise<Conversation> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/chat/conversations/${convId}`, data)
  return response.data
}

export async function deleteConversation(orgId: string, convId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/chat/conversations/${convId}`)
}

export interface MessageListResult {
  messages: Message[]
  needsPolling: boolean
  hasPendingApprovals: boolean
}

export async function listMessages(
  orgId: string,
  convId: string,
  lastN?: number
): Promise<MessageListResult> {
  const params: Record<string, number> = {}
  if (lastN) params.last_n = lastN
  const response = await apiClient.get(
    `/v1/orgs/${orgId}/chat/conversations/${convId}/messages`,
    { params }
  )
  return {
    messages: response.data,
    needsPolling: response.headers['x-needs-polling'] === 'true',
    hasPendingApprovals: response.headers['x-has-pending-approvals'] === 'true',
  }
}

export async function sendMessage(
  orgId: string,
  convId: string,
  message: string,
  attachmentIds?: string[]
): Promise<SendMessageResponse> {
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/chat/conversations/${convId}/messages`,
    { message, attachment_ids: attachmentIds ?? [] }
  )
  return response.data
}

export function streamMessage(
  orgId: string,
  convId: string,
  message: string,
  callbacks: {
    onDelta: (text: string) => void
    onDone: (metadata: SendMessageResponse['metadata'] | undefined) => void
    onError: (err: string) => void
    onPaused?: (data: { pending_approvals: string[]; run_id?: string }) => void
  },
  signal: AbortSignal,
  attachmentIds?: string[]
): void {
  const token = getAccessToken()
  const deptId = getDeptId()
  fetchEventSource(`${SSE_URL}/v1/orgs/${orgId}/chat/conversations/${convId}/messages/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: token ? `Bearer ${token}` : '',
      ...(deptId ? { 'X-Department-Id': deptId } : {}),
    },
    body: JSON.stringify({ message, attachment_ids: attachmentIds ?? [] }),
    signal,
    openWhenHidden: true,
    onmessage(ev) {
      // ev.event contains the SSE named event (e.g. "content_delta")
      // ev.data contains the JSON payload
      try {
        const parsed = JSON.parse(ev.data)
        if (ev.event === 'content_delta') {
          if (parsed.delta) callbacks.onDelta(parsed.delta)
        } else if (ev.event === 'run_paused') {
          callbacks.onPaused?.(parsed)
        } else if (ev.event === 'message_end') {
          callbacks.onDone(parsed.metadata)
        } else if (ev.event === 'error') {
          callbacks.onError(parsed.detail ?? 'Streaming error')
        }
      } catch {
        // Non-JSON data — treat as raw delta
        if (ev.data) callbacks.onDelta(ev.data)
      }
    },
    onclose() {
      // Stream closed by server without an explicit message_end (e.g. network drop).
      // Call onDone so the UI doesn't hang indefinitely.
      callbacks.onDone(undefined)
    },
    onerror(err) {
      callbacks.onError(String(err))
      throw err // stop retrying
    },
  })
}

export interface UploadedAttachment {
  id: string
  filename: string
  content_type: string
  size_bytes: number
}

export async function uploadChatAttachment(
  orgId: string,
  convId: string,
  file: File,
  description?: string
): Promise<UploadedAttachment> {
  const form = new FormData()
  form.append('file', file)
  const params = description ? `?description=${encodeURIComponent(description)}` : ''
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/chat/conversations/${convId}/attachments${params}`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } }
  )
  const data = response.data
  return {
    id: data.id,
    filename: data.filename,
    content_type: data.content_type,
    size_bytes: data.size_bytes,
  }
}
