import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Activity, Clock, CheckCircle2, XCircle, Search, ChevronRight, Terminal } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { listTasks, getTask, type BackgroundTask } from '@/api/tasks'
import { JsonTable } from '@/components/ui/json-table'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'

const STATUS_CONFIG: Record<string, { icon: React.ElementType; variant: any; label: string }> = {
  running: { icon: Activity, variant: 'info', label: 'Running' },
  queued: { icon: Clock, variant: 'warning', label: 'Queued' },
  completed: { icon: CheckCircle2, variant: 'success', label: 'Completed' },
  failed: { icon: XCircle, variant: 'destructive', label: 'Failed' },
  cancelled: { icon: XCircle, variant: 'secondary', label: 'Cancelled' },
}

export default function TasksPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['tasks', orgId, page, statusFilter],
    queryFn: () => listTasks(orgId!, page, 20, undefined, statusFilter as any || undefined),
    enabled: !!orgId,
    refetchInterval: (data: any) => {
      const hasRunning = data?.items?.some((t: BackgroundTask) => t.status === 'running' || t.status === 'queued')
      return hasRunning ? 5000 : false
    },
  })

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['task-detail', orgId, selectedId],
    queryFn: () => getTask(orgId!, selectedId!),
    enabled: !!orgId && !!selectedId,
    refetchInterval: (data: any) => {
      if (data?.status === 'running' || data?.status === 'queued') return 3000
      return false
    },
  })

  const STATUS_FILTERS = ['', 'running', 'queued', 'completed', 'failed']

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-5xl mx-auto">
        <div className="mb-6">
          <h1 className="text-2xl font-bold">{t('tasks.title')}</h1>
          <p className="text-muted-foreground text-sm mt-1">{t('tasks.subtitle')}</p>
        </div>

        {/* Status filters */}
        <div className="flex gap-1.5 mb-4 flex-wrap">
          {STATUS_FILTERS.map((s) => (
            <Button
              key={s || 'all'}
              variant={statusFilter === s ? 'secondary' : 'outline'}
              size="sm"
              onClick={() => { setStatusFilter(s); setPage(1) }}
            >
              {s ? STATUS_CONFIG[s]?.label ?? s : t('common.all')}
            </Button>
          ))}
        </div>

        {/* Task list */}
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-16 w-full" />)}
          </div>
        ) : (data?.items ?? []).length === 0 ? (
          <Card>
            <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
              <Terminal className="h-10 w-10 opacity-40" />
              <p className="text-sm">{t('tasks.noTasks')}</p>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-2">
            {(data?.items ?? []).map((task) => {
              const config = STATUS_CONFIG[task.status] ?? STATUS_CONFIG['cancelled']
              const Icon = config.icon
              return (
                <Card
                  key={task.id}
                  className="cursor-pointer hover:shadow-sm transition-shadow group"
                  onClick={() => setSelectedId(task.id)}
                >
                  <CardContent className="p-4 flex items-center gap-3">
                    <Icon
                      className={cn(
                        'h-5 w-5 shrink-0',
                        task.status === 'running' && 'text-blue-500 animate-pulse',
                        task.status === 'completed' && 'text-green-500',
                        task.status === 'failed' && 'text-destructive'
                      )}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <code className="text-xs bg-muted rounded px-1.5 py-0.5 truncate max-w-[200px]">
                          {task.tool_name ?? task.id}
                        </code>
                        <Badge variant={config.variant} className="text-xs">{task.status}</Badge>
                      </div>
                    </div>
                    <span className="text-xs text-muted-foreground shrink-0">
                      {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                    </span>
                    <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0 opacity-0 group-hover:opacity-100" />
                  </CardContent>
                </Card>
              )
            })}
          </div>
        )}

        {/* Pagination */}
        <Pagination
          page={page}
          totalPages={calcTotalPages(data?.total ?? 0, 20)}
          onPageChange={setPage}
          className="mt-4"
        />
      </div>

      {/* Task detail dialog */}
      <Dialog open={!!selectedId} onOpenChange={(o) => !o && setSelectedId(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {detail ? (
                <>
                  <code className="text-sm font-normal bg-muted rounded px-2 py-1">
                    {detail.tool_name ?? detail.id}
                  </code>
                  <Badge variant={STATUS_CONFIG[detail.status]?.variant ?? 'secondary'}>
                    {detail.status}
                  </Badge>
                </>
              ) : t('tasks.title')}
            </DialogTitle>
            <DialogDescription className="sr-only">{t('tasks.subtitle')}</DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <div className="space-y-3 py-4">
              <Skeleton className="h-6 w-3/4" />
              <Skeleton className="h-40 w-full" />
            </div>
          ) : detail ? (
            <div className="space-y-4 max-h-[60vh] overflow-y-auto">
              {!!detail.result && (
                <div>
                  <p className="text-xs text-muted-foreground mb-2">{t('tasks.output')}</p>
                  <div className="bg-muted/40 rounded p-3 overflow-x-auto">
                    {typeof detail.result === 'string' ? (
                      <pre className="text-xs whitespace-pre-wrap">{detail.result}</pre>
                    ) : (
                      <JsonTable data={detail.result} className="text-xs" />
                    )}
                  </div>
                </div>
              )}
              {detail.error_message && (
                <div>
                  <p className="text-xs text-muted-foreground mb-1">{t('tasks.error')}</p>
                  <pre className="bg-destructive/10 text-destructive rounded p-3 text-xs overflow-x-auto">
                    {detail.error_message}
                  </pre>
                </div>
              )}
              <div className="text-xs text-muted-foreground">
                {t('tasks.started')}: {formatDistanceToNow(new Date(detail.created_at), { addSuffix: true })}
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  )
}
