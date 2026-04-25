import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Shield,
  CheckCircle2,
  XCircle,
  Play,
  Pause,
  Trash2,
  Plus,
  ChevronRight,
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
import { useAuth } from '@/contexts/AuthContext'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import {
  listApprovalRules,
  getApprovalRule,
  createApprovalRule,
  updateApprovalRule,
  deleteApprovalRule,
  activateApprovalRule,
  deactivateApprovalRule,
  listOrgMembers,
  type ApprovalRule,
  type ApprovalRuleCreate,
  type ApprovalRuleUpdate,
  type OrgMember,
} from '@/api/approvalRules'

const ACTIVE_FILTERS = [
  { value: '', label: 'filter.all' },
  { value: 'true', label: 'filter.active' },
  { value: 'false', label: 'filter.inactive' },
] as const

export default function ApprovalRulesPage() {
  const { t } = useTranslation()
  const { orgId, hasPermission, user } = useAuth()
  const queryClient = useQueryClient()

  const [page, setPage] = useState(1)
  const [activeFilter, setActiveFilter] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editRule, setEditRule] = useState<ApprovalRule | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  const currentMembership = user?.memberships.find((m) => m.org_id === orgId)

  const deptName = (deptIdPattern: string): string => {
    if (deptIdPattern === '*') return t('approvalRules.allDepts')
    const dept = currentMembership?.departments?.find((d) => d.dept_id === deptIdPattern)
    return dept ? dept.dept_name : deptIdPattern.slice(0, 8) + '…'
  }

  // ── Members query (for approver dropdown) ────────────────────────────
  const { data: members = [] } = useQuery({
    queryKey: ['org-members', orgId],
    queryFn: () => listOrgMembers(orgId!),
    enabled: !!orgId,
    staleTime: 1000 * 60 * 5, // 5 min
  })

  // ── List query ───────────────────────────────────────────────────────
  const { data, isLoading } = useQuery({
    queryKey: ['approval-rules', orgId, page, activeFilter],
    queryFn: () =>
      listApprovalRules(
        orgId!,
        page,
        20,
        activeFilter === '' ? undefined : activeFilter === 'true',
      ),
    enabled: !!orgId,
  })

  // ── Detail query ─────────────────────────────────────────────────────
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['approval-rule-detail', orgId, selectedId],
    queryFn: () => getApprovalRule(orgId!, selectedId!),
    enabled: !!orgId && !!selectedId,
  })

  // ── Mutations ────────────────────────────────────────────────────────
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['approval-rules', orgId] })

  const activateMut = useMutation({
    mutationFn: (id: string) => activateApprovalRule(orgId!, id),
    onSuccess: () => { invalidate(); toast.success(t('approvalRules.activated')) },
    onError: () => toast.error(t('common.error')),
  })

  const deactivateMut = useMutation({
    mutationFn: (id: string) => deactivateApprovalRule(orgId!, id),
    onSuccess: () => { invalidate(); toast.success(t('approvalRules.deactivated')) },
    onError: () => toast.error(t('common.error')),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteApprovalRule(orgId!, id),
    onSuccess: () => {
      invalidate()
      setSelectedId(null)
      toast.success(t('approvalRules.deleted'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const memberName = (userId: string) => {
    const m = members.find((m) => m.user_id === userId)
    return m ? m.full_name : userId.slice(0, 8) + '…'
  }

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-5xl mx-auto">
        {/* Header */}
        <div className="mb-6 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">{t('approvalRules.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('approvalRules.subtitle')}</p>
          </div>
          {hasPermission('approval_rules:write') && (
            <Button onClick={() => setCreateOpen(true)} size="sm" className="shrink-0">
              <Plus className="h-4 w-4 me-1.5" />
              {t('approvalRules.newRule')}
            </Button>
          )}
        </div>

        {/* Filters */}
        <div className="flex flex-wrap gap-2 mb-4">
          {ACTIVE_FILTERS.map(({ value, label }) => (
            <Button
              key={value || 'all'}
              variant={activeFilter === value ? 'secondary' : 'outline'}
              size="sm"
              onClick={() => { setActiveFilter(value); setPage(1) }}
            >
              {t(`approvalRules.${label}`)}
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
              <Shield className="h-10 w-10 opacity-40" />
              <p className="text-sm">{t('approvalRules.noRules')}</p>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-2">
            {(data?.items ?? []).map((rule) => (
              <RuleRow
                key={rule.id}
                rule={rule}
                memberName={memberName}
                deptName={deptName}
                onClick={() => setSelectedId(rule.id)}
                onActivate={() => activateMut.mutate(rule.id)}
                onDeactivate={() => deactivateMut.mutate(rule.id)}
                onEdit={() => setEditRule(rule)}
                canWrite={hasPermission('approval_rules:write')}
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
            <DialogTitle>{t('approvalRules.detailTitle')}</DialogTitle>
            <DialogDescription className="sr-only">{t('approvalRules.subtitle')}</DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <div className="space-y-2 py-4">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          ) : detail ? (
            <RuleDetail
              rule={detail}
              memberName={memberName}
              deptName={deptName}
              onActivate={() => activateMut.mutate(detail.id)}
              onDeactivate={() => deactivateMut.mutate(detail.id)}
              onEdit={() => { setSelectedId(null); setEditRule(detail) }}
              onDelete={() => deleteMut.mutate(detail.id)}
              loading={activateMut.isPending || deactivateMut.isPending || deleteMut.isPending}
              canWrite={hasPermission('approval_rules:write')}
            />
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Create dialog */}
      <RuleFormDialog
        open={createOpen}
        mode="create"
        members={members}
        onClose={() => setCreateOpen(false)}
        orgId={orgId!}
        onSaved={() => {
          invalidate()
          setCreateOpen(false)
        }}
      />

      {/* Edit dialog */}
      <RuleFormDialog
        open={!!editRule}
        mode="edit"
        rule={editRule ?? undefined}
        members={members}
        onClose={() => setEditRule(null)}
        orgId={orgId!}
        onSaved={() => {
          invalidate()
          setEditRule(null)
        }}
      />
    </div>
  )
}

// ── Sub-components ──────────────────────────────────────────────────────────

function RuleRow({
  rule,
  memberName,
  deptName,
  onClick,
  onActivate,
  onDeactivate,
  onEdit,
  canWrite = false,
}: {
  rule: ApprovalRule
  memberName: (id: string) => string
  deptName: (pattern: string) => string
  onClick: () => void
  onActivate: () => void
  onDeactivate: () => void
  onEdit: () => void
  canWrite?: boolean
}) {
  const { t } = useTranslation()

  return (
    <Card
      className="cursor-pointer hover:shadow-sm transition-shadow group"
      onClick={onClick}
    >
      <CardContent className="p-4 flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <code className="font-medium text-sm truncate max-w-[220px] bg-muted rounded px-1">
              {rule.tool_pattern}
            </code>
            <Badge variant={rule.is_active ? 'success' : 'secondary'} className="text-xs shrink-0">
              {rule.is_active ? t('approvalRules.active') : t('approvalRules.inactive')}
            </Badge>
            {rule.priority > 0 && (
              <Badge variant="outline" className="text-xs shrink-0">
                priority {rule.priority}
              </Badge>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t('approvalRules.deptPattern')}:{' '}
            <span className="font-mono">{deptName(rule.dept_id_pattern)}</span>
            {' · '}
            {t('approvalRules.userPattern')}:{' '}
            <span className="font-mono">{rule.user_id_pattern}</span>
            {' · '}
            {t('approvalRules.approver')}:{' '}
            <span className="font-medium">{memberName(rule.approver_user_id)}</span>
          </p>
        </div>
        {canWrite && (
          <div className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              title={t('common.edit')}
              onClick={onEdit}
            >
              <Pencil className="h-3.5 w-3.5" />
            </Button>
            {rule.is_active ? (
              <Button variant="ghost" size="icon" className="h-7 w-7" title={t('approvalRules.deactivate')} onClick={onDeactivate}>
                <Pause className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button variant="ghost" size="icon" className="h-7 w-7" title={t('approvalRules.activate')} onClick={onActivate}>
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

function RuleDetail({
  rule,
  memberName,
  deptName,
  onActivate,
  onDeactivate,
  onEdit,
  onDelete,
  loading,
  canWrite = false,
}: {
  rule: ApprovalRule
  memberName: (id: string) => string
  deptName: (pattern: string) => string
  onActivate: () => void
  onDeactivate: () => void
  onEdit: () => void
  onDelete: () => void
  loading: boolean
  canWrite?: boolean
}) {
  const { t } = useTranslation()
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <DetailRow label={t('approvalRules.toolPattern')} value={<code className="bg-muted rounded px-1">{rule.tool_pattern}</code>} />
        <DetailRow label={t('approvalRules.deptPattern')} value={<code className="bg-muted rounded px-1">{deptName(rule.dept_id_pattern)}</code>} />
        <DetailRow label={t('approvalRules.userPattern')} value={<code className="bg-muted rounded px-1">{rule.user_id_pattern}</code>} />
        <DetailRow label={t('approvalRules.approver')} value={memberName(rule.approver_user_id)} />
        <DetailRow label={t('approvalRules.priority')} value={String(rule.priority)} />
        <DetailRow label={t('approvalRules.status')} value={
          <Badge variant={rule.is_active ? 'success' : 'secondary'}>
            {rule.is_active ? t('approvalRules.active') : t('approvalRules.inactive')}
          </Badge>
        } />
        {rule.created_at && (
          <DetailRow label="Created" value={new Date(rule.created_at).toLocaleString()} />
        )}
      </div>

      {rule.description && (
        <div>
          <p className="text-xs font-medium text-muted-foreground mb-1">{t('approvalRules.description')}</p>
          <p className="text-sm">{rule.description}</p>
        </div>
      )}

      {canWrite && (
        <DialogFooter className="gap-2 pt-2">
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5 me-1.5" />
            {t('common.edit')}
          </Button>
          {rule.is_active ? (
            <Button variant="outline" size="sm" disabled={loading} onClick={onDeactivate}>
              <Pause className="h-3.5 w-3.5 me-1.5" />
              {t('approvalRules.deactivate')}
            </Button>
          ) : (
            <Button variant="outline" size="sm" disabled={loading} onClick={onActivate}>
              <Play className="h-3.5 w-3.5 me-1.5" />
              {t('approvalRules.activate')}
            </Button>
          )}
          {confirmDelete ? (
            <Button variant="destructive" size="sm" disabled={loading} onClick={onDelete}>
              {t('approvalRules.confirmDelete')}
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

function RuleFormDialog({
  open,
  mode,
  rule,
  members,
  onClose,
  orgId,
  onSaved,
}: {
  open: boolean
  mode: 'create' | 'edit'
  rule?: ApprovalRule
  members: OrgMember[]
  onClose: () => void
  orgId: string
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { user } = useAuth()

  const currentMembership = user?.memberships.find((m) => m.org_id === orgId)
  const orgRole = currentMembership?.role ?? ''
  const isOrgAdmin = orgRole === 'owner' || orgRole === 'admin'
  const adminDepts = currentMembership?.departments?.filter((d) => d.role === 'admin' && d.is_active) ?? []

  const defaultDeptPattern = rule?.dept_id_pattern ?? (isOrgAdmin ? '*' : (adminDepts[0]?.dept_id ?? '*'))

  const [toolPattern, setToolPattern] = useState(rule?.tool_pattern ?? '')
  const [userIdPattern, setUserIdPattern] = useState(rule?.user_id_pattern ?? '*')
  const [deptIdPattern, setDeptIdPattern] = useState(defaultDeptPattern)
  const [approverUserId, setApproverUserId] = useState(rule?.approver_user_id ?? '')
  const [priority, setPriority] = useState(String(rule?.priority ?? 0))
  const [description, setDescription] = useState(rule?.description ?? '')

  // Reset form when dialog opens with new data
  const handleOpenChange = (isOpen: boolean) => {
    if (!isOpen) {
      onClose()
    } else {
      setToolPattern(rule?.tool_pattern ?? '')
      setUserIdPattern(rule?.user_id_pattern ?? '*')
      setDeptIdPattern(rule?.dept_id_pattern ?? (isOrgAdmin ? '*' : (adminDepts[0]?.dept_id ?? '*')))
      setApproverUserId(rule?.approver_user_id ?? '')
      setPriority(String(rule?.priority ?? 0))
      setDescription(rule?.description ?? '')
    }
  }

  const createMut = useMutation({
    mutationFn: (payload: ApprovalRuleCreate) => createApprovalRule(orgId, payload),
    onSuccess: () => {
      toast.success(t('approvalRules.created'))
      onSaved()
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail ?? t('common.error')
      toast.error(String(detail))
    },
  })

  const updateMut = useMutation({
    mutationFn: (payload: ApprovalRuleUpdate) => updateApprovalRule(orgId, rule!.id, payload),
    onSuccess: () => {
      toast.success(t('approvalRules.updated'))
      onSaved()
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail ?? t('common.error')
      toast.error(String(detail))
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!toolPattern.trim() || !approverUserId) return
    const payload = {
      tool_pattern: toolPattern.trim(),
      user_id_pattern: userIdPattern || '*',
      dept_id_pattern: deptIdPattern || '*',
      approver_user_id: approverUserId,
      priority: parseInt(priority, 10) || 0,
      description: description.trim() || undefined,
    }
    if (mode === 'create') {
      createMut.mutate(payload)
    } else {
      updateMut.mutate(payload)
    }
  }

  const isPending = createMut.isPending || updateMut.isPending

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {mode === 'create' ? t('approvalRules.createTitle') : t('approvalRules.editTitle')}
          </DialogTitle>
          <DialogDescription className="sr-only">{t('approvalRules.subtitle')}</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="ar-tool">
              {t('approvalRules.toolPattern')} *
            </Label>
            <Input
              id="ar-tool"
              value={toolPattern}
              onChange={(e) => setToolPattern(e.target.value)}
              placeholder={t('approvalRules.toolPatternPlaceholder')}
              required
            />
            <p className="text-xs text-muted-foreground">{t('approvalRules.toolPatternHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ar-user">{t('approvalRules.userPattern')}</Label>
            <Select value={userIdPattern} onValueChange={setUserIdPattern}>
              <SelectTrigger id="ar-user">
                <SelectValue placeholder={t('approvalRules.allUsers')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="*">{t('approvalRules.allUsers')}</SelectItem>
                {members.map((m) => (
                  <SelectItem key={m.user_id} value={m.user_id}>
                    {m.full_name} ({m.email})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('approvalRules.userPatternHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ar-dept">{t('approvalRules.deptPattern')}</Label>
            <Select value={deptIdPattern} onValueChange={setDeptIdPattern}>
              <SelectTrigger id="ar-dept">
                <SelectValue placeholder={t('approvalRules.allDepts')} />
              </SelectTrigger>
              <SelectContent>
                {isOrgAdmin && (
                  <SelectItem value="*">{t('approvalRules.allDepts')}</SelectItem>
                )}
                {adminDepts.map((d) => (
                  <SelectItem key={d.dept_id} value={d.dept_id}>
                    {d.dept_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('approvalRules.deptPatternHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ar-approver">{t('approvalRules.approver')} *</Label>
            <Select value={approverUserId} onValueChange={setApproverUserId} required>
              <SelectTrigger id="ar-approver">
                <SelectValue placeholder={t('approvalRules.approverPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {members.map((m) => (
                  <SelectItem key={m.user_id} value={m.user_id}>
                    {m.full_name} — {m.role}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ar-priority">{t('approvalRules.priority')}</Label>
            <Input
              id="ar-priority"
              type="number"
              min={0}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">{t('approvalRules.priorityHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ar-desc">{t('approvalRules.description')}</Label>
            <Textarea
              id="ar-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('approvalRules.descriptionPlaceholder')}
              rows={2}
            />
          </div>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={isPending || !toolPattern.trim() || !approverUserId}>
              {mode === 'create' ? t('approvalRules.newRule') : t('common.save')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
