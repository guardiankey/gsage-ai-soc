import { apiClient } from './client'

export type CredentialKind = 'basic' | 'token' | 'api_key' | 'oauth2' | 'custom'

export interface ToolLink {
  id: string
  credential_id: string
  tool_name: string
  is_active: boolean
  created_at: string
}

export interface ToolLinkIn {
  tool_name: string
  is_active?: boolean
}

export interface Credential {
  id: string
  user_id: string
  org_id: string
  label: string
  kind: CredentialKind
  username?: string | null
  domain?: string | null
  has_username: boolean
  has_password: boolean
  has_domain: boolean
  has_token: boolean
  has_refresh_token: boolean
  has_extra_fields: boolean
  extra_fields_keys: string[]
  token_expires_at?: string | null
  last_used_at?: string | null
  created_at: string
  updated_at: string
  tool_links: ToolLink[]
}

export interface CredentialIn {
  label: string
  kind: CredentialKind
  username?: string | null
  password?: string | null
  domain?: string | null
  token?: string | null
  refresh_token?: string | null
  extra_fields?: Record<string, string> | null
  token_expires_at?: string | null
  tool_links?: ToolLinkIn[]
}

export interface CredentialUpdate {
  label?: string
  kind?: CredentialKind
  username?: string | null
  password?: string | null
  domain?: string | null
  token?: string | null
  refresh_token?: string | null
  extra_fields?: Record<string, string> | null
  token_expires_at?: string | null
}

export interface AvailableTool {
  name: string
  summary: string
  category: string
  credential_schema?: Record<string, unknown> | null
}

const base = (orgId: string) => `/v1/orgs/${orgId}/me/credentials`

export async function listMyCredentials(orgId: string): Promise<Credential[]> {
  const r = await apiClient.get(base(orgId))
  return r.data
}

export async function createMyCredential(
  orgId: string,
  payload: CredentialIn,
): Promise<Credential> {
  const r = await apiClient.post(base(orgId), payload)
  return r.data
}

export async function updateMyCredential(
  orgId: string,
  credId: string,
  payload: CredentialUpdate,
): Promise<Credential> {
  const r = await apiClient.put(`${base(orgId)}/${credId}`, payload)
  return r.data
}

export async function deleteMyCredential(orgId: string, credId: string): Promise<void> {
  await apiClient.delete(`${base(orgId)}/${credId}`)
}

export async function linkCredentialToTool(
  orgId: string,
  credId: string,
  payload: ToolLinkIn,
): Promise<ToolLink> {
  const r = await apiClient.post(`${base(orgId)}/${credId}/links`, payload)
  return r.data
}

export async function unlinkCredentialFromTool(
  orgId: string,
  credId: string,
  linkId: string,
): Promise<void> {
  await apiClient.delete(`${base(orgId)}/${credId}/links/${linkId}`)
}

export async function activateCredentialLink(
  orgId: string,
  credId: string,
  linkId: string,
): Promise<ToolLink> {
  const r = await apiClient.post(`${base(orgId)}/${credId}/links/${linkId}/activate`)
  return r.data
}

export async function listAvailableCredentialTools(orgId: string): Promise<AvailableTool[]> {
  const r = await apiClient.get(`${base(orgId)}/available-tools`)
  return r.data
}
