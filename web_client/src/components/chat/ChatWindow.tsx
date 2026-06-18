import { useRef, useEffect, forwardRef, useImperativeHandle, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Clock } from 'lucide-react'
import { Link } from 'react-router-dom'
import { listMessages, checkMessages, subscribeConversationEvents, type Message, type MessageListResult, type MessageCheck } from '@/api/chat'
import { useAuth } from '@/contexts/AuthContext'
import { MessageBubble } from './MessageBubble'
import { StreamingMessage } from './StreamingMessage'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

interface Props {
  conversationId: string
  streamingContent: string
  isStreaming: boolean
  streamError: string | null
  pendingApprovals: boolean
  hasActiveBgTasks: boolean
  streamEndedAt: number
  onBgTasksResolved?: () => void
  onPendingApprovalsDetected?: (pending: boolean) => void
  pendingUserMessage: string | null
  onFirstMessage?: () => void
}

export interface ChatWindowHandle {
  scrollToBottom: () => void
}

export const ChatWindow = forwardRef<ChatWindowHandle, Props>(function ChatWindow(
  {
    conversationId,
    streamingContent,
    isStreaming,
    streamError,
    pendingApprovals,
    hasActiveBgTasks,
    streamEndedAt,
    onBgTasksResolved,
    onPendingApprovalsDetected,
    pendingUserMessage,
    onFirstMessage,
  },
  ref
) {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()
  const bottomRef = useRef<HTMLDivElement>(null)
  const [prevMsgCount, setPrevMsgCount] = useState<number | null>(null)
  const lastMessageIdRef = useRef<string | null>(null)

  // Reset last-known message id when conversation changes.
  useEffect(() => {
    lastMessageIdRef.current = null
    setPrevMsgCount(null)
  }, [conversationId])

  // ── Lightweight poll: only fetches last_message_id (cheap) ────────────
  // When the id changes the full message list is invalidated below.
  const shouldPoll = pendingApprovals || hasActiveBgTasks
    || (streamEndedAt > 0 && (Date.now() - streamEndedAt) < 60_000)

  const { data: checkData } = useQuery({
    queryKey: ['messagesCheck', orgId, conversationId],
    queryFn: () => checkMessages(orgId!, conversationId),
    enabled: !!orgId && !!conversationId,
    refetchInterval: shouldPoll ? 5000 : false,
    // Never stale while polling — always fetch fresh.
    staleTime: 0,
  })

  // When the last_message_id changes (new message arrived), invalidate
  // the full message list so it refetches immediately.
  // On first load we seed the ref without invalidating.
  useEffect(() => {
    const newId = checkData?.last_message_id ?? null
    if (newId === null) return
    if (lastMessageIdRef.current === null) {
      // First load — seed, don't invalidate.
      lastMessageIdRef.current = newId
      return
    }
    if (newId !== lastMessageIdRef.current) {
      lastMessageIdRef.current = newId
      queryClient.invalidateQueries({
        queryKey: ['messages', orgId, conversationId],
      })
    }
  }, [checkData?.last_message_id, orgId, conversationId, queryClient])

  // ── Full message list — fetched on mount / invalidation only ─────────
  const { data, isLoading } = useQuery({
    queryKey: ['messages', orgId, conversationId],
    queryFn: () => listMessages(orgId!, conversationId),
    enabled: !!orgId && !!conversationId,
    // No refetchInterval — updates are driven by the lightweight check
    // query (above) and SSE events (below).
  })

  const messages = data?.messages

  // Track when this client's own stream was active / just ended so we can
  // suppress the SSE ``messages_updated`` event that the backend emits at
  // the end of OUR OWN message stream (chat.py publishes the event right
  // before closing the stream). Without this guard the subscriber would
  // trigger an extra GET on the messages list redundantly with the
  // invalidation already issued in ChatPage's onDone.
  const isStreamingRef = useRef(isStreaming)
  const streamEndedAtRef = useRef(0)
  useEffect(() => {
    if (isStreamingRef.current && !isStreaming) {
      streamEndedAtRef.current = Date.now()
    }
    isStreamingRef.current = isStreaming
  }, [isStreaming])

  // Subscribe to server-pushed conversation update events (SSE) so we
  // refetch the message list immediately when a background-tool
  // continuation appends a new assistant message — instead of waiting
  // for the 5 s polling cycle.  The 5 s polling remains as a fallback
  // if the SSE connection drops or the network is restrictive.
  //
  // We suppress events for 300 ms after our own stream ended (the
  // backend emits a ``messages_updated`` right before closing the SSE
  // stream; that event arrives within single-digit ms).  300 ms is
  // enough to silence that duplicate while letting through the
  // follow-up event from the Celery background-task continuation.
  useEffect(() => {
    if (!orgId || !conversationId) return
    const stop = subscribeConversationEvents(orgId, conversationId, () => {
      // Skip events that coincide with our own just-finished stream
      // (ChatPage.onDone already invalidates the messages query).
      if (isStreamingRef.current) return
      if (Date.now() - streamEndedAtRef.current < 300) return
      queryClient.invalidateQueries({
        queryKey: ['messages', orgId, conversationId],
      })
    })
    return stop
  }, [orgId, conversationId, queryClient])

  // Propagate server-side pending-approvals flag to parent so the approval
  // banner and input-disable state are restored after navigation.
  useEffect(() => {
    if (data?.hasPendingApprovals) {
      onPendingApprovalsDetected?.(true)
    }
  }, [data?.hasPendingApprovals, onPendingApprovalsDetected])

  // When polling with active bg tasks, detect new messages and stop polling.
  // prevMsgCount starts as null and is seeded on first data load so the
  // first message arrival during active bg tasks triggers resolution.
  useEffect(() => {
    const count = messages?.length ?? 0
    if (prevMsgCount === null) {
      setPrevMsgCount(count)
      return
    }
    if (hasActiveBgTasks && count > prevMsgCount) {
      onBgTasksResolved?.()
    }
    setPrevMsgCount(count)
  }, [messages?.length, hasActiveBgTasks, onBgTasksResolved])

  useImperativeHandle(ref, () => ({
    scrollToBottom: () => {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    },
  }))

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  if (isLoading && !pendingUserMessage && !streamingContent && !pendingApprovals) {
    return (
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className={cn('flex gap-3', i % 2 === 0 ? 'justify-start' : 'justify-end')}
          >
            <Skeleton className={cn('h-16 rounded-2xl', i % 2 === 0 ? 'w-3/4' : 'w-1/2')} />
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto overflow-x-hidden min-w-0">
      {/* HITL approval banner */}
      {pendingApprovals && (
        <div className="sticky top-0 z-10 mx-4 mt-2">
          <div className="flex items-center gap-2 bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-300 dark:border-yellow-700 rounded-lg p-3 text-sm text-yellow-800 dark:text-yellow-200">
            <Clock className="h-4 w-4 shrink-0" />
            <span className="flex-1">{t('chat.approvalRequired')}</span>
            <Link
              to="/approvals"
              className="font-medium underline underline-offset-2 hover:text-yellow-900 dark:hover:text-yellow-100"
            >
              {t('chat.viewApprovals')}
            </Link>
          </div>
        </div>
      )}

      {/* Messages */}
      <div className="p-4 space-y-4">
        {(messages ?? []).length === 0 && !isStreaming && !pendingApprovals && !pendingUserMessage && (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
            <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3">
              <span className="text-2xl">💬</span>
            </div>
            <p className="text-sm">{t('chat.startConversation')}</p>
          </div>
        )}

        {(messages ?? []).map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}

        {/* Optimistic user message — shown immediately while waiting for stream */}
        {pendingUserMessage && (
          <div className="flex justify-end">
            <div className="max-w-[75%] rounded-2xl px-4 py-2.5 text-sm bg-[hsl(var(--primary))] text-white">
              {pendingUserMessage}
            </div>
          </div>
        )}

        {/* Streaming response — also shown while waiting for approval refetch */}
        {(isStreaming || streamingContent) && (
          <StreamingMessage content={streamingContent} isStreaming={isStreaming} />
        )}

        {/* Stream error */}
        {streamError && (
          <div className="flex items-start gap-2 p-3 bg-destructive/10 rounded-lg text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
            {streamError}
          </div>
        )}

        {/* Unexpected end of background-task continuation: last visible
            message is from the user but no active bg tasks remain and the
            agent never produced a follow-up. This signals the continuation
            task crashed silently (e.g. retries exhausted) so the user is
            informed instead of left waiting. */}
        {!isStreaming &&
          !pendingApprovals &&
          !hasActiveBgTasks &&
          !pendingUserMessage &&
          !streamingContent &&
          (messages?.length ?? 0) > 0 &&
          messages![messages!.length - 1].role === 'user' && (
            <div className="flex items-start gap-2 p-3 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg text-sm text-orange-800 dark:text-orange-200">
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
              <span>{t('chat.taskEndedUnexpectedly')}</span>
            </div>
          )}

        <div ref={bottomRef} className="h-1" />
      </div>
    </div>
  )
})
