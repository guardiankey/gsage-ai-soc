import { useQuery } from '@tanstack/react-query'
import { listTasks, type BackgroundTask } from '@/api/tasks'

/**
 * Conversation-scoped background-tasks watcher (spec F1).
 *
 * ``sessionId`` is the **conversation UUID** (``GSageTenantSession.id``),
 * which is what the endpoint's ``session_id`` filter expects — not the
 * ``agno_session_id`` string.
 *
 * The first fetch is always made when the conversation becomes active
 * (query ``enabled``) — it must never be gated on a local count, which
 * would be circular. After that, continuous polling is armed whenever
 * either the fetched data shows active tasks OR ``forcePoll`` is set
 * (``message_end.has_active_bg_tasks`` from the stream, which fires even
 * when the initial fetch found no tasks yet). An empty authoritative
 * response with ``forcePoll`` off ends the cycle.
 */
export function useConversationBgTasks(
  orgId: string | null,
  sessionId: string | null,
  forcePoll = false
) {
  const { data, ...rest } = useQuery({
    queryKey: ['conversationBgTasks', orgId, sessionId],
    queryFn: () =>
      listTasks(orgId!, 1, 50, undefined, ['queued', 'running'], sessionId!),
    enabled: !!orgId && !!sessionId,
    staleTime: 0,
    refetchInterval: (query) => {
      const tasks = query.state.data?.items
      if (forcePoll) return 5000
      const hasActive = !!tasks?.some(
        (task: BackgroundTask) =>
          task.status === 'queued' || task.status === 'running'
      )
      return hasActive ? 5000 : false
    },
  })

  const tasks = data?.items ?? []
  const count = data?.total ?? tasks.length

  return { data, tasks, count, ...rest }
}
