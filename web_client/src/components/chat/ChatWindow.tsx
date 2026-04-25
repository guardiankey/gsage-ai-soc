import { useRef, useEffect, forwardRef, useImperativeHandle, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Clock } from 'lucide-react'
import { Link } from 'react-router-dom'
import { listMessages, type Message, type MessageListResult } from '@/api/chat'
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
    onBgTasksResolved,
    onPendingApprovalsDetected,
    pendingUserMessage,
    onFirstMessage,
  },
  ref
) {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const bottomRef = useRef<HTMLDivElement>(null)
  const [prevMsgCount, setPrevMsgCount] = useState(0)

  const { data, isLoading } = useQuery({
    queryKey: ['messages', orgId, conversationId],
    queryFn: () => listMessages(orgId!, conversationId),
    enabled: !!orgId && !!conversationId,
    refetchInterval: (query) => {
      const needsPolling = query.state.data?.needsPolling ?? false
      return (pendingApprovals || hasActiveBgTasks || needsPolling) ? 5000 : false
    },
  })

  const messages = data?.messages

  // Propagate server-side pending-approvals flag to parent so the approval
  // banner and input-disable state are restored after navigation.
  useEffect(() => {
    if (data?.hasPendingApprovals) {
      onPendingApprovalsDetected?.(true)
    }
  }, [data?.hasPendingApprovals, onPendingApprovalsDetected])

  // When polling with active bg tasks, detect new messages and stop polling
  useEffect(() => {
    const count = messages?.length ?? 0
    if (hasActiveBgTasks && count > prevMsgCount && prevMsgCount > 0) {
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
    <div className="flex-1 overflow-y-auto">
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

        <div ref={bottomRef} className="h-1" />
      </div>
    </div>
  )
})
