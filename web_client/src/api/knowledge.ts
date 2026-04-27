import { apiClient, uploadRequest } from './client'

export interface KnowledgeDocument {
  id: string
  name: string
  description?: string
  type: string
  status: string
  scope: 'org' | 'user' | 'dept'
  chunk_count?: number
  created_at: string
  updated_at: string
}

export interface PaginatedKnowledge {
  items: KnowledgeDocument[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface PaginatedIngestJobs {
  items: IngestJob[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface KnowledgeSearchResult {
  id: string
  name: string
  content: string
  score: number
  metadata?: Record<string, unknown>
}

export interface KnowledgeSearchResponse {
  results: KnowledgeSearchResult[]
  total: number
  query: string
}

export interface IngestJob {
  job_id: string
  filename: string
  status: 'queued' | 'processing' | 'completed' | 'failed'
  scope: 'org' | 'user' | 'dept'
  chunks_stored?: number
  error?: string
  error_message?: string
  storage_key?: string | null
  created_at: string
  updated_at: string
}

export async function searchKnowledge(
  orgId: string,
  query: string,
  maxResults = 8
): Promise<KnowledgeSearchResponse> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/knowledge/search`, {
    query,
    max_results: maxResults,
  })
  return response.data
}

export async function listKnowledge(
  orgId: string,
  page = 1,
  limit = 20
): Promise<PaginatedKnowledge> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/knowledge/content`, {
    params: { page, limit },
  })
  return response.data
}

export async function addKnowledge(
  orgId: string,
  name: string,
  content?: string,
  description?: string,
  url?: string
): Promise<KnowledgeDocument> {
  const payload: Record<string, string> = { name }
  if (content) payload.content = content
  if (description) payload.description = description
  if (url) payload.url = url
  const response = await apiClient.post(`/v1/orgs/${orgId}/knowledge/content`, payload)
  return response.data
}

export async function deleteKnowledge(orgId: string, contentId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/knowledge/content/${contentId}`)
}

export async function ingestDocument(
  orgId: string,
  file: File,
  scope: 'org' | 'user' | 'dept' = 'org'
): Promise<IngestJob> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('scope', scope)
  const response = await uploadRequest(`/v1/orgs/${orgId}/knowledge/ingest`, formData)
  return response.data
}

export async function ingestUrl(
  orgId: string,
  name: string,
  url: string,
  scope: 'org' | 'user' | 'dept' = 'org',
  description?: string
): Promise<IngestJob> {
  const payload: Record<string, string> = { name, url, scope }
  if (description) payload.description = description
  const response = await apiClient.post(`/v1/orgs/${orgId}/knowledge/ingest/url`, payload)
  return response.data
}

export async function getIngestStatus(orgId: string, jobId: string): Promise<IngestJob> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/knowledge/ingest/${jobId}`)
  return response.data
}

export async function listIngestJobs(
  orgId: string,
  page = 1,
  limit = 20
): Promise<PaginatedIngestJobs> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/knowledge/ingest`, {
    params: { page, limit },
  })
  return response.data
}

export async function downloadIngestOriginal(orgId: string, jobId: string, filename: string): Promise<void> {
  const response = await apiClient.get(
    `/v1/orgs/${orgId}/knowledge/ingest/${jobId}/download`,
    { responseType: 'blob' }
  )
  const url = URL.createObjectURL(response.data as Blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}
