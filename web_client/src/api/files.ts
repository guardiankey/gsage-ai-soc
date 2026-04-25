import { apiClient } from './client'

export interface GeneratedFile {
  id: string
  filename: string
  tool_name?: string
  size_bytes?: number
  mime_type?: string
  is_purged: boolean
  expires_at?: string
  created_at: string
  category?: string
  scope?: string
  description?: string
}

export interface PaginatedFiles {
  items: GeneratedFile[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export async function listFiles(
  orgId: string,
  page = 1,
  limit = 20,
  toolName?: string,
  includeAll = false,
  category?: string
): Promise<PaginatedFiles> {
  const params: Record<string, unknown> = { page, limit }
  if (toolName) params.tool_name = toolName
  if (includeAll) params.include_all = true
  if (category) params.category = category
  const response = await apiClient.get(`/v1/orgs/${orgId}/files`, { params })
  return response.data
}

export async function downloadFile(
  orgId: string,
  fileId: string,
  filename: string
): Promise<void> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/files/${fileId}/download`, {
    responseType: 'blob',
  })
  const url = URL.createObjectURL(response.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * Parse a filename out of a ``Content-Disposition`` header value.
 *
 * Supports both the legacy ``filename="..."`` form and the RFC 5987
 * ``filename*=UTF-8''...`` form (preferred when present).  Returns null
 * if no filename can be extracted.
 */
function parseContentDispositionFilename(header: string | undefined): string | null {
  if (!header) return null
  // Prefer RFC 5987 form: filename*=UTF-8''<percent-encoded>
  const starMatch = header.match(/filename\*\s*=\s*[^']*''([^;]+)/i)
  if (starMatch) {
    try {
      return decodeURIComponent(starMatch[1].trim())
    } catch {
      // Fall through to the plain form below.
    }
  }
  // Plain form: filename="..." or filename=...
  const plainMatch = header.match(/filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)/i)
  if (plainMatch) {
    return (plainMatch[1] ?? plainMatch[2]).trim()
  }
  return null
}

/**
 * Download a file via its API path (e.g. `/v1/orgs/{org}/files/{id}/download`).
 * Used to intercept download links rendered by the LLM inside chat messages.
 *
 * The filename is taken from the response's ``Content-Disposition``
 * header when available; the *fallback* argument is only used if the
 * header is missing or unparseable.
 */
export async function downloadFileByPath(
  path: string,
  fallback: string
): Promise<void> {
  const response = await apiClient.get(path, { responseType: 'blob' })
  const headerName =
    parseContentDispositionFilename(
      response.headers?.['content-disposition'] as string | undefined,
    ) ?? fallback
  const url = URL.createObjectURL(response.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = headerName
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export async function uploadFile(
  orgId: string,
  file: File,
  description?: string,
  scope: 'user' | 'organization' | 'department' = 'user'
): Promise<GeneratedFile> {
  const formData = new FormData()
  formData.append('file', file)
  const params: Record<string, string> = { scope }
  if (description) params.description = description
  const response = await apiClient.post(`/v1/orgs/${orgId}/files/upload`, formData, {
    params,
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return response.data
}

export async function deleteFile(orgId: string, fileId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/files/${fileId}`)
}
