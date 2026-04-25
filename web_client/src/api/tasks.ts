import { apiClient } from './client'

export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface BackgroundTask {
  id: string
  tool_name: string
  status: TaskStatus
  session_id?: string
  conversation_id?: string
  result?: unknown
  error_message?: string
  started_at?: string
  completed_at?: string
  created_at: string
  updated_at: string
}

export interface PaginatedTasks {
  items: BackgroundTask[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export async function listTasks(
  orgId: string,
  page = 1,
  limit = 20,
  toolName?: string,
  status?: TaskStatus,
  sessionId?: string
): Promise<PaginatedTasks> {
  const params: Record<string, unknown> = { page, limit }
  if (toolName) params.tool_name = toolName
  if (status) params.status = status
  if (sessionId) params.session_id = sessionId
  const response = await apiClient.get(`/v1/orgs/${orgId}/background-tasks`, { params })
  return response.data
}

export async function getTask(orgId: string, taskId: string): Promise<BackgroundTask> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/background-tasks/${taskId}`)
  return response.data
}
