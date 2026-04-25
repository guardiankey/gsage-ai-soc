import { apiClient } from './client'

export type DataStoreVisibility = 'shared' | 'private'

export interface DataStore {
  id: string
  org_id: string
  created_by?: string
  name: string
  description?: string
  schema: Record<string, unknown>
  visibility: DataStoreVisibility
  max_records: number
  record_count: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface DataStoreRecord {
  id: string
  datastore_id: string
  data: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PaginatedDataStores {
  items: DataStore[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface PaginatedRecords {
  items: DataStoreRecord[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export interface DataStoreCreate {
  name: string
  description?: string
  schema?: Record<string, unknown>
  visibility?: DataStoreVisibility
  max_records?: number
}

export interface DataStoreUpdate {
  name?: string
  description?: string
  schema?: Record<string, unknown>
  visibility?: DataStoreVisibility
  max_records?: number
  is_active?: boolean
}

// ── Stores ──────────────────────────────────────────────────────────────────

export async function listStores(
  orgId: string,
  deptId: string,
  page = 1,
  limit = 20
): Promise<PaginatedDataStores> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/depts/${deptId}/datastores`, {
    params: { page, limit },
  })
  return response.data
}

export async function getStore(orgId: string, deptId: string, storeId: string): Promise<DataStore> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}`)
  return response.data
}

export async function createStore(
  orgId: string,
  deptId: string,
  payload: DataStoreCreate
): Promise<DataStore> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/depts/${deptId}/datastores`, payload)
  return response.data
}

export async function updateStore(
  orgId: string,
  deptId: string,
  storeId: string,
  payload: DataStoreUpdate
): Promise<DataStore> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}`, payload)
  return response.data
}

export async function deleteStore(orgId: string, deptId: string, storeId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}`)
}

// ── Records ──────────────────────────────────────────────────────────────────

export async function listRecords(
  orgId: string,
  deptId: string,
  storeId: string,
  page = 1,
  limit = 20
): Promise<PaginatedRecords> {
  const response = await apiClient.get(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records`,
    { params: { page, limit } }
  )
  return response.data
}

export async function getRecord(
  orgId: string,
  deptId: string,
  storeId: string,
  recordId: string
): Promise<DataStoreRecord> {
  const response = await apiClient.get(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records/${recordId}`
  )
  return response.data
}

export async function insertRecord(
  orgId: string,
  deptId: string,
  storeId: string,
  data: Record<string, unknown>
): Promise<DataStoreRecord> {
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records`,
    { data }
  )
  return response.data
}

export async function updateRecord(
  orgId: string,
  deptId: string,
  storeId: string,
  recordId: string,
  data: Record<string, unknown>
): Promise<DataStoreRecord> {
  const response = await apiClient.patch(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records/${recordId}`,
    { data }
  )
  return response.data
}

export async function deleteRecord(
  orgId: string,
  deptId: string,
  storeId: string,
  recordId: string
): Promise<void> {
  await apiClient.delete(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records/${recordId}`
  )
}

export async function queryRecords(
  orgId: string,
  deptId: string,
  storeId: string,
  filters: Record<string, unknown> = {},
  page = 1,
  pageSize = 50
): Promise<PaginatedRecords> {
  const response = await apiClient.post(
    `/v1/orgs/${orgId}/depts/${deptId}/datastores/${storeId}/records/query`,
    { filters, page, page_size: pageSize }
  )
  return response.data
}
