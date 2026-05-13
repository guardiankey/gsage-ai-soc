import { useRef, useState, useCallback, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Menu } from 'lucide-react'
import { createConversation, streamMessage, uploadChatAttachment, type SendMessageResponse } from '@/api/chat'
import { useAuth } from '@/contexts/AuthContext'
import { ConversationList } from '@/components/chat/ConversationList'
import { ChatWindow, type ChatWindowHandle } from '@/components/chat/ChatWindow'
import { ChatInput } from '@/components/chat/ChatInput'
import { Button } from '@/components/ui/button'
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
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  // Tracks whether onPaused fired in the current stream (avoids stale closure reads).
  const pausedRef = useRef(false)
  // Tracks whether we just created a new conversation (avoids resetting pendingUserMessage on nav).
  const justCreatedRef = useRef(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Reset streaming state when conversation changes
  useEffect(() => {
    if (justCreatedRef.current) {
      // Don't reset streaming state — handleSend is actively streaming
      // into this newly created conversation.
      justCreatedRef.current = false
      setSidebarOpen(false)
      return
    }
    setStreamingContent('')
    setIsStreaming(false)
    setStreamError(null)
    setPendingApprovals(false)
    setHasActiveBgTasks(false)
    setPendingUserMessage(null)
    pausedRef.current = false
    setSidebarOpen(false)
  }, [conversationId])

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
          accumulated += text
          setStreamingContent(accumulated)
        },
        onPaused: (_data: { pending_approvals: string[]; run_id?: string }) => {
          // Agent paused for HITL approval — stop the streaming cursor.
          // Re-set pendingUserMessage here to survive the conversationId-change
          // reset effect (which may have fired when navigate() created a new conv).
          pausedRef.current = true
          setIsStreaming(false)
          setPendingApprovals(true)
          setPendingUserMessage(message)
        },
        onDone: (metadata: SendMessageResponse['metadata'], status?: 'error' | 'paused' | null) => {
          setIsStreaming(false)
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
                setStreamingContent('')
                accumulated = ''
                setPendingUserMessage(null)
              })
            queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
            chatWindowRef.current?.scrollToBottom()
          } else {
            queryClient
              .invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
              .then(() => {
                setStreamingContent('')
                accumulated = ''
                setPendingUserMessage(null)
              })
            queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
            chatWindowRef.current?.scrollToBottom()
          }
        },
        onError: (err: string) => {
          // Do NOT clear streamingContent here — keep what was streamed so
          // the user sees the partial response. The next refetch (triggered
          // below) will surface the persisted error-status message.
          setIsStreaming(false)
          setStreamError(err)
          queryClient
            .invalidateQueries({ queryKey: ['messages', orgId, targetConvId] })
            .then(() => {
              setStreamingContent('')
              accumulated = ''
              setPendingUserMessage(null)
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
    setStreamingContent('')
    setPendingUserMessage(null)
  }, [])

  const handleBgTasksResolved = useCallback(() => {
    setHasActiveBgTasks(false)
  }, [])

  const handlePendingApprovalsDetected = useCallback((pending: boolean) => {
    setPendingApprovals(pending)
  }, [])

  return (
    <div className="flex flex-1 h-full">
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
          <>
            <ChatWindow
              ref={chatWindowRef}
              conversationId={conversationId}
              streamingContent={streamingContent}
              isStreaming={isStreaming}
              streamError={streamError}
              pendingApprovals={pendingApprovals}
              hasActiveBgTasks={hasActiveBgTasks}
              onBgTasksResolved={handleBgTasksResolved}
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
          </>
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
    </div>
  )
}
