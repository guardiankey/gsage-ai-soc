import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useAuth } from '@/contexts/AuthContext'
import { getPendingCount } from '@/api/approvals'

const POLL_INTERVAL_MS = 15_000

export interface UsePendingApprovalsResult {
  pendingCount: number
}

/**
 * Global hook that polls for pending approvals every 15 seconds.
 *
 * - Returns `pendingCount` for rendering a badge in the nav.
 * - Fires a Sonner toast when the count *increases* (new approvals arrived).
 * - Only active when the user has the `approvals:read` permission.
 */
export function usePendingApprovals(): UsePendingApprovalsResult {
  const { orgId, hasPermission, isAuthenticated } = useAuth()
  const { t } = useTranslation()
  const prevCountRef = useRef<number | null>(null)

  const enabled = isAuthenticated && !!orgId && hasPermission('approvals:read')

  const { data: count = 0 } = useQuery({
    queryKey: ['approvals-pending-count', orgId],
    queryFn: () => getPendingCount(orgId!),
    enabled,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  })

  useEffect(() => {
    if (!enabled) return

    const prev = prevCountRef.current

    if (prev !== null && count > prev) {
      toast.info(t('approvals.newPending', { count }), {
        action: {
          label: t('approvals.view'),
          onClick: () => {
            window.location.href = '/approvals'
          },
        },
      })
    }

    prevCountRef.current = count
  }, [count, enabled, t])

  return { pendingCount: count }
}
