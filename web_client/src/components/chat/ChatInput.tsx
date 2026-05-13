import { useRef, useState, useCallback, useEffect, KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, Square, Paperclip, X, FileText, Loader2, AlertCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'
import axios from 'axios'

interface QueuedFile {
  /** Local UUID used as React key while the upload is in-flight. */
  localId: string
  /** Resolved attachment UUID from the API (set when status === 'done'). */
  id?: string
  filename: string
  size_bytes: number
  /** Upload progress 0-100. */
  progress: number
  status: 'uploading' | 'done' | 'error'
  /** Abort handle for in-flight uploads. */
  abort?: () => void
}

interface UploadProgressOptions {
  onProgress: (percent: number) => void
  signal: AbortSignal
}

interface Props {
  onSend: (message: string, attachmentIds?: string[]) => void
  onAbort: () => void
  isStreaming: boolean
  disabled?: boolean
  orgId?: string
  convId?: string
  onUploadAttachment?: (
    file: File,
    options: UploadProgressOptions
  ) => Promise<{ id: string; filename: string; size_bytes: number }>
}

function getUploadErrorKey(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const status = error.response?.status
    if (status === 413) return 'chat.attachUploadErrorTooLarge'
    if (status === 415) return 'chat.attachUploadErrorInvalidType'
  }
  return 'chat.attachUploadErrorGeneric'
}

function generateLocalId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function ChatInput({ onSend, onAbort, isStreaming, disabled, onUploadAttachment }: Props) {
  const { t } = useTranslation()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [value, setValue] = useState('')
  const [queuedFiles, setQueuedFiles] = useState<QueuedFile[]>([])
  const [dragOver, setDragOver] = useState(false)

  // Any chip currently being uploaded → disables Send and the +file button
  const hasInFlightUpload = queuedFiles.some((f) => f.status === 'uploading')

  const adjustHeight = () => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'
  }

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value)
    adjustHeight()
  }

  const handleSend = useCallback(() => {
    const msg = value.trim()
    if (!msg || isStreaming || hasInFlightUpload) return
    const ids = queuedFiles
      .filter((f) => f.status === 'done' && f.id)
      .map((f) => f.id as string)
    setValue('')
    setQueuedFiles([])
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    onSend(msg, ids.length > 0 ? ids : undefined)
  }, [value, isStreaming, hasInFlightUpload, onSend, queuedFiles])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  /**
   * Start uploading the given files concurrently.
   *
   * For each file we immediately push a chip into `queuedFiles` with
   * status='uploading', then update its progress / status as the upload
   * proceeds. This gives the user feedback for large files instead of an
   * apparent freeze while the POST is in-flight.
   */
  const startUploads = useCallback(
    (files: File[]) => {
      if (!files.length || !onUploadAttachment) return
      const available = 10 - queuedFiles.length
      if (available <= 0) return
      const toUpload = files.slice(0, available)

      const entries: QueuedFile[] = toUpload.map((file) => {
        const controller = new AbortController()
        return {
          localId: generateLocalId(),
          filename: file.name,
          size_bytes: file.size,
          progress: 0,
          status: 'uploading',
          abort: () => controller.abort(),
          // Stash the controller on the entry so onProgress can refer to it
          // via closure below.
          ...({ _controller: controller } as object),
        } as QueuedFile & { _controller: AbortController }
      })

      setQueuedFiles((prev) => [...prev, ...entries])

      entries.forEach((entry, index) => {
        const file = toUpload[index]
        const controller = (entry as QueuedFile & { _controller: AbortController })._controller

        onUploadAttachment(file, {
          onProgress: (percent) => {
            setQueuedFiles((prev) =>
              prev.map((f) =>
                f.localId === entry.localId && f.status === 'uploading'
                  ? { ...f, progress: percent }
                  : f
              )
            )
          },
          signal: controller.signal,
        })
          .then((result) => {
            setQueuedFiles((prev) =>
              prev.map((f) =>
                f.localId === entry.localId
                  ? {
                      ...f,
                      id: result.id,
                      filename: result.filename,
                      size_bytes: result.size_bytes,
                      progress: 100,
                      status: 'done',
                      abort: undefined,
                    }
                  : f
              )
            )
          })
          .catch((error) => {
            // Aborted uploads were already removed from the list; ignore.
            if (axios.isCancel(error) || (error as { name?: string })?.name === 'CanceledError') {
              return
            }
            toast.error(t(getUploadErrorKey(error)))
            setQueuedFiles((prev) =>
              prev.map((f) =>
                f.localId === entry.localId
                  ? { ...f, status: 'error', abort: undefined }
                  : f
              )
            )
          })
      })
    },
    [onUploadAttachment, queuedFiles.length, t]
  )

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? [])
    // Reset input so same file can be re-selected
    e.target.value = ''
    startUploads(files)
  }

  const handleDragEnter = (e: React.DragEvent<HTMLDivElement>) => {
    if (!onUploadAttachment || isStreaming) return
    e.preventDefault()
    e.stopPropagation()
    setDragOver(true)
  }

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (!onUploadAttachment || isStreaming) return
    e.preventDefault()
    e.stopPropagation()
  }

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setDragOver(false)
    }
  }

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    if (!onUploadAttachment || isStreaming) return
    const files = Array.from(e.dataTransfer.files)
    startUploads(files)
  }

  const removeQueuedFile = (localId: string) => {
    setQueuedFiles((prev) => {
      const target = prev.find((f) => f.localId === localId)
      if (target?.status === 'uploading') {
        try {
          target.abort?.()
        } catch {
          // ignore abort errors
        }
      }
      return prev.filter((f) => f.localId !== localId)
    })
  }

  // Restore focus to the textarea when streaming ends
  useEffect(() => {
    if (!isStreaming && !disabled) {
      textareaRef.current?.focus()
    }
  }, [isStreaming, disabled])

  return (
    <div
      className="border-t bg-background p-4"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <div className="relative">
        {/* Queued file chips */}
        {queuedFiles.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {queuedFiles.map((f) => {
              const showProgress = f.status === 'uploading'
              const isError = f.status === 'error'
              const removeLabel =
                f.status === 'uploading'
                  ? t('chat.cancelAttachment')
                  : t('chat.removeAttachment')
              return (
                <div
                  key={f.localId}
                  className={cn(
                    'flex flex-col gap-1 rounded-lg border bg-muted/50 px-2 py-1 text-xs min-w-[160px]',
                    isError && 'border-destructive/50 bg-destructive/10'
                  )}
                >
                  <div className="flex items-center gap-1">
                    {showProgress ? (
                      <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
                    ) : isError ? (
                      <AlertCircle className="h-3 w-3 shrink-0 text-destructive" />
                    ) : (
                      <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                    )}
                    <span className="max-w-[160px] truncate" title={f.filename}>
                      {f.filename}
                    </span>
                    {showProgress && (
                      <span className="text-muted-foreground tabular-nums">
                        {f.progress}%
                      </span>
                    )}
                    <button
                      onClick={() => removeQueuedFile(f.localId)}
                      className="ml-auto rounded-full p-0.5 hover:bg-muted"
                      title={removeLabel}
                      aria-label={removeLabel}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                  {showProgress && (
                    <Progress
                      value={f.progress}
                      className="h-1 w-full"
                      aria-label={t('chat.attachUploading', { filename: f.filename })}
                    />
                  )}
                  {isError && (
                    <span className="text-destructive">
                      {t('chat.attachUploadFailed')}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )}

        <div
          className={cn(
            'relative flex items-end gap-2 rounded-2xl border bg-background shadow-sm transition-colors',
            'focus-within:border-primary focus-within:ring-1 focus-within:ring-primary/30',
            disabled && 'opacity-60 pointer-events-none'
          )}
        >
          <textarea
            ref={textareaRef}
            rows={1}
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            disabled={disabled || isStreaming}
            placeholder={t('chat.messagePlaceholder')}
            className="flex-1 resize-none bg-transparent px-4 py-3 text-sm outline-none min-h-[44px] max-h-[200px] placeholder:text-muted-foreground"
          />

          <div className="flex items-center gap-1 pe-2 pb-2">
            {/* Attachment button — only when upload handler provided and not streaming */}
            {onUploadAttachment && !isStreaming && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={handleFileSelect}
                  disabled={disabled || queuedFiles.length >= 10}
                />
                <Button
                  size="icon"
                  variant="ghost"
                  className="h-8 w-8 rounded-full text-muted-foreground hover:text-foreground"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={disabled || queuedFiles.length >= 10}
                  title={t('chat.attachFile')}
                >
                  <Paperclip className={cn('h-4 w-4', hasInFlightUpload && 'animate-pulse')} />
                </Button>
              </>
            )}

            {isStreaming ? (
              <Button
                size="icon"
                variant="ghost"
                className="h-8 w-8 rounded-full text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={onAbort}
                title={t('chat.stopGeneration')}
              >
                <Square className="h-4 w-4 fill-current" />
              </Button>
            ) : (
              <Button
                size="icon"
                className="h-8 w-8 rounded-full"
                onClick={handleSend}
                disabled={!value.trim() || hasInFlightUpload}
                title={
                  hasInFlightUpload ? t('chat.attachUploadingHint') : t('chat.sendMessage')
                }
              >
                <Send className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>

        <p className="text-center text-xs text-muted-foreground mt-2">
          {t('chat.inputHint')}
        </p>

        {/* Drag-and-drop overlay */}
        {dragOver && onUploadAttachment && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-primary bg-background/90">
            <Paperclip className="h-6 w-6 text-primary mb-1" />
            <p className="text-sm font-medium text-primary">{t('chat.dropFilesHere')}</p>
          </div>
        )}
      </div>
    </div>
  )
}
