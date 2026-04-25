import { apiClient } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface OrgAdminOut {
  id: string
  name: string
  slug: string
  is_active: boolean
  system_prompt: string | null
  default_maker_model: string
  default_reviewer_model: string
  agent_timeout_seconds: number
  max_context_tokens: number
  llm_provider: string
  llm_api_key_set: boolean
  auth_providers: string[]
  created_at: string
  updated_at: string
}

export interface OrgAdminUpdate {
  name?: string
  slug?: string
  is_active?: boolean
  system_prompt?: string | null
  default_maker_model?: string
  default_reviewer_model?: string
  agent_timeout_seconds?: number
  max_context_tokens?: number
  llm_provider?: string
  llm_api_key?: string | null
}

// ---------- Permissions ----------
export interface PermissionOut {
  id: string
  tag: string
  description: string | null
  category: string
}

export interface GroupPermissionOut {
  id: string
  tag: string
  description: string | null
  category: string
  dept_id: string | null
}

export interface GroupPermissionEntry {
  permission_id: string
  dept_id: string | null
}

// ---------- Groups ----------
export interface GroupOut {
  id: string
  org_id: string
  name: string
  description: string | null
  member_count: number
  permission_tags: string[]
  created_at: string
  updated_at: string
}

export interface GroupMemberOut {
  user_id: string
  email: string
  full_name: string
}

export interface GroupDetail extends GroupOut {
  members: GroupMemberOut[]
  permissions: GroupPermissionOut[]
}

export interface GroupCreate {
  name: string
  description?: string
}

export interface GroupUpdate {
  name?: string
  description?: string
}

// ---------- Departments ----------
export interface DepartmentOut {
  id: string
  org_id: string
  name: string
  slug: string
  description: string | null
  is_active: boolean
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface DepartmentCreate {
  name: string
  slug?: string
  description?: string | null
}

export interface DepartmentUpdate {
  name?: string
  slug?: string
  description?: string | null
  is_active?: boolean
}

export interface DeptMemberOut {
  id: string
  user_id: string
  dept_id: string
  role: string
  is_active: boolean
  created_at: string
  user_email: string | null
  user_full_name: string | null
}

export interface DeptMemberAdd {
  user_id: string
  role?: string
}

export interface DeptMemberUpdate {
  role: string
  is_active?: boolean
}

// ---------- Users ----------
export interface AdminUserOut {
  id: string
  email: string
  full_name: string
  is_active: boolean
  auth_provider: string
  otp_enabled: boolean
  role_in_org: string
  group_ids: string[]
  dept_ids: string[]
  telegram_id: string | null
  secondary_emails: string | null
  ai_instructions: string | null
  created_at: string
  updated_at: string
}

export interface AdminUserCreate {
  email: string
  full_name: string
  password: string
  role?: string
}

export interface AdminUserUpdate {
  full_name?: string
  is_active?: boolean
  role?: string
  telegram_id?: string | null
  secondary_emails?: string | null
  ai_instructions?: string | null
  otp_enabled?: boolean
}

export interface ResetPasswordResponse {
  temporary_password: string
  message: string
}

// ---------- ToolConfigs ----------
export interface ToolConfigOut {
  id: string
  org_id: string
  dept_id: string | null
  tool_name: string
  profile_id: string
  description: string | null
  config: Record<string, unknown>
  updated_by_user_id: string | null
  created_at: string
  updated_at: string
}

export interface ToolConfigCreate {
  dept_id?: string | null
  tool_name: string
  profile_id?: string
  description?: string | null
  config?: Record<string, unknown>
}

export interface ToolConfigUpdate {
  tool_name?: string
  profile_id?: string
  dept_id?: string | null
  description?: string | null
  config?: Record<string, unknown>
}

// ---------- Interface Profiles ----------
export interface InterfaceProfileOut {
  id: string
  org_id: string
  dept_id: string | null
  interface: string
  user_id: string | null
  is_active: boolean
  description: string | null
  system_prompt: string | null
  mode: string
  tool_permissions: string[]
  interface_config: Record<string, unknown> | null
  preferences: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface InterfaceProfileCreate {
  dept_id?: string | null
  interface: string
  user_id?: string | null
  is_active?: boolean
  description?: string | null
  system_prompt?: string | null
  mode?: string
  tool_permissions?: string[]
  interface_config?: Record<string, unknown> | null
  preferences?: Record<string, unknown> | null
}

export interface InterfaceProfileUpdate {
  dept_id?: string | null
  user_id?: string | null
  is_active?: boolean
  description?: string | null
  system_prompt?: string | null
  mode?: string
  tool_permissions?: string[]
  interface_config?: Record<string, unknown> | null
  preferences?: Record<string, unknown> | null
}

// ---------- Email Accounts ----------
export interface EmailAccountOut {
  id: string
  org_id: string
  dept_id: string | null
  display_name: string
  email: string
  is_active: boolean
  imap_host: string
  imap_port: number
  imap_use_tls: boolean
  imap_verify_ssl: boolean
  imap_username: string
  imap_password_set: boolean
  imap_folder: string
  imap_idle_supported: boolean
  smtp_host: string
  smtp_port: number
  smtp_use_tls: boolean
  smtp_verify_ssl: boolean
  smtp_username: string
  smtp_password_set: boolean
  sender_name: string
  subject_prefix: string | null
  reply_footer: string | null
  unknown_sender_folder: string
  max_email_size_bytes: number
  polling_interval_seconds: number
  created_at: string
  updated_at: string
}

export interface EmailAccountCreate {
  dept_id?: string | null
  display_name: string
  email: string
  is_active?: boolean
  imap_host: string
  imap_port?: number
  imap_use_tls?: boolean
  imap_verify_ssl?: boolean
  imap_username: string
  imap_password: string
  imap_folder?: string
  imap_idle_supported?: boolean
  smtp_host: string
  smtp_port?: number
  smtp_use_tls?: boolean
  smtp_verify_ssl?: boolean
  smtp_username?: string
  smtp_password?: string | null
  sender_name?: string
  subject_prefix?: string | null
  reply_footer?: string | null
  unknown_sender_folder?: string
  max_email_size_bytes?: number
  polling_interval_seconds?: number
}

export interface EmailAccountUpdate {
  dept_id?: string | null
  display_name?: string
  is_active?: boolean
  imap_host?: string
  imap_port?: number
  imap_use_tls?: boolean
  imap_verify_ssl?: boolean
  imap_username?: string
  imap_password?: string | null
  imap_folder?: string
  imap_idle_supported?: boolean
  smtp_host?: string
  smtp_port?: number
  smtp_use_tls?: boolean
  smtp_verify_ssl?: boolean
  smtp_username?: string
  smtp_password?: string | null
  sender_name?: string
  subject_prefix?: string | null
  reply_footer?: string | null
  unknown_sender_folder?: string
  max_email_size_bytes?: number
  polling_interval_seconds?: number
}

export interface EmailConnectionTestResult {
  imap_ok: boolean
  smtp_ok: boolean
  imap_error: string | null
  smtp_error: string | null
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

function base(orgId: string): string {
  return `/v1/orgs/${orgId}/admin`
}

// ---- Departments ----
export async function listDepartments(
  orgId: string,
  params?: { include_inactive?: boolean },
): Promise<DepartmentOut[]> {
  const { data } = await apiClient.get<DepartmentOut[]>(`/v1/orgs/${orgId}/depts/`, { params })
  return data
}

export async function createDepartment(
  orgId: string,
  payload: DepartmentCreate,
): Promise<DepartmentOut> {
  const { data } = await apiClient.post<DepartmentOut>(`/v1/orgs/${orgId}/depts/`, payload)
  return data
}

export async function getDepartment(orgId: string, deptId: string): Promise<DepartmentOut> {
  const { data } = await apiClient.get<DepartmentOut>(`/v1/orgs/${orgId}/depts/${deptId}`)
  return data
}

export async function updateDepartment(
  orgId: string,
  deptId: string,
  payload: DepartmentUpdate,
): Promise<DepartmentOut> {
  const { data } = await apiClient.patch<DepartmentOut>(
    `/v1/orgs/${orgId}/depts/${deptId}`,
    payload,
  )
  return data
}

export async function deleteDepartment(orgId: string, deptId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/depts/${deptId}`)
}

export async function listDeptMembers(
  orgId: string,
  deptId: string,
  params?: { include_inactive?: boolean },
): Promise<DeptMemberOut[]> {
  const { data } = await apiClient.get<DeptMemberOut[]>(
    `/v1/orgs/${orgId}/depts/${deptId}/members`,
    { params },
  )
  return data
}

export async function addDeptMember(
  orgId: string,
  deptId: string,
  payload: DeptMemberAdd,
): Promise<DeptMemberOut> {
  const { data } = await apiClient.post<DeptMemberOut>(
    `/v1/orgs/${orgId}/depts/${deptId}/members`,
    payload,
  )
  return data
}

export async function updateDeptMember(
  orgId: string,
  deptId: string,
  userId: string,
  payload: DeptMemberUpdate,
): Promise<DeptMemberOut> {
  const { data } = await apiClient.patch<DeptMemberOut>(
    `/v1/orgs/${orgId}/depts/${deptId}/members/${userId}`,
    payload,
  )
  return data
}

export async function removeDeptMember(
  orgId: string,
  deptId: string,
  userId: string,
): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/depts/${deptId}/members/${userId}`)
}

// ---- Organization ----
export async function getAdminOrg(orgId: string): Promise<OrgAdminOut> {
  const { data } = await apiClient.get<OrgAdminOut>(`${base(orgId)}/organization`)
  return data
}

export async function updateAdminOrg(orgId: string, payload: OrgAdminUpdate): Promise<OrgAdminOut> {
  const { data } = await apiClient.patch<OrgAdminOut>(`${base(orgId)}/organization`, payload)
  return data
}

// ---- Permissions ----
export async function listPermissions(orgId: string): Promise<PermissionOut[]> {
  const { data } = await apiClient.get<PermissionOut[]>(`${base(orgId)}/permissions`)
  return data
}

// ---- Groups ----
export async function listGroups(orgId: string): Promise<GroupOut[]> {
  const { data } = await apiClient.get<GroupOut[]>(`${base(orgId)}/groups`)
  return data
}

export async function createGroup(orgId: string, payload: GroupCreate): Promise<GroupOut> {
  const { data } = await apiClient.post<GroupOut>(`${base(orgId)}/groups`, payload)
  return data
}

export async function getGroup(orgId: string, groupId: string): Promise<GroupDetail> {
  const { data } = await apiClient.get<GroupDetail>(`${base(orgId)}/groups/${groupId}`)
  return data
}

export async function updateGroup(orgId: string, groupId: string, payload: GroupUpdate): Promise<GroupOut> {
  const { data } = await apiClient.patch<GroupOut>(`${base(orgId)}/groups/${groupId}`, payload)
  return data
}

export async function deleteGroup(orgId: string, groupId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/groups/${groupId}`)
}

export async function updateGroupMembers(orgId: string, groupId: string, userIds: string[]): Promise<GroupDetail> {
  const { data } = await apiClient.put<GroupDetail>(`${base(orgId)}/groups/${groupId}/members`, { user_ids: userIds })
  return data
}

export async function updateGroupPermissions(orgId: string, groupId: string, permissions: GroupPermissionEntry[]): Promise<GroupDetail> {
  const { data } = await apiClient.put<GroupDetail>(`${base(orgId)}/groups/${groupId}/permissions`, { permissions })
  return data
}

// ---- Users ----
export async function listAdminUsers(
  orgId: string,
  params?: { page?: number; limit?: number; search?: string },
): Promise<{ items: AdminUserOut[]; total: number; page: number; limit: number; has_more: boolean }> {
  const { data } = await apiClient.get(`${base(orgId)}/users`, { params })
  return data
}

export async function createAdminUser(orgId: string, payload: AdminUserCreate): Promise<AdminUserOut> {
  const { data } = await apiClient.post<AdminUserOut>(`${base(orgId)}/users`, payload)
  return data
}

export async function getAdminUser(orgId: string, userId: string): Promise<AdminUserOut> {
  const { data } = await apiClient.get<AdminUserOut>(`${base(orgId)}/users/${userId}`)
  return data
}

export async function updateAdminUser(orgId: string, userId: string, payload: AdminUserUpdate): Promise<AdminUserOut> {
  const { data } = await apiClient.patch<AdminUserOut>(`${base(orgId)}/users/${userId}`, payload)
  return data
}

export async function deleteAdminUser(orgId: string, userId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/users/${userId}`)
}

export async function resetUserPassword(orgId: string, userId: string): Promise<ResetPasswordResponse> {
  const { data } = await apiClient.post<ResetPasswordResponse>(`${base(orgId)}/users/${userId}/reset-password`)
  return data
}

export async function resetUserOtp(orgId: string, userId: string): Promise<void> {
  await apiClient.post(`${base(orgId)}/users/${userId}/reset-otp`)
}

export async function updateUserGroups(orgId: string, userId: string, groupIds: string[]): Promise<AdminUserOut> {
  const { data } = await apiClient.put<AdminUserOut>(`${base(orgId)}/users/${userId}/groups`, { group_ids: groupIds })
  return data
}

// ---- ToolConfigs ----
export interface AvailableTool {
  name: string
  display_name: string
  category: string
}

export async function listAvailableTools(orgId: string): Promise<AvailableTool[]> {
  const { data } = await apiClient.get<AvailableTool[]>(`${base(orgId)}/tools`)
  return data
}

export async function listToolConfigs(orgId: string, params?: { tool_name?: string; dept_id?: string }): Promise<ToolConfigOut[]> {
  const { data } = await apiClient.get<ToolConfigOut[]>(`${base(orgId)}/tool-configs`, { params })
  return data
}

export async function createToolConfig(orgId: string, payload: ToolConfigCreate): Promise<ToolConfigOut> {
  const { data } = await apiClient.post<ToolConfigOut>(`${base(orgId)}/tool-configs`, payload)
  return data
}

export async function getToolConfig(orgId: string, configId: string): Promise<ToolConfigOut> {
  const { data } = await apiClient.get<ToolConfigOut>(`${base(orgId)}/tool-configs/${configId}`)
  return data
}

export async function updateToolConfig(orgId: string, configId: string, payload: ToolConfigUpdate): Promise<ToolConfigOut> {
  const { data } = await apiClient.patch<ToolConfigOut>(`${base(orgId)}/tool-configs/${configId}`, payload)
  return data
}

export async function deleteToolConfig(orgId: string, configId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/tool-configs/${configId}`)
}

// ---- Interface Profiles ----
export async function listInterfaceProfiles(orgId: string, params?: { interface?: string; dept_id?: string }): Promise<InterfaceProfileOut[]> {
  const { data } = await apiClient.get<InterfaceProfileOut[]>(`${base(orgId)}/interface-profiles`, { params })
  return data
}

export async function createInterfaceProfile(orgId: string, payload: InterfaceProfileCreate): Promise<InterfaceProfileOut> {
  const { data } = await apiClient.post<InterfaceProfileOut>(`${base(orgId)}/interface-profiles`, payload)
  return data
}

export async function getInterfaceProfile(orgId: string, profileId: string): Promise<InterfaceProfileOut> {
  const { data } = await apiClient.get<InterfaceProfileOut>(`${base(orgId)}/interface-profiles/${profileId}`)
  return data
}

export async function updateInterfaceProfile(orgId: string, profileId: string, payload: InterfaceProfileUpdate): Promise<InterfaceProfileOut> {
  const { data } = await apiClient.patch<InterfaceProfileOut>(`${base(orgId)}/interface-profiles/${profileId}`, payload)
  return data
}

export async function deleteInterfaceProfile(orgId: string, profileId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/interface-profiles/${profileId}`)
}

// ---- Email Accounts ----
export async function listEmailAccounts(orgId: string): Promise<EmailAccountOut[]> {
  const { data } = await apiClient.get<EmailAccountOut[]>(`${base(orgId)}/email-accounts`)
  return data
}

export async function createEmailAccount(orgId: string, payload: EmailAccountCreate): Promise<EmailAccountOut> {
  const { data } = await apiClient.post<EmailAccountOut>(`${base(orgId)}/email-accounts`, payload)
  return data
}

export async function getEmailAccount(orgId: string, accountId: string): Promise<EmailAccountOut> {
  const { data } = await apiClient.get<EmailAccountOut>(`${base(orgId)}/email-accounts/${accountId}`)
  return data
}

export async function updateEmailAccount(orgId: string, accountId: string, payload: EmailAccountUpdate): Promise<EmailAccountOut> {
  const { data } = await apiClient.patch<EmailAccountOut>(`${base(orgId)}/email-accounts/${accountId}`, payload)
  return data
}

export async function deleteEmailAccount(orgId: string, accountId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/email-accounts/${accountId}`)
}

export async function testEmailAccount(orgId: string, accountId: string): Promise<EmailConnectionTestResult> {
  const { data } = await apiClient.post<EmailConnectionTestResult>(`${base(orgId)}/email-accounts/${accountId}/test`)
  return data
}
