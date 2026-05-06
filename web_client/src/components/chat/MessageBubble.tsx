import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, User, Bot, FileText, AlertTriangle } from 'lucide-react'
import { type Message } from '@/api/chat'
import { cn, formatDate } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { MarkdownLink } from './MarkdownLink'
import { MarkdownCode } from './MarkdownCode'

interface Props {
  message: Message
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function MessageBubble({ message }: Props) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const isUser = message.role === 'user'
  const isError = !isUser && message.status === 'error'

  const handleCopy = async () => {
    await navigator.clipboard.writeText(message.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className={cn('flex gap-3 group', isUser ? 'justify-end' : 'justify-start')}>
      {/* Avatar / Icon */}
      {!isUser && (
        <div className="shrink-0 w-8 h-8 rounded-full bg-[hsl(var(--primary))] flex items-center justify-center mt-1">
          <Bot className="h-4 w-4 text-white" />
        </div>
      )}

      <div className="flex flex-col gap-1 max-w-[80%]">
        {/* Attachment chips (user messages) */}
        {isUser && message.attachments && message.attachments.length > 0 && (
          <div className="flex flex-wrap gap-1 justify-end">
            {message.attachments.map((att) => (
              <div
                key={att.file_id}
                className="flex items-center gap-1 rounded-lg border bg-muted/50 px-2 py-1 text-xs"
              >
                <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="max-w-[120px] truncate">{att.filename}</span>
                <span className="text-muted-foreground">({formatBytes(att.size_bytes)})</span>
              </div>
            ))}
          </div>
        )}

        {/* Bubble */}
        <div
          className={cn(
            'relative rounded-2xl px-4 py-3 text-sm shadow-sm',
            isUser
              ? 'bg-[hsl(var(--primary))] text-white rounded-tr-sm'
              : isError
                ? 'bg-destructive/5 border border-destructive/40 rounded-tl-sm'
                : 'bg-card border rounded-tl-sm'
          )}
        >
          {isError && (
            <div className="flex items-center gap-1.5 mb-2 text-xs font-medium text-destructive">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              <span>{t('chat.errorBadge')}</span>
            </div>
          )}
          {isUser ? (
            <p className="whitespace-pre-wrap leading-relaxed">{message.content}</p>
          ) : (
            <div className="prose-chat">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink, pre: MarkdownCode }}>
                {message.content}
              </ReactMarkdown>
            </div>
          )}

          {/* Footer */}
          <div
            className={cn(
              'flex items-center gap-2 mt-1.5',
              isUser ? 'justify-start' : 'justify-end'
            )}
          >
            <span
              className={cn(
                'text-[10px]',
                isUser ? 'text-white/60' : 'text-muted-foreground'
              )}
            >
              {message.created_at ? formatDate(message.created_at) : ''}
            </span>

            {!isUser && (
              <Button
                size="icon"
                variant="ghost"
                className="h-5 w-5 opacity-0 group-hover:opacity-100 transition-opacity"
                onClick={handleCopy}
                title={t('common.copy')}
              >
                {copied
                  ? <Check className="h-3 w-3 text-green-500" />
                  : <Copy className="h-3 w-3" />
                }
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* User avatar */}
      {isUser && (
        <div className="shrink-0 w-8 h-8 rounded-full bg-muted border flex items-center justify-center mt-1">
          <User className="h-4 w-4 text-muted-foreground" />
        </div>
      )}
    </div>
  )
}
