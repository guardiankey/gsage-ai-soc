import { apiClient } from './client'

export interface ApprovalRule {
  id: string
  org_id_pattern: string
  dept_id_pattern: string
  user_id_pattern: string
  tool_pattern: string
  approver_user_id: string
  is_active: boolean
  priority: number
  description?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ApprovalRuleCreate {
  tool_pattern: string
  user_id_pattern?: string
  dept_id_pattern?: string
  approver_user_id: string
  is_active?: boolean
  priority?: number
  description?: string
}

export interface ApprovalRuleUpdate {
  tool_pattern?: string
  user_id_pattern?: string
  dept_id_pattern?: string | null
  approver_user_id?: string
  is_active?: boolean
  priority?: number
  description?: string | null
}

export interface PaginatedApprovalRules {
  items: ApprovalRule[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface OrgMember {
  user_id: string
  full_name: string
  email: string
  role: string
}

export async function listOrgMembers(orgId: string): Promise<OrgMember[]> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/members`)
  return response.data
}

export async function listApprovalRules(
  orgId: string,
  page = 1,
  limit = 20,
  isActive?: boolean,
  toolPattern?: string
): Promise<PaginatedApprovalRules> {
  const params: Record<string, unknown> = { page, limit }
  if (isActive !== undefined) params.is_active = isActive
  if (toolPattern) params.tool_pattern = toolPattern
  const response = await apiClient.get(`/v1/orgs/${orgId}/approval-rules`, { params })
  return response.data
}

export async function getApprovalRule(orgId: string, ruleId: string): Promise<ApprovalRule> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/approval-rules/${ruleId}`)
  return response.data
}

export async function createApprovalRule(
  orgId: string,
  payload: ApprovalRuleCreate
): Promise<ApprovalRule> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/approval-rules`, payload)
  return response.data
}

export async function updateApprovalRule(
  orgId: string,
  ruleId: string,
  payload: ApprovalRuleUpdate
): Promise<ApprovalRule> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/approval-rules/${ruleId}`, payload)
  return response.data
}

export async function deleteApprovalRule(orgId: string, ruleId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/approval-rules/${ruleId}`)
}

export async function activateApprovalRule(orgId: string, ruleId: string): Promise<ApprovalRule> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/approval-rules/${ruleId}/activate`)
  return response.data
}

export async function deactivateApprovalRule(orgId: string, ruleId: string): Promise<ApprovalRule> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/approval-rules/${ruleId}/deactivate`)
  return response.data
}
