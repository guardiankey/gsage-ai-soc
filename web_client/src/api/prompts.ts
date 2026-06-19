import { apiClient } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PromptCategory {
  id: string
  name: string
  parent_id: string | null
  dept_id: string | null
  description: string | null
  sort_order: number
  is_active: boolean
  children: PromptCategory[]
  prompt_count: number
  created_at: string
  updated_at: string
}

export interface Prompt {
  id: string
  title: string
  description: string | null
  content: string
  scope: 'personal' | 'department' | 'organization'
  category_id: string | null
  category_name: string | null
  created_by: string
  creator_name: string
  is_favorite: boolean
  usage_count: number
  created_at: string
  updated_at: string
}

export interface PromptListResponse {
  prompts: Prompt[]
  total: number
  page: number
  page_size: number
}

export interface PromptCreatePayload {
  title: string
  content: string
  description?: string
  category_id?: string
  scope?: 'personal' | 'department' | 'organization'
}

export interface PromptUpdatePayload {
  title?: string
  content?: string
  description?: string
  category_id?: string
  scope?: 'personal' | 'department' | 'organization'
  is_active?: boolean
}

export interface SearchPayload {
  query?: string
  scope?: 'personal' | 'department' | 'organization'
  category_id?: string
  favorites_only?: boolean
  page?: number
  page_size?: number
}

// ---------------------------------------------------------------------------
// Category API
// ---------------------------------------------------------------------------

export async function listCategories(orgId: string): Promise<PromptCategory[]> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/prompts/categories`)
  return response.data
}

export async function createCategory(
  orgId: string,
  payload: { name: string; parent_id?: string; dept_id?: string | null; description?: string },
): Promise<PromptCategory> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/prompts/categories`, payload)
  return response.data
}

export async function updateCategory(
  orgId: string,
  categoryId: string,
  payload: { name?: string; parent_id?: string; dept_id?: string | null; description?: string; is_active?: boolean },
): Promise<PromptCategory> {
  const response = await apiClient.put(
    `/v1/orgs/${orgId}/prompts/categories/${categoryId}`,
    payload,
  )
  return response.data
}

export async function deleteCategory(orgId: string, categoryId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/prompts/categories/${categoryId}`)
}

// ---------------------------------------------------------------------------
// Prompt API
// ---------------------------------------------------------------------------

export async function listPrompts(
  orgId: string,
  params?: {
    scope?: string
    category_id?: string
    favorites_only?: boolean
    page?: number
    page_size?: number
  },
): Promise<PromptListResponse> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/prompts`, { params })
  return response.data
}

export async function searchPrompts(
  orgId: string,
  payload: SearchPayload,
): Promise<PromptListResponse> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/prompts/search`, payload)
  return response.data
}

export async function getPrompt(orgId: string, promptId: string): Promise<Prompt> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/prompts/${promptId}`)
  return response.data
}

export async function createPrompt(
  orgId: string,
  payload: PromptCreatePayload,
): Promise<Prompt> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/prompts`, payload)
  return response.data
}

export async function updatePrompt(
  orgId: string,
  promptId: string,
  payload: PromptUpdatePayload,
): Promise<Prompt> {
  const response = await apiClient.put(`/v1/orgs/${orgId}/prompts/${promptId}`, payload)
  return response.data
}

export async function deletePrompt(orgId: string, promptId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/prompts/${promptId}`)
}

// ---------------------------------------------------------------------------
// Favorites API
// ---------------------------------------------------------------------------

export async function toggleFavorite(
  orgId: string,
  promptId: string,
): Promise<{ favorited: boolean }> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/prompts/${promptId}/favorite`)
  return response.data
}

export async function listFavorites(
  orgId: string,
  page = 1,
  pageSize = 20,
): Promise<PromptListResponse> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/prompts/favorites`, {
    params: { page, page_size: pageSize },
  })
  return response.data
}
