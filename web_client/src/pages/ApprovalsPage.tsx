import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, XCircle, Clock, ChevronRight, MessageSquare, Loader2 } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  listApprovals,
  getApproval,
  resolveApproval,
  type Approval,
} from '@/api/approvals'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'

/** Render an object/array as a human-friendly key-value list.
 *  Primitive values shown inline; nested objects/arrays collapse to JSON. */
function formatLabel(key: string): string {
  return key
    .replace(/^_+/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function DataTable({ data }: { data: unknown }) {
  if (data === null || data === undefined) return null

  if (typeof data === 'string' || typeof data === 'number' || typeof data === 'boolean') {
    return <span className="text-sm">{String(data)}</span>
  }

  if (Array.isArray(data)) {
    if (data.length === 0) return <span className="text-sm text-muted-foreground">—</span>
    // Array of primitives → comma-separated
    if (data.every((v) => typeof v !== 'object' || v === null)) {
      return <span className="text-sm">{data.map(String).join(', ')}</span>
    }
    // Array of objects → render each as a card
    return (
      <div className="space-y-2">
        {data.map((item, i) => (
          <div key={i} className="bg-muted/50 rounded p-2 border">
            <DataTable data={item} />
          </div>
        ))}
      </div>
    )
  }

  if (typeof data === 'object') {
    const entries = Object.entries(data as Record<string, unknown>)
    if (entries.length === 0) return <span className="text-sm text-muted-foreground">—</span>
    return (
      <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        {entries.map(([key, value]) => {
          const isComplex = value !== null && typeof value === 'object'
          return (
            <div key={key} className={cn('contents', isComplex && 'col-span-2')}>
              {isComplex ? (
                <div className="col-span-2 mt-1">
                  <p className="text-xs text-muted-foreground font-medium mb-1">{formatLabel(key)}</p>
                  <div className="ps-3 border-s-2 border-muted">
                    <DataTable data={value} />
                  </div>
                </div>
              ) : (
                <>
                  <span className="text-muted-foreground text-xs font-medium whitespace-nowrap py-0.5">
                    {formatLabel(key)}
                  </span>
                  <span className="py-0.5 break-words">{String(value ?? '—')}</span>
                </>
              )}
            </div>
          )
        })}
      </div>
    )
  }

  return <span className="text-sm">{String(data)}</span>
}

const STATUS_MAP: Record<string, { label: string; variant: 'warning' | 'success' | 'destructive' | 'secondary' }> = {
  pending: { label: 'Pending', variant: 'warning' },
  approved: { label: 'Approved', variant: 'success' },
  rejected: { label: 'Rejected', variant: 'destructive' },
  timeout: { label: 'Timeout', variant: 'secondary' },
}

export default function ApprovalsPage() {
  const { t } = useTranslation()
  const { orgId, hasPermission } = useAuth()
  const canResolve = hasPermission('approvals:resolve')
  const queryClient = useQueryClient()
  const [tab, setTab] = useState('pending')
  const [pendingPage, setPendingPage] = useState(1)
  const [allPage, setAllPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [comment, setComment] = useState('')

  const { data: pendingData, isLoading: pendingLoading } = useQuery({
    queryKey: ['approvals', orgId, 'pending', pendingPage],
    queryFn: () => listApprovals(orgId!, 'pending', pendingPage, 20),
    enabled: !!orgId,
    refetchInterval: 10_000,
  })

  const { data: allData, isLoading: allLoading } = useQuery({
    queryKey: ['approvals', orgId, 'all', allPage],
    queryFn: () => listApprovals(orgId!, undefined, allPage, 20),
    enabled: !!orgId && tab === 'all',
  })

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['approval-detail', orgId, selectedId],
    queryFn: () => getApproval(orgId!, selectedId!),
    enabled: !!orgId && !!selectedId,
  })

  const resolveMut = useMutation({
    mutationFn: ({ decision }: { decision: 'approve' | 'reject' }) =>
      resolveApproval(orgId!, selectedId!, decision, comment),
    onSuccess: (_, { decision }) => {
      queryClient.invalidateQueries({ queryKey: ['approvals', orgId] })
      toast.success(decision === 'approve' ? t('approvals.approved') : t('approvals.rejected'))
      setSelectedId(null)
      setComment('')
      // Continuation is handled automatically by the backend Celery task
      // dispatched inside the /resolve endpoint. No need to call /continue-run.
    },
    onError: () => toast.error(t('common.error')),
  })

  const pendingItems = pendingData?.items ?? []
  const allItems = allData?.items ?? []

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold">{t('approvals.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('approvals.subtitle')}</p>
          </div>
          {pendingData?.total != null && pendingData.total > 0 && (
            <Badge variant="warning" className="text-sm px-3 py-1">
              {pendingData.total} {t('approvals.pending')}
            </Badge>
          )}
        </div>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="pending">
              {t('approvals.pending')}
              {(pendingData?.total ?? 0) > 0 && (
                <span className="ms-1.5 bg-orange-200 dark:bg-orange-800 text-orange-800 dark:text-orange-200 rounded-full text-xs px-1.5 py-0.5 leading-none">
                  {pendingData!.total}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="all">{t('approvals.history')}</TabsTrigger>
          </TabsList>

          <TabsContent value="pending" className="mt-4">
            <ApprovalList
              items={pendingItems}
              loading={pendingLoading}
              onSelect={canResolve ? setSelectedId : undefined}
              emptyMessage={t('approvals.noPending')}
            />
            {pendingData && (
              <Pagination
                page={pendingPage}
                totalPages={calcTotalPages(pendingData.total, 20)}
                onPageChange={setPendingPage}
                className="mt-4"
              />
            )}
          </TabsContent>

          <TabsContent value="all" className="mt-4">
            <ApprovalList
              items={allItems}
              loading={allLoading}
              onSelect={canResolve ? setSelectedId : undefined}
              emptyMessage={t('approvals.noHistory')}
            />
            {allData && (
              <Pagination
                page={allPage}
                totalPages={calcTotalPages(allData.total, 20)}
                onPageChange={setAllPage}
                className="mt-4"
              />
            )}
          </TabsContent>
        </Tabs>
      </div>

      {/* Detail dialog */}
      <Dialog open={!!selectedId} onOpenChange={(o) => !o && setSelectedId(null)}>
        <DialogContent className="max-w-2xl">
          {detailLoading ? (
            <>
              <DialogHeader>
                <DialogTitle className="sr-only">{t('approvals.reviewTitle')}</DialogTitle>
                <DialogDescription className="sr-only">{t('approvals.reviewTitle')}</DialogDescription>
              </DialogHeader>
              <div className="space-y-3 py-4">
                <Skeleton className="h-6 w-3/4" />
                <Skeleton className="h-20 w-full" />
              </div>
            </>
          ) : detail ? (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2">
                  {t('approvals.reviewTitle')}
                  <Badge variant={STATUS_MAP[detail.status]?.variant ?? 'secondary'}>
                    {detail.status}
                  </Badge>
                </DialogTitle>
              </DialogHeader>

              <div className="space-y-4 max-h-[60vh] overflow-y-auto">
                {/* Summary / justification */}
                {detail.summary && (
                  <div className="bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg p-3">
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.summary')}</p>
                    <p className="text-sm">{detail.summary}</p>
                  </div>
                )}

                {/* Tool info */}
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.toolName')}</p>
                    <code className="bg-muted rounded px-2 py-1 text-xs">{detail.tool_name}</code>
                  </div>
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.requestedAt')}</p>
                    <p className="font-medium text-xs">{formatDistanceToNow(new Date(detail.created_at * 1000), { addSuffix: true })}</p>
                  </div>
                  {detail.requester_user_name && (
                    <div>
                      <p className="text-muted-foreground text-xs mb-1">{t('approvals.requestedBy')}</p>
                      <p className="text-xs font-medium">{detail.requester_user_name}</p>
                    </div>
                  )}
                  {detail.delegated_to_user_name && (
                    <div>
                      <p className="text-muted-foreground text-xs mb-1">{t('approvals.delegatedTo')}</p>
                      <p className="text-xs font-medium">{detail.delegated_to_user_name}</p>
                    </div>
                  )}
                  {detail.status === 'pending' && detail.expires_at && (
                    <div>
                      <p className="text-muted-foreground text-xs mb-1">{t('approvals.expiresAt')}</p>
                      <p className="text-xs font-medium">{formatDistanceToNow(new Date(detail.expires_at * 1000), { addSuffix: true })}</p>
                    </div>
                  )}
                </div>

                {/* Tool input */}
                {detail.tool_args && Object.keys(detail.tool_args).length > 0 && (
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.toolInput')}</p>
                    <div className="bg-muted rounded p-3">
                      <DataTable data={detail.tool_args} />
                    </div>
                  </div>
                )}

                {/* Requirements */}
                {detail.requirements && (
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.requirements')}</p>
                    <div className="bg-muted rounded p-3">
                      {typeof detail.requirements === 'string'
                        ? <p className="text-sm">{detail.requirements}</p>
                        : <DataTable data={detail.requirements} />}
                    </div>
                  </div>
                )}

                {/* Context */}
                {detail.context && Object.keys(detail.context).length > 0 && (
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.context')}</p>
                    <div className="bg-muted rounded p-3">
                      <DataTable data={detail.context} />
                    </div>
                  </div>
                )}

                {/* Resolution data (shown when resolved) */}
                {detail.resolution_data && Object.keys(detail.resolution_data).length > 0 && (
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.resolutionData')}</p>
                    <div className="bg-muted rounded p-3">
                      <DataTable data={detail.resolution_data} />
                    </div>
                  </div>
                )}

                {/* Reviewer comment (existing comment on resolved approval) */}
                {detail.comment && detail.status !== 'pending' && (
                  <div>
                    <p className="text-muted-foreground text-xs mb-1">{t('approvals.comment')}</p>
                    <p className="text-sm bg-muted rounded p-3">{detail.comment}</p>
                  </div>
                )}

                {detail.status === 'pending' && canResolve && (
                  <div>
                    <Label htmlFor="approval-comment">
                      {t('approvals.comment')} ({t('common.optional')})
                    </Label>
                    <Textarea
                      id="approval-comment"
                      rows={3}
                      value={comment}
                      onChange={(e) => setComment(e.target.value)}
                      placeholder={t('approvals.commentPlaceholder')}
                    />
                  </div>
                )}

                {/* Reviewer info */}
                {(detail.resolved_by || detail.resolved_by_user_id) && (
                  <div className="text-xs text-muted-foreground">
                    {t('approvals.reviewedBy')}: <span className="font-medium">{detail.resolved_by ?? detail.resolved_by_user_id}</span>
                  </div>
                )}
              </div>

              {detail.status === 'pending' && canResolve && (
                <DialogFooter className="gap-2">
                  <Button
                    variant="outline"
                    onClick={() => resolveMut.mutate({ decision: 'reject' })}
                    disabled={resolveMut.isPending}
                    className="text-destructive hover:bg-destructive/10 border-destructive/50"
                  >
                    <XCircle className="h-4 w-4 me-2" />
                    {t('approvals.reject')}
                  </Button>
                  <Button
                    onClick={() => resolveMut.mutate({ decision: 'approve' })}
                    disabled={resolveMut.isPending}
                  >
                    {resolveMut.isPending
                      ? <Loader2 className="h-4 w-4 animate-spin me-2" />
                      : <CheckCircle2 className="h-4 w-4 me-2" />
                    }
                    {t('approvals.approve')}
                  </Button>
                </DialogFooter>
              )}
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  )
}

function ApprovalList({
  items,
  loading,
  onSelect,
  emptyMessage,
}: {
  items: Approval[]
  loading: boolean
  onSelect?: (id: string) => void
  emptyMessage: string
}) {
  const { t } = useTranslation()
  if (loading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <Card>
        <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
          <CheckCircle2 className="h-10 w-10 opacity-40" />
          <p className="text-sm">{emptyMessage}</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-2">
      {items.map((item) => (
        <Card
          key={item.id}
          className={onSelect ? 'cursor-pointer hover:shadow-md transition-shadow' : 'opacity-75'}
          onClick={onSelect ? () => onSelect(item.id) : undefined}
        >
          <CardContent className="p-4 flex items-center gap-3">
            <div className="shrink-0">
              {item.status === 'pending' && <Clock className="h-5 w-5 text-orange-500" />}
              {item.status === 'approved' && <CheckCircle2 className="h-5 w-5 text-green-500" />}
              {item.status === 'rejected' && <XCircle className="h-5 w-5 text-destructive" />}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <code className="text-xs bg-muted rounded px-1.5 py-0.5">{item.tool_name}</code>
                <Badge variant={STATUS_MAP[item.status]?.variant ?? 'secondary'} className="text-xs">
                  {item.status}
                </Badge>
              </div>
            </div>
            <div className="text-xs text-muted-foreground shrink-0">
              {formatDistanceToNow(new Date(item.created_at * 1000), { addSuffix: true })}
            </div>
            <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
