import { apiClient } from './client'

export type JobType = 'PROMPT_RUN' | 'SYSTEM_TASK'
export type JobStatus = 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILURE' | 'SKIPPED'

export interface ScheduledJob {
  id: string
  org_id: string
  user_id: string
  name: string
  description?: string | null
  job_type: JobType
  cron_expression: string
  timezone: string
  starts_at?: string | null
  ends_at?: string | null
  is_active: boolean
  max_runs?: number | null
  run_count: number
  prompt_content?: string | null
  prompt_conversation_id?: string | null
  prompt_output_format: string
  task_name?: string | null
  task_kwargs?: Record<string, unknown> | null
  last_run_at?: string | null
  last_run_status?: JobStatus | null
  last_run_result?: Record<string, unknown> | null
  redbeat_key?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ScheduledJobCreate {
  name: string
  description?: string
  job_type: JobType
  cron_expression: string
  timezone?: string
  starts_at?: string
  ends_at?: string
  is_active?: boolean
  max_runs?: number
  prompt_content?: string
  prompt_conversation_id?: string
  prompt_output_format?: string
  task_name?: string
  task_kwargs?: Record<string, unknown>
}

export interface ScheduledJobUpdate {
  name?: string
  description?: string
  cron_expression?: string
  timezone?: string
  starts_at?: string | null
  ends_at?: string | null
  is_active?: boolean
  max_runs?: number | null
  prompt_content?: string
  prompt_conversation_id?: string | null
  prompt_output_format?: string
  task_name?: string
  task_kwargs?: Record<string, unknown> | null
}

export interface PaginatedScheduledJobs {
  items: ScheduledJob[]
  total: number
  page: number
  limit: number
  has_more: boolean
}

export async function listScheduledJobs(
  orgId: string,
  page = 1,
  limit = 20,
  jobType?: JobType,
  isActive?: boolean
): Promise<PaginatedScheduledJobs> {
  const params: Record<string, unknown> = { page, limit }
  if (jobType) params.job_type = jobType
  if (isActive !== undefined) params.is_active = isActive
  const response = await apiClient.get(`/v1/orgs/${orgId}/scheduled-jobs`, { params })
  return response.data
}

export async function getScheduledJob(orgId: string, jobId: string): Promise<ScheduledJob> {
  const response = await apiClient.get(`/v1/orgs/${orgId}/scheduled-jobs/${jobId}`)
  return response.data
}

export async function createScheduledJob(
  orgId: string,
  payload: ScheduledJobCreate
): Promise<ScheduledJob> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/scheduled-jobs`, payload)
  return response.data
}

export async function updateScheduledJob(
  orgId: string,
  jobId: string,
  payload: ScheduledJobUpdate
): Promise<ScheduledJob> {
  const response = await apiClient.patch(`/v1/orgs/${orgId}/scheduled-jobs/${jobId}`, payload)
  return response.data
}

export async function deleteScheduledJob(orgId: string, jobId: string): Promise<void> {
  await apiClient.delete(`/v1/orgs/${orgId}/scheduled-jobs/${jobId}`)
}

export async function activateScheduledJob(orgId: string, jobId: string): Promise<ScheduledJob> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/scheduled-jobs/${jobId}/activate`)
  return response.data
}

export async function deactivateScheduledJob(orgId: string, jobId: string): Promise<ScheduledJob> {
  const response = await apiClient.post(`/v1/orgs/${orgId}/scheduled-jobs/${jobId}/deactivate`)
  return response.data
}
