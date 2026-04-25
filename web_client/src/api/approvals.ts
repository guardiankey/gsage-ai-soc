import { apiClient } from './client'

export type ApprovalStatus = 'pending' | 'approved' | 'rejected'

export interface Approval {
  id: string
  status: ApprovalStatus
  approval_type?: string
  source_type?: string
  pause_type?: string
  tool_name: string
  tool_description?: string
  tool_args?: Record<string, unknown>
  summary?: string
  context?: Record<string, unknown>
  requirements?: string
  resolution_data?: Record<string, unknown>
  agent_id?: string
  user_id?: string
  requested_by_user_id?: string
  requester_user_name?: string
  delegated_to_user_id?: string
  delegated_to_user_name?: string
  comment?: string
  resolved_by?: string
  resolved_by_user_id?: string
  resolved_at?: number
  expires_at?: number
  conversation_id?: string
  session_id?: string
  run_id?: string
  created_at: number
  updated_at: number
}

export interface PaginatedApprovals {
  items: Approval[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface ResolveApprovalResponse {
  approval: Approval
  next_approvals?: string[]
}

export interface ContinueRunResponse {
  content: string
  status: string
  metadata?: Record<string, unknown>
}

export async function listApprovals(
  orgId: string,
  status?: ApprovalStatus,
  page = 1,
  limit = 20
): Promise<PaginatedApprovals> {
  const params: Record<string, unknown> = { page, limit }
  if (status) params.status = status
  const response = await apiClient.get(`/v1/orgs/${orgId}/approvals`, { params })
  return response.data
}

export async function getApproval(orgId: string, approvalId: string): Promise<Approval> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/approvals/${approvalId}`)
  return response.data
}

export async function getPendingCount(orgId: string): Promise<number> {
  const response = await apiClient.get<{ count: number }>(
    `/v1/orgs/${orgId}/approvals/pending-count`
  )
  return response.data.count
}

export async function resolveApproval(
  orgId: string,
  approvalId: string,
  action: 'approve' | 'reject',
  comment?: string
): Promise<ResolveApprovalResponse> {
  const payload: Record<string, string> = { action }
  if (comment) payload.comment = comment
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/approvals/${approvalId}/resolve`,
    payload
  )
  return response.data
}

export async function continueRunFromApproval(
  orgId: string,
  approvalId: string
): Promise<ContinueRunResponse> {
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/approvals/${approvalId}/continue-run`
  )
  return response.data
}
