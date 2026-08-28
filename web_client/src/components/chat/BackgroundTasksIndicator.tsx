import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Activity, Clock, ListChecks } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import type { BackgroundTask } from '@/api/tasks'
import { cn } from '@/lib/utils'

interface Props {
  count: number
  tasks: BackgroundTask[]
}

/**
 * Floating bottom-right indicator of the active conversation's background
 * tasks (spec F1). Conversation-scoped: the parent only feeds tasks of the
 * active conversation. Hidden while nothing is queued/running.
 */
export function BackgroundTasksIndicator({ count, tasks }: Props) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // Close the panel on click outside.
  useEffect(() => {
    if (!open) return
    const handleMouseDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [open])

  // Close when the last task finished.
  useEffect(() => {
    if (count === 0) setOpen(false)
  }, [count])

  if (count <= 0) return null

  const runningCount = tasks.filter((task) => task.status === 'running').length
  const queuedCount = tasks.filter((task) => task.status === 'queued').length

  return (
    <div ref={containerRef} className="absolute bottom-2 right-3 z-30">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-label={t('chat.bgTasks.title')}
        className={cn(
          'relative flex h-8 w-8 items-center justify-center rounded-full border shadow-sm',
          'bg-background text-foreground hover:bg-accent transition-colors',
          runningCount > 0 && 'border-blue-400/50'
        )}
      >
        <Activity
          className={cn('h-3.5 w-3.5', runningCount > 0 && 'animate-pulse text-blue-500')}
        />
        <span className="absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-0.5 text-[9px] font-semibold text-primary-foreground">
          {count}
        </span>
      </button>

      {open && (
        <div className="absolute bottom-9 right-0 w-72 overflow-hidden rounded-lg border bg-popover text-popover-foreground shadow-lg">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-sm font-semibold">
              {t('chat.bgTasks.title')} ({count})
            </span>
            <span className="text-xs text-muted-foreground">
              {t('chat.bgTasks.running')}: {runningCount} · {t('chat.bgTasks.queued')}: {queuedCount}
            </span>
          </div>
          <div className="max-h-72 overflow-y-auto p-2">
            {tasks.length === 0 ? (
              <p className="px-2 py-4 text-center text-xs text-muted-foreground">
                {t('chat.bgTasks.empty')}
              </p>
            ) : (
              tasks.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => navigate('/tasks')}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent"
                >
                  <Clock
                    className={cn(
                      'h-3.5 w-3.5 shrink-0',
                      task.status === 'running'
                        ? 'text-blue-500 animate-pulse'
                        : 'text-amber-500'
                    )}
                  />
                  <span className="min-w-0 flex-1 truncate">{task.tool_name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatDistanceToNow(new Date(task.started_at ?? task.created_at), {
                      addSuffix: true,
                    })}
                  </span>
                </button>
              ))
            )}
          </div>
          <button
            type="button"
            onClick={() => navigate('/tasks')}
            className="flex w-full items-center justify-center gap-2 border-t px-3 py-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <ListChecks className="h-3.5 w-3.5" />
            {t('chat.bgTasks.openTasks')}
          </button>
        </div>
      )}
    </div>
  )
}
