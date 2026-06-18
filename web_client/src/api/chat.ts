import { apiClient, uploadRequest, getAccessToken, getDeptId, SSE_URL } from './client'
import { fetchEventSource } from '@microsoft/fetch-event-source'

export interface Conversation {
  id: string
  title: string
  is_active: boolean
  folder_id?: string | null
  agent_id: string
  created_at: string
  updated_at: string
  last_message_at?: string
  message_count?: number
}

export interface Folder {
  id: string
  name: string
  is_active: boolean
  conversation_count: number
  created_at: string
  updated_at: string
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
  // Optional run-level status surfaced from the backend.
  // 'error' means the underlying agent run failed; the UI should render an
  // error badge so users can see the turn ended in failure.
  // 'paused' means the run is awaiting HITL approval.
  status?: 'error' | 'paused' | null
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
  // Optional run-level status emitted on message_end (e.g. 'error').
  status?: 'error' | 'paused' | null
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
  agentId = 'assistant',
  folderId?: string | null
): Promise<Conversation> {
  const payload: Record<string, string> = { agent_id: agentId }
  if (title) payload.title = title
  if (folderId) payload.folder_id = folderId
  const response = await apiClient.post(`/v1/orgs/${orgId}/chat/conversations`, payload)
  return response.data
}

export async function updateConversation(
  orgId: string,
  convId: string,
  data: { title?: string; is_active?: boolean; folder_id?: string | null; clear_folder?: boolean }
): Promise<Conversation> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/chat/conversations/${convId}`, data)
  return response.data
}

export async function deleteConversation(orgId: string, convId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/chat/conversations/${convId}`)
}

// ---------------------------------------------------------------------------
// Conversation folders
// ---------------------------------------------------------------------------

export async function listFolders(orgId: string, activeOnly = true): Promise<Folder[]> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/chat/folders`, {
    params: { active: activeOnly },
  })
  return response.data
}

export async function createFolder(orgId: string, name: string): Promise<Folder> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/chat/folders`, { name })
  return response.data
}

export async function updateFolder(
  orgId: string,
  folderId: string,
  data: { name?: string; is_active?: boolean }
): Promise<Folder> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/chat/folders/${folderId}`, data)
  return response.data
}

export async function deleteFolder(orgId: string, folderId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/chat/folders/${folderId}`)
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

export interface MessageCheck {
  last_message_id: string | null
  message_count: number
}

/** Lightweight poll — returns only the last message id so the UI can
 *  decide whether to refetch the full message list. */
export async function checkMessages(orgId: string, convId: string): Promise<MessageCheck> {
  const response = await apiClient.get(
    `/v1/orgs/${orgId}/chat/conversations/${convId}/messages/check`
  )
  return response.data
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
    onDone: (metadata: SendMessageResponse['metadata'] | undefined, status?: 'error' | 'paused' | null) => void
    onError: (err: string) => void
    onPaused?: (data: { pending_approvals: string[]; run_id?: string }) => void
  },
  signal: AbortSignal,
  attachmentIds?: string[]
): void {
  const token = getAccessToken()
  const deptId = getDeptId()
  // Guard against double-invocation of terminal callbacks: the server sends
  // ``message_end`` and then closes the connection, which would otherwise
  // fire ``onclose`` and call ``onDone`` a second time (causing duplicate
  // query invalidations / refetches on the UI side).
  let terminated = false
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
          if (terminated) return
          terminated = true
          callbacks.onDone(parsed.metadata, parsed.status ?? null)
        } else if (ev.event === 'error') {
          if (terminated) return
          terminated = true
          callbacks.onError(parsed.detail ?? 'Streaming error')
        }
      } catch {
        // Non-JSON data — treat as raw delta
        if (ev.data) callbacks.onDelta(ev.data)
      }
    },
    onclose() {
      // Stream closed by server. If a terminal event (``message_end`` or
      // ``error``) already fired, do NOT call onDone again — that would
      // re-invalidate the messages/conversations queries and trigger
      // duplicate GETs. Only act as a fallback for true connection drops
      // where no terminal event was received.
      if (terminated) return
      terminated = true
      callbacks.onDone(undefined)
    },
    onerror(err) {
      if (!terminated) {
        terminated = true
        callbacks.onError(String(err))
      }
      throw err // stop retrying
    },
  })
}

/**
 * Subscribe to conversation update events via SSE.
 *
 * The backend emits a ``messages_updated`` event whenever a new
 * assistant/tool message is appended to the conversation from outside the
 * caller's own request — most importantly when a background-tool
 * continuation finishes in a Celery worker.  Consumers should react by
 * refetching the message list immediately, avoiding the 5 s polling delay.
 *
 * Returns a function that, when called, closes the subscription.
 */
export function subscribeConversationEvents(
  orgId: string,
  convId: string,
  onUpdate: (reason: string) => void
): () => void {
  const token = getAccessToken()
  const deptId = getDeptId()
  const controller = new AbortController()
  fetchEventSource(`${SSE_URL}/v1/orgs/${orgId}/chat/conversations/${convId}/events`, {
    method: 'GET',
    headers: {
      Authorization: token ? `Bearer ${token}` : '',
      ...(deptId ? { 'X-Department-Id': deptId } : {}),
    },
    signal: controller.signal,
    openWhenHidden: true,
    onmessage(ev) {
      if (ev.event === 'messages_updated') {
        try {
          const parsed = JSON.parse(ev.data || '{}')
          onUpdate(parsed.reason ?? 'updated')
        } catch {
          onUpdate('updated')
        }
      }
      // Ignore ``connected`` and any other event types.
    },
    onclose() {
      // Server closed the connection cleanly.  fetch-event-source will
      // NOT automatically retry on clean close without this handler.
      // Throw so the library retries with backoff — the controller's
      // abort() (called from cleanup) is the canonical way to stop.
      throw new Error('SSE connection closed — retrying')
    },
    onerror(err) {
      // Let fetch-event-source retry with backoff by NOT re-throwing.
      // The controller's abort() (called from cleanup) is the canonical
      // way to stop the stream.
      console.warn('conversation events SSE error:', err)
    },
  })
  return () => controller.abort()
}

export interface UploadedAttachment {
  id: string
  filename: string
  content_type: string
  size_bytes: number
}

export interface UploadChatAttachmentOptions {
  description?: string
  /** Called with the upload progress percentage (0-100) as bytes are sent. */
  onProgress?: (percent: number) => void
  /** Abort the in-flight upload. */
  signal?: AbortSignal
}

export async function uploadChatAttachment(
  orgId: string,
  convId: string,
  file: File,
  options?: UploadChatAttachmentOptions
): Promise<UploadedAttachment> {
  const form = new FormData()
  form.append('file', file)
  const params = options?.description
    ? `?description=${encodeURIComponent(options.description)}`
    : ''
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/chat/conversations/${convId}/attachments${params}`,
    form,
    {
      headers: { 'Content-Type': 'multipart/form-data' },
      signal: options?.signal,
      onUploadProgress: (event) => {
        if (!options?.onProgress) return
        // event.total may be undefined for some browsers/proxies; fall back to file.size.
        const total = event.total ?? file.size
        if (!total) return
        const percent = Math.min(100, Math.round((event.loaded / total) * 100))
        options.onProgress(percent)
      },
    }
  )
  const data = response.data
  return {
    id: data.id,
    filename: data.filename,
    content_type: data.content_type,
    size_bytes: data.size_bytes,
  }
}
