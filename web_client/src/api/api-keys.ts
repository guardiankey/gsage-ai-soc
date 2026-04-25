import { apiClient } from './client'

export interface ApiKey {
  id: string
  name: string
  prefix: string
  scoped_permissions?: string[]
  environment?: string
  is_active: boolean
  last_used_at?: string
  expires_at?: string
  created_at: string
}

export interface CreateApiKeyResponse {
  key: ApiKey
  raw_key: string
}

export interface PaginatedApiKeys {
  items: ApiKey[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

// Personal API keys (any member)
export async function listMyApiKeys(orgId: string): Promise<ApiKey[]> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/me/api-keys`)
  return response.data
}

export async function createMyApiKey(
  orgId: string,
  name: string
): Promise<CreateApiKeyResponse> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/me/api-keys`, { name })
  return response.data
}

export async function deleteMyApiKey(orgId: string, keyId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/me/api-keys/${keyId}`)
}

// Org-level API keys (admin only)
export async function listOrgApiKeys(orgId: string, page = 1, limit = 20): Promise<PaginatedApiKeys> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/api-keys`, {
    params: { page, limit },
  })
  return response.data
}

export async function createOrgApiKey(
  orgId: string,
  name: string
): Promise<CreateApiKeyResponse> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/api-keys`, { name })
  return response.data
}

export async function deleteOrgApiKey(orgId: string, keyId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/api-keys/${keyId}`)
}
