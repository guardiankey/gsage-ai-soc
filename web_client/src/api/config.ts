import { apiClient } from './client'

export interface PublicConfig {
  allow_self_register: boolean
}

export async function getPublicConfig(): Promise<PublicConfig> {
  const response = await apiClient.get<PublicConfig>('/v1/config')
  return response.data
}
