import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import {
  CalendarClock,
  CheckCircle2,
  XCircle,
  Play,
  Pause,
  Trash2,
  Plus,
  ChevronRight,
  Clock,
  Activity,
  Pencil,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useAuth } from '@/contexts/AuthContext'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import {
  listScheduledJobs,
  getScheduledJob,
  createScheduledJob,
  updateScheduledJob,
  deleteScheduledJob,
  activateScheduledJob,
  deactivateScheduledJob,
  type ScheduledJob,
  type ScheduledJobCreate,
  type ScheduledJobUpdate,
} from '@/api/scheduledJobs'

const STATUS_CONFIG: Record<string, { icon: React.ElementType; className: string }> = {
  RUNNING: { icon: Activity, className: 'text-blue-500 animate-pulse' },
  PENDING: { icon: Clock, className: 'text-yellow-500' },
  SUCCESS: { icon: CheckCircle2, className: 'text-green-500' },
  FAILURE: { icon: XCircle, className: 'text-destructive' },
  SKIPPED: { icon: Clock, className: 'text-muted-foreground' },
}

const TYPE_FILTERS = ['', 'PROMPT_RUN', 'SYSTEM_TASK'] as const
const ACTIVE_FILTERS = [
  { value: '', label: 'all' },
  { value: 'true', label: 'active' },
  { value: 'false', label: 'inactive' },
] as const

export default function ScheduledJobsPage() {
  const { t } = useTranslation()
  const { orgId, hasPermission } = useAuth()
  const queryClient = useQueryClient()

  const [page, setPage] = useState(1)
  const [typeFilter, setTypeFilter] = useState('')
  const [activeFilter, setActiveFilter] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editJob, setEditJob] = useState<ScheduledJob | null>(null)

  // ── List query ───────────────────────────────────────────────────────────
  const { data, isLoading } = useQuery({
    queryKey: ['scheduled-jobs', orgId, page, typeFilter, activeFilter],
    queryFn: () =>
      listScheduledJobs(
        orgId!,
        page,
        20,
        typeFilter as any || undefined,
        activeFilter === '' ? undefined : activeFilter === 'true',
      ),
    enabled: !!orgId,
  })

  // ── Detail query ─────────────────────────────────────────────────────────
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['scheduled-job-detail', orgId, selectedId],
    queryFn: () => getScheduledJob(orgId!, selectedId!),
    enabled: !!orgId && !!selectedId,
  })

  // ── Mutations ────────────────────────────────────────────────────────────
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['scheduled-jobs', orgId] })

  const activateMut = useMutation({
    mutationFn: (id: string) => activateScheduledJob(orgId!, id),
    onSuccess: () => { invalidate(); toast.success(t('aiAgents.activated')) },
    onError: () => toast.error(t('common.error')),
  })

  const deactivateMut = useMutation({
    mutationFn: (id: string) => deactivateScheduledJob(orgId!, id),
    onSuccess: () => { invalidate(); toast.success(t('aiAgents.deactivated')) },
    onError: () => toast.error(t('common.error')),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteScheduledJob(orgId!, id),
    onSuccess: () => {
      invalidate()
      setSelectedId(null)
      toast.success(t('aiAgents.deleted'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ScheduledJobUpdate }) =>
      updateScheduledJob(orgId!, id, payload),
    onSuccess: () => {
      invalidate()
      queryClient.invalidateQueries({ queryKey: ['scheduled-job-detail', orgId] })
      setEditJob(null)
      toast.success(t('aiAgents.updated'))
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail ?? t('common.error')
      toast.error(String(detail))
    },
  })

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-5xl mx-auto">
        {/* Header */}
        <div className="mb-6 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">{t('aiAgents.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('aiAgents.subtitle')}</p>
          </div>
          {hasPermission('scheduled_jobs:write') && (
            <Button onClick={() => setCreateOpen(true)} size="sm" className="shrink-0">
              <Plus className="h-4 w-4 me-1.5" />
              {t('aiAgents.newJob')}
            </Button>
          )}
        </div>

        {/* Filters */}
        <div className="flex flex-wrap gap-2 mb-4">
          {TYPE_FILTERS.map((f) => (
            <Button
              key={f || 'all-types'}
              variant={typeFilter === f ? 'secondary' : 'outline'}
              size="sm"
              onClick={() => { setTypeFilter(f); setPage(1) }}
            >
              {f || t('common.all')}
            </Button>
          ))}
          <div className="w-px bg-border mx-1" />
          {ACTIVE_FILTERS.map(({ value, label }) => (
            <Button
              key={value || 'all-active'}
              variant={activeFilter === value ? 'secondary' : 'outline'}
              size="sm"
              onClick={() => { setActiveFilter(value); setPage(1) }}
            >
              {t(`aiAgents.filter.${label}`)}
            </Button>
          ))}
        </div>

        {/* List */}
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-16 w-full" />)}
          </div>
        ) : (data?.items ?? []).length === 0 ? (
          <Card>
            <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
              <CalendarClock className="h-10 w-10 opacity-40" />
              <p className="text-sm">{t('aiAgents.noJobs')}</p>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-2">
            {(data?.items ?? []).map((job) => (
              <JobRow
                key={job.id}
                job={job}
                onClick={() => setSelectedId(job.id)}
                onActivate={() => activateMut.mutate(job.id)}
                onDeactivate={() => deactivateMut.mutate(job.id)}
                canWrite={hasPermission('scheduled_jobs:write')}
              />
            ))}
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

      {/* Detail dialog */}
      <Dialog open={!!selectedId} onOpenChange={(o) => !o && setSelectedId(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('aiAgents.detailTitle')}</DialogTitle>
            <DialogDescription className="sr-only">{t('aiAgents.subtitle')}</DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <div className="space-y-2 py-4">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          ) : detail ? (
            <JobDetail
              job={detail}
              onActivate={() => activateMut.mutate(detail.id)}
              onDeactivate={() => deactivateMut.mutate(detail.id)}
              onDelete={() => deleteMut.mutate(detail.id)}
              onEdit={() => setEditJob(detail)}
              loading={activateMut.isPending || deactivateMut.isPending || deleteMut.isPending || updateMut.isPending}
              canWrite={hasPermission('scheduled_jobs:write')}
            />
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Create dialog */}
      <CreateJobDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        orgId={orgId!}
        onCreated={() => {
          invalidate()
          setCreateOpen(false)
        }}
      />

      {/* Edit dialog */}
      <EditJobDialog
        job={editJob}
        orgId={orgId!}
        onClose={() => setEditJob(null)}
        onSave={(payload) => editJob && updateMut.mutate({ id: editJob.id, payload })}
        isPending={updateMut.isPending}
      />
    </div>
  )
}

// ── Sub-components ──────────────────────────────────────────────────────────

function JobRow({
  job,
  onClick,
  onActivate,
  onDeactivate,
  canWrite = false,
}: {
  job: ScheduledJob
  onClick: () => void
  onActivate: () => void
  onDeactivate: () => void
  canWrite?: boolean
}) {
  const { t } = useTranslation()
  const statusCfg = job.last_run_status ? STATUS_CONFIG[job.last_run_status] : null

  return (
    <Card
      className="cursor-pointer hover:shadow-sm transition-shadow group"
      onClick={onClick}
    >
      <CardContent className="p-4 flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-sm truncate max-w-[260px]">{job.name}</span>
            <Badge variant={job.is_active ? 'success' : 'secondary'} className="text-xs shrink-0">
              {job.is_active ? t('aiAgents.active') : t('aiAgents.inactive')}
            </Badge>
            <Badge variant="outline" className="text-xs shrink-0">{job.job_type}</Badge>
            {statusCfg && (
              <Badge variant="outline" className="text-xs shrink-0 gap-1">
                <statusCfg.icon className={cn('h-3 w-3', statusCfg.className)} />
                {job.last_run_status}
              </Badge>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            <code>{job.cron_expression}</code>
            {job.timezone !== 'UTC' && <span className="ms-1">({job.timezone})</span>}
            {job.last_run_at && (
              <span className="ms-2">
                · {t('aiAgents.lastRun')}{' '}
                {formatDistanceToNow(new Date(job.last_run_at), { addSuffix: true })}
              </span>
            )}
          </p>
        </div>
        {canWrite && (
          <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
            {job.is_active ? (
              <Button variant="ghost" size="icon" className="h-7 w-7" title={t('aiAgents.deactivate')} onClick={onDeactivate}>
                <Pause className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button variant="ghost" size="icon" className="h-7 w-7" title={t('aiAgents.activate')} onClick={onActivate}>
                <Play className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        )}
        <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0 opacity-0 group-hover:opacity-100" />
      </CardContent>
    </Card>
  )
}

function JobDetail({
  job,
  onActivate,
  onDeactivate,
  onDelete,
  onEdit,
  loading,
  canWrite = false,
}: {
  job: ScheduledJob
  onActivate: () => void
  onDeactivate: () => void
  onDelete: () => void
  onEdit: () => void
  loading: boolean
  canWrite?: boolean
}) {
  const { t } = useTranslation()
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <DetailRow label={t('aiAgents.name')} value={job.name} />
        <DetailRow label={t('aiAgents.type')} value={job.job_type} />
        <DetailRow label={t('aiAgents.cron')} value={<code className="bg-muted rounded px-1">{job.cron_expression}</code>} />
        <DetailRow label={t('aiAgents.timezone')} value={job.timezone} />
        <DetailRow label={t('aiAgents.status')} value={
          <Badge variant={job.is_active ? 'success' : 'secondary'}>
            {job.is_active ? t('aiAgents.active') : t('aiAgents.inactive')}
          </Badge>
        } />
        <DetailRow label={t('aiAgents.runCount')} value={String(job.run_count)} />
        {job.max_runs != null && <DetailRow label={t('aiAgents.maxRuns')} value={String(job.max_runs)} />}
        {job.last_run_at && (
          <DetailRow
            label={t('aiAgents.lastRun')}
            value={formatDistanceToNow(new Date(job.last_run_at), { addSuffix: true })}
          />
        )}
        {job.last_run_status && <DetailRow label={t('aiAgents.lastStatus')} value={job.last_run_status} />}
      </div>

      {job.prompt_content && (
        <div>
          <p className="text-xs font-medium text-muted-foreground mb-1">{t('aiAgents.promptContent')}</p>
          <pre className="text-xs bg-muted rounded p-2 max-h-32 overflow-auto whitespace-pre-wrap">
            {job.prompt_content}
          </pre>
        </div>
      )}

      {job.description && (
        <div>
          <p className="text-xs font-medium text-muted-foreground mb-1">{t('aiAgents.description')}</p>
          <p className="text-sm">{job.description}</p>
        </div>
      )}

      {canWrite && (
        <DialogFooter className="gap-2 pt-2">
          <Button variant="outline" size="sm" disabled={loading} onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5 me-1.5" />
            {t('common.edit')}
          </Button>
          {job.is_active ? (
            <Button variant="outline" size="sm" disabled={loading} onClick={onDeactivate}>
              <Pause className="h-3.5 w-3.5 me-1.5" />
              {t('aiAgents.deactivate')}
            </Button>
          ) : (
            <Button variant="outline" size="sm" disabled={loading} onClick={onActivate}>
              <Play className="h-3.5 w-3.5 me-1.5" />
              {t('aiAgents.activate')}
            </Button>
          )}
          {confirmDelete ? (
            <Button variant="destructive" size="sm" disabled={loading} onClick={onDelete}>
              {t('aiAgents.confirmDelete')}
            </Button>
          ) : (
            <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="h-3.5 w-3.5 me-1.5" />
              {t('common.delete')}
            </Button>
          )}
        </DialogFooter>
      )}
    </div>
  )
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="font-medium text-sm">{value}</p>
    </div>
  )
}

function CreateJobDialog({
  open,
  onClose,
  orgId,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  orgId: string
  onCreated: () => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [prompt, setPrompt] = useState('')
  const [description, setDescription] = useState('')

  const createMut = useMutation({
    mutationFn: (payload: ScheduledJobCreate) => createScheduledJob(orgId, payload),
    onSuccess: () => {
      toast.success(t('aiAgents.created'))
      setName('')
      setCron('')
      setTimezone('UTC')
      setPrompt('')
      setDescription('')
      onCreated()
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail ?? t('common.error')
      toast.error(String(detail))
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !cron.trim() || !prompt.trim()) return
    createMut.mutate({
      name: name.trim(),
      description: description.trim() || undefined,
      job_type: 'PROMPT_RUN',
      cron_expression: cron.trim(),
      timezone: timezone.trim() || 'UTC',
      prompt_content: prompt.trim(),
    })
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('aiAgents.createTitle')}</DialogTitle>
          <DialogDescription className="sr-only">{t('aiAgents.subtitle')}</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="sj-name">{t('aiAgents.name')} *</Label>
            <Input
              id="sj-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('aiAgents.namePlaceholder')}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="sj-cron">
              {t('aiAgents.cron')} *{' '}
              <span className="text-xs font-normal text-muted-foreground">
                (e.g. <code>0 9 * * 1-5</code>)
              </span>
            </Label>
            <Input
              id="sj-cron"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 9 * * 1-5"
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="sj-tz">{t('aiAgents.timezone')}</Label>
            <Input
              id="sj-tz"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              placeholder="UTC"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="sj-prompt">{t('aiAgents.promptContent')} *</Label>
            <Textarea
              id="sj-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t('aiAgents.promptPlaceholder')}
              rows={4}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="sj-desc">{t('aiAgents.description')} <span className="text-muted-foreground text-xs">({t('common.optional')})</span></Label>
            <Input
              id="sj-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('aiAgents.descriptionPlaceholder')}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={createMut.isPending || !name.trim() || !cron.trim() || !prompt.trim()}>
              {createMut.isPending ? t('common.loading') : t('common.create')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function EditJobDialog({
  job,
  orgId,
  onClose,
  onSave,
  isPending,
}: {
  job: ScheduledJob | null
  orgId: string
  onClose: () => void
  onSave: (payload: ScheduledJobUpdate) => void
  isPending: boolean
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [prompt, setPrompt] = useState('')
  const [description, setDescription] = useState('')

  // Reset form when job changes
  useEffect(() => {
    if (job) {
      setName(job.name ?? '')
      setCron(job.cron_expression ?? '')
      setTimezone(job.timezone ?? 'UTC')
      setPrompt(job.prompt_content ?? '')
      setDescription(job.description ?? '')
    }
  }, [job])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!job) return
    const payload: ScheduledJobUpdate = {}
    if (name.trim() !== (job.name ?? '')) payload.name = name.trim()
    if (cron.trim() !== (job.cron_expression ?? '')) payload.cron_expression = cron.trim()
    if (timezone.trim() !== (job.timezone ?? 'UTC')) payload.timezone = timezone.trim()
    if (prompt.trim() !== (job.prompt_content ?? '')) payload.prompt_content = prompt.trim()
    if (description.trim() !== (job.description ?? '')) payload.description = description.trim() || undefined
    onSave(payload)
  }

  return (
    <Dialog open={!!job} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('aiAgents.editTitle')}</DialogTitle>
          <DialogDescription className="sr-only">{t('aiAgents.subtitle')}</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="ej-name">{t('aiAgents.name')} *</Label>
            <Input
              id="ej-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('aiAgents.namePlaceholder')}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ej-cron">
              {t('aiAgents.cron')} *{' '}
              <span className="text-xs font-normal text-muted-foreground">
                (e.g. <code>0 9 * * 1-5</code>)
              </span>
            </Label>
            <Input
              id="ej-cron"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 9 * * 1-5"
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ej-tz">{t('aiAgents.timezone')}</Label>
            <Input
              id="ej-tz"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              placeholder="UTC"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ej-prompt">{t('aiAgents.promptContent')} *</Label>
            <Textarea
              id="ej-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t('aiAgents.promptPlaceholder')}
              rows={4}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ej-desc">{t('aiAgents.description')} <span className="text-muted-foreground text-xs">({t('common.optional')})</span></Label>
            <Input
              id="ej-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('aiAgents.descriptionPlaceholder')}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={isPending || !name.trim() || !cron.trim() || !prompt.trim()}>
              {isPending ? t('common.loading') : t('common.save')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
