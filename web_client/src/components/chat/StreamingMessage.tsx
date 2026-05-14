import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Bot } from 'lucide-react'
import { MarkdownLink } from './MarkdownLink'
import { StreamingMarkdownCode } from './MarkdownCode'
import { MarkdownTable } from './MarkdownTable'

interface Props {
  content: string
  isStreaming?: boolean
}

export function StreamingMessage({ content, isStreaming = true }: Props) {
  return (
    <div className="flex gap-3 justify-start">
      <div className="shrink-0 w-8 h-8 rounded-full bg-[hsl(var(--primary))] flex items-center justify-center mt-1">
        <Bot className="h-4 w-4 text-white" />
      </div>
      <div className="relative max-w-[80%] min-w-0 rounded-2xl rounded-tl-sm px-4 py-3 text-sm shadow-sm bg-card border overflow-hidden">
        {content ? (
          <div className="prose-chat min-w-0">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MarkdownLink, pre: StreamingMarkdownCode, table: MarkdownTable }}>{content}</ReactMarkdown>
          </div>
        ) : isStreaming ? (
          <div className="flex gap-1 items-center py-1">
            <span className="w-2 h-2 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:0ms]" />
            <span className="w-2 h-2 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:150ms]" />
            <span className="w-2 h-2 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:300ms]" />
          </div>
        ) : null}
        {content && isStreaming && (
          <span className="typing-cursor" />
        )}
      </div>
    </div>
  )
}
