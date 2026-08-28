import { useRef, useState, useCallback, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Menu } from 'lucide-react'
import { createConversation, streamMessage, uploadChatAttachment, subscribeConversationEvents, type SendMessageResponse } from '@/api/chat'
import { useAuth } from '@/contexts/AuthContext'
import { ConversationList } from '@/components/chat/ConversationList'
import { ChatWindow, type ChatWindowHandle } from '@/components/chat/ChatWindow'
import { ChatInput } from '@/components/chat/ChatInput'
import { BackgroundTasksIndicator } from '@/components/chat/BackgroundTasksIndicator'
import { useConversationBgTasks } from '@/hooks/useConversationBgTasks'
import { Button } from '@/components/ui/button'
import { InteractionRenderer } from '@/components/interaction/InteractionRenderer'
import { useInteraction } from '@/hooks/useInteraction'
import type { InteractionEvent } from '@/hooks/useInteraction'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'

export default function ChatPage() {
  const { t } = useTranslation()
  const { orgId, hasPermission } = useAuth()
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const chatWindowRef = useRef<ChatWindowHandle>(null)

  const [streamingContent, setStreamingContent] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [pendingApprovals, setPendingApprovals] = useState(false)
  const [hasActiveBgTasks, setHasActiveBgTasks] = useState(false)
  const [streamEndedAt, setStreamEndedAt] = useState(0)
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  // The conversation the active stream belongs to. Every stream callback is
  // guarded against this ref so a zombie stream from a previous conversation
  // can never write into the current one (spec B2).
  const currentConversationIdRef = useRef<string | null>(null)
  // Tracks whether onPaused fired in the current stream (avoids stale closure reads).
  const pausedRef = useRef(false)
  // Tracks whether we just created a new conversation (avoids resetting pendingUserMessage on nav).
  const justCreatedRef = useRef(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // ── Interaction Service ─────────────────────────────────────────────
  const interaction = useInteraction(orgId)
  const [interactionLoading, setInteractionLoading] = useState(false)

  // Subscribe to interaction.requested SSE events for the current conversation
  useEffect(() => {
    if (!orgId || !conversationId) return
    const stop = subscribeConversationEvents(
      orgId,
      conversationId,
      () => {}, // messages_updated — already handled by ChatWindow
      (event) => {
        interaction.handleEvent(event as unknown as InteractionEvent)
      }
    )
    return stop
    // NOTE: depend on the stable ``handleEvent`` callback only — the
    // ``useInteraction`` hook returns a NEW object on every render, and
    // depending on the whole object would abort/re-subscribe the SSE
    // connection on every re-render (e.g. every streamed token), flooding
    // the network log with canceled /events requests.
  }, [orgId, conversationId, interaction.handleEvent])

  const handleInteractionSubmit = useCallback(
    async (interactionId: string, responses: Record<string, unknown>) => {
      setInteractionLoading(true)
      try {
        await interaction.submit(interactionId, responses)
      } catch {
        toast.error(t('interaction.submitError'))
      } finally {
        setInteractionLoading(false)
      }
    },
    [interaction, t]
  )

  const handleInteractionCancel = useCallback(
    async (interactionId: string) => {
      await interaction.cancel(interactionId)
    },
    [interaction]
  )

  // Reset streaming state when the conversation changes.
  //
  // Aborting the previous stream is mandatory: without it, the old stream's
  // callbacks keep firing into the new conversation (spec B2). The guard on
  // currentConversationIdRef below additionally drops any callbacks that were
  // already queued in the JS task queue before the abort took effect.
  useEffect(() => {
    abortControllerRef.current?.abort()
    abortControllerRef.current = null
    currentConversationIdRef.current = conversationId ?? null
    if (justCreatedRef.current) {
      // Don't reset the streaming placeholder — handleSend has just seeded
      // state for the newly created conversation and is actively streaming
      // into it.
      justCreatedRef.current = false
      setSidebarOpen(false)
      return
    }
    setStreamingContent('')
    setIsStreaming(false)
    setStreamError(null)
    setPendingApprovals(false)
    setHasActiveBgTasks(false)
    setStreamEndedAt(0)
    setPendingUserMessage(null)
    pausedRef.current = false
    setSidebarOpen(false)
  }, [conversationId])

  // Abort the in-flight stream when leaving the chat page entirely. The
  // agent run continues server-side; this client only stops listening.
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort()
    }
  }, [])

  const handleSend = useCallback(
    async (message: string, attachmentIds?: string[]) => {
      if (!orgId) return

      setStreamError(null)
      setPendingApprovals(false)
      setHasActiveBgTasks(false)

      let targetConvId = conversationId
      // Create conversation if none selected
      if (!targetConvId) {
        try {
          const conv = await createConversation(orgId, message.slice(0, 60))
          targetConvId = conv.id
          justCreatedRef.current = true
          navigate(`/chat/${conv.id}`, { replace: true })
          // Re-point the stream guard to the new conversation immediately so
          // callbacks arriving before the next effect pass are accepted.
          currentConversationIdRef.current = targetConvId
          queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
        } catch {
          toast.error(t('chat.createConvError'))
          return
        }
      }

      setIsStreaming(true)
      setStreamingContent('')
      setPendingUserMessage(message)

      let accumulated = ''

      const callbacks = {
        onDelta: (text: string) => {
          // Drop late events from a stream that belongs to a previous
          // conversation (spec B2 — the abort may not have flushed
          // callbacks already queued in the JS task queue).
          if (targetConvId !== currentConversationIdRef.current) return
          accumulated += text
          setStreamingContent(accumulated)
        },
        onPaused: (_data: { pending_approvals: string[]; run_id?: string }) => {
          if (targetConvId !== currentConversationIdRef.current) return
          // Agent paused for HITL approval — stop the streaming cursor.
          // Re-set pendingUserMessage here to survive the conversationId-change
          // reset effect (which may have fired when navigate() created a new conv).
          pausedRef.current = true
          setIsStreaming(false)
          setStreamEndedAt(Date.now())
          setPendingApprovals(true)
          setPendingUserMessage(message)
        },
        onDone: (metadata: SendMessageResponse['metadata'], status?: 'error' | 'paused' | null) => {
          if (targetConvId !== currentConversationIdRef.current) return
          setIsStreaming(false)
          setStreamEndedAt(Date.now())
          setHasActiveBgTasks(!!metadata?.has_active_bg_tasks)
          const wasPaused = pausedRef.current
          pausedRef.current = false

          if (wasPaused) {
            // Approval is pending — do NOT clear streamingContent or
            // pendingUserMessage. The backend may not have committed the
            // assistant message to the DB yet, so the refetch would return
            // without it and the message would disappear from the UI.
            // The state resets when conversationId changes (navigation).
            queryClient.invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
            queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
            chatWindowRef.current?.scrollToBottom()
          } else if (status === 'error') {
            // Run errored. The backend has already persisted an error-status
            // run; the next refetch will surface it as an error-badged message.
            // Wait for that refetch to complete before clearing the streaming
            // buffer, otherwise the message visibly "disappears" between the
            // SSE end and the DB read.
            queryClient
              .invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
              .then(() => {
                // Defer one animation frame so the messages useQuery has
                // time to re-render with the freshly refetched data before
                // we remove the streaming placeholder. Without this rAF the
                // placeholder is cleared in a render that runs before the
                // QueryObserver notifies subscribers, producing a brief
                // visual gap ("flicker") at the end of the stream.
                requestAnimationFrame(() => {
                  setStreamingContent('')
                  accumulated = ''
                  setPendingUserMessage(null)
                })
              })
            queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
            chatWindowRef.current?.scrollToBottom()
          } else {
            queryClient
              .invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
              .then(() => {
                // See comment above — defer to next frame to avoid the
                // end-of-stream flicker.
                requestAnimationFrame(() => {
                  setStreamingContent('')
                  accumulated = ''
                  setPendingUserMessage(null)
                })
              })
            queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
            chatWindowRef.current?.scrollToBottom()
          }
        },
        onError: (err: string) => {
          if (targetConvId !== currentConversationIdRef.current) return
          // Do NOT clear streamingContent here — keep what was streamed so
          // the user sees the partial response. The next refetch (triggered
          // below) will surface the persisted error-status message.
          setIsStreaming(false)
          setStreamEndedAt(Date.now())
          setStreamError(err)
          queryClient
            .invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
            .then(() => {
              // Defer to next frame so the refetched messages render before
              // we remove the streaming placeholder (avoids end-of-stream
              // flicker).
              requestAnimationFrame(() => {
                setStreamingContent('')
                accumulated = ''
                setPendingUserMessage(null)
              })
            })
        },
      }

      abortControllerRef.current = new AbortController()
      streamMessage(orgId, targetConvId, message, callbacks, abortControllerRef.current.signal, attachmentIds)
    },
    [orgId, conversationId, navigate, queryClient, t]
  )

  const handleAbort = useCallback(() => {
    abortControllerRef.current?.abort()
    setIsStreaming(false)
    setStreamEndedAt(Date.now())
    setStreamingContent('')
    setPendingUserMessage(null)
  }, [])

  const handlePendingApprovalsDetected = useCallback((pending: boolean) => {
    setPendingApprovals(pending)
  }, [])

  // Scope the background-tasks indicator to this conversation (spec F1).
  // The endpoint's ``session_id`` filter maps to ``GSageTenantSession.id`` —
  // the conversation UUID from the URL — NOT to the ``agno_session_id``
  // string (which is not a valid UUID and would be rejected with 422).
  // ``hasActiveBgTasks`` (from message_end metadata) keeps the poll armed
  // even when the first fetch found no tasks yet.
  const { count: activeBgTaskCount, tasks: activeBgTasks } = useConversationBgTasks(
    orgId,
    conversationId ?? null,
    hasActiveBgTasks
  )

  // Authoritative resolution of the active-background-tasks flag (spec B3
  // item 6): only the background-tasks query may clear it — never the arrival
  // of a new chat message.
  useEffect(() => {
    if (activeBgTaskCount === 0) setHasActiveBgTasks(false)
  }, [activeBgTaskCount])

  return (
    <div className="flex flex-1 h-full min-w-0">
      {/* Conversation sidebar */}
      <ConversationList mobileOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/* Main chat area */}
      <div className="flex flex-col flex-1 min-w-0 h-full">
        {/* Mobile toolbar — shows sidebar toggle button */}
        <div className="flex items-center gap-2 px-3 py-2 border-b md:hidden">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open conversations"
          >
            <Menu className="h-5 w-5" />
          </Button>
        </div>
        {conversationId ? (
          <div className="relative flex flex-col flex-1 min-h-0">
            <ChatWindow
              ref={chatWindowRef}
              conversationId={conversationId}
              streamingContent={streamingContent}
              isStreaming={isStreaming}
              streamError={streamError}
              pendingApprovals={pendingApprovals}
              hasActiveBgTasks={hasActiveBgTasks}
              streamEndedAt={streamEndedAt}
              onPendingApprovalsDetected={handlePendingApprovalsDetected}
              pendingUserMessage={pendingUserMessage}
            />
            <ChatInput
              onSend={handleSend}
              onAbort={handleAbort}
              isStreaming={isStreaming}
              disabled={pendingApprovals || !hasPermission('agents:run')}
              onUploadAttachment={
                orgId
                  ? (file, options) =>
                      uploadChatAttachment(orgId, conversationId, file, options)
                  : undefined
              }
            />
            <BackgroundTasksIndicator count={activeBgTaskCount} tasks={activeBgTasks} />
          </div>
        ) : (
          <div className="flex flex-col flex-1 items-center justify-between">
            {/* Empty state - still can accept a new message */}
            <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center px-4">
              <div className="w-16 h-16 rounded-full bg-[hsl(var(--primary))]/10 flex items-center justify-center">
                <span className="text-3xl">🤖</span>
              </div>
              <div>
                <h2 className="text-xl font-semibold">{t('chat.welcomeTitle')}</h2>
                <p className="text-muted-foreground text-sm mt-1">{t('chat.welcomeSubtitle')}</p>
              </div>
            </div>
            <ChatInput
              onSend={handleSend}
              onAbort={handleAbort}
              isStreaming={isStreaming}
              disabled={!hasPermission('agents:run')}
            />
          </div>
        )}
      </div>

      {/* Interaction Service — renders modals for form/confirm/upload requests from tools */}
      <InteractionRenderer
        open={interaction.state.visible}
        interactionId={interaction.state.interactionId}
        title={interaction.state.title}
        description={interaction.state.description}
        schema={interaction.state.schema}
        submitLabel={interaction.state.submitLabel}
        cancelLabel={interaction.state.cancelLabel}
        size={interaction.state.size}
        isLoading={interactionLoading}
        onSubmit={handleInteractionSubmit}
        onCancel={handleInteractionCancel}
        onClose={interaction.dismiss}
      />
    </div>
  )
}
