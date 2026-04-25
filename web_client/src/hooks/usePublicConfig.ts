import { useQuery } from '@tanstack/react-query'
import { getPublicConfig, type PublicConfig } from '@/api/config'

const FALLBACK: PublicConfig = { allow_self_register: false }

export interface PublicConfigResult extends PublicConfig {
  isLoading: boolean
}

export function usePublicConfig(): PublicConfigResult {
  const { data, isLoading } = useQuery({
    queryKey: ['public-config'],
    queryFn: getPublicConfig,
    staleTime: Infinity,
    retry: false,
  })
  return { ...(data ?? FALLBACK), isLoading }
}
