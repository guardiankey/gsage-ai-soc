import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, FolderTree, X, Search } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import {
  listDepartments,
  createDepartment,
  updateDepartment,
  deleteDepartment,
  listDeptMembers,
  addDeptMember,
  updateDeptMember,
  removeDeptMember,
  listAdminUsers,
  type DepartmentOut,
  type DepartmentCreate,
  type DepartmentUpdate,
  type DeptMemberOut,
  type DeptMemberAdd,
  type DeptMemberUpdate,
  type AdminUserOut,
} from '@/api/admin'

const DEPT_ROLES = ['admin', 'member', 'viewer'] as const
type DeptRole = (typeof DEPT_ROLES)[number]

export default function DepartmentsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [detail, setDetail] = useState<DepartmentOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<DepartmentOut | null>(null)

  // ── Queries ──────────────────────────────────────────────────────────────

  const { data: depts = [], isLoading } = useQuery({
    queryKey: ['admin', 'depts', orgId],
    queryFn: () => listDepartments(orgId!, { include_inactive: true }),
    enabled: !!orgId,
  })

  // ── Mutations ─────────────────────────────────────────────────────────────

  const muCreate = useMutation({
    mutationFn: (p: DepartmentCreate) => createDepartment(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.depts.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DepartmentUpdate }) =>
      updateDepartment(orgId!, id, payload),
    onSuccess: (updated) => {
      toast.success(t('admin.depts.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId] })
      setDetail((prev) => (prev?.id === updated.id ? updated : prev))
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteDepartment(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.depts.deleted'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <FolderTree className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.depts.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.depts.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.depts.add')}
        </Button>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.depts.name')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.depts.slug')}</th>
                <th className="text-left px-4 py-3 font-medium hidden md:table-cell">
                  {t('admin.depts.description')}
                </th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.depts.status')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {depts.map((dept) => (
                <tr
                  key={dept.id}
                  className="hover:bg-muted/30 transition-colors cursor-pointer"
                  onClick={() => setDetail(dept)}
                >
                  <td className="px-4 py-3 font-medium">
                    <span>{dept.name}</span>
                    {dept.is_default && (
                      <Badge variant="secondary" className="ml-2 text-xs">
                        {t('admin.depts.default')}
                      </Badge>
                    )}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground font-mono text-xs">
                    {dept.slug}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground hidden md:table-cell max-w-xs truncate">
                    {dept.description ?? '—'}
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={dept.is_active ? 'default' : 'secondary'}>
                      {dept.is_active
                        ? t('approvalRules.active')
                        : t('approvalRules.inactive')}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    <div
                      className="flex items-center gap-1 justify-end"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => setDetail(dept)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-destructive"
                        disabled={dept.is_default}
                        onClick={() => setDeleteTarget(dept)}
                        title={
                          dept.is_default
                            ? t('admin.depts.cannotDeleteDefault')
                            : undefined
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {depts.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">
                    {t('common.noResults')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Create dialog ── */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.depts.createTitle')}</DialogTitle>
          </DialogHeader>
          <DeptCreateForm
            onSubmit={(p) => muCreate.mutate(p)}
            onCancel={() => setCreateOpen(false)}
            isLoading={muCreate.isPending}
          />
        </DialogContent>
      </Dialog>

      {/* ── Detail / edit dialog ── */}
      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('admin.depts.editTitle')}</DialogTitle>
          </DialogHeader>
          {detail && (
            <DeptDetailPanel
              dept={detail}
              orgId={orgId!}
              onUpdate={(payload) => muUpdate.mutate({ id: detail.id, payload })}
              isUpdating={muUpdate.isPending}
              onClose={() => setDetail(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* ── Delete confirmation ── */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.depts.deleteTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.depts.deleteDesc', { name: deleteTarget?.name })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              disabled={muDelete.isPending}
              onClick={() => deleteTarget && muDelete.mutate(deleteTarget.id)}
            >
              {t('common.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ─── Create form ────────────────────────────────────────────────────────────

function DeptCreateForm({
  onSubmit,
  onCancel,
  isLoading,
}: {
  onSubmit: (p: DepartmentCreate) => void
  onCancel: () => void
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<DepartmentCreate>({ name: '', slug: '', description: '' })

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit({
          name: form.name,
          slug: form.slug?.trim() || undefined,
          description: form.description?.trim() || null,
        })
      }}
      className="space-y-4"
    >
      <div className="space-y-1.5">
        <Label>{t('admin.depts.name')} *</Label>
        <Input
          value={form.name}
          onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
          required
          autoFocus
        />
      </div>
      <div className="space-y-1.5">
        <Label>
          {t('admin.depts.slug')}{' '}
          <span className="text-muted-foreground text-xs">({t('common.optional')})</span>
        </Label>
        <Input
          value={form.slug ?? ''}
          placeholder="auto-generated"
          onChange={(e) => setForm((p) => ({ ...p, slug: e.target.value.toLowerCase() }))}
        />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.depts.description')}</Label>
        <Textarea
          value={form.description ?? ''}
          rows={2}
          onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
        />
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>
          {t('common.cancel')}
        </Button>
        <Button type="submit" disabled={isLoading}>
          {isLoading ? t('common.loading') : t('common.create')}
        </Button>
      </DialogFooter>
    </form>
  )
}

// ─── Detail panel (edit + members) ──────────────────────────────────────────

function DeptDetailPanel({
  dept,
  orgId,
  onUpdate,
  isUpdating,
  onClose,
}: {
  dept: DepartmentOut
  orgId: string
  onUpdate: (p: DepartmentUpdate) => void
  isUpdating: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<'settings' | 'members'>('settings')
  const [editForm, setEditForm] = useState<DepartmentUpdate>({
    name: dept.name,
    slug: dept.slug,
    description: dept.description ?? '',
    is_active: dept.is_active,
  })
  const [memberSearch, setMemberSearch] = useState('')
  const [addingUserId, setAddingUserId] = useState<string>('')
  const [addingRole, setAddingRole] = useState<DeptRole>('member')

  // Members query
  const { data: members = [], isLoading: membersLoading } = useQuery({
    queryKey: ['admin', 'depts', orgId, dept.id, 'members'],
    queryFn: () => listDeptMembers(orgId, dept.id),
    enabled: tab === 'members',
  })

  // User search query for "add member"
  const { data: usersData } = useQuery({
    queryKey: ['admin', 'users', orgId, 1, memberSearch],
    queryFn: () => listAdminUsers(orgId, { page: 1, limit: 20, search: memberSearch || undefined }),
    enabled: tab === 'members' && memberSearch.length >= 1,
  })
  const allUsers: AdminUserOut[] = usersData?.items ?? []

  // Add member
  const muAdd = useMutation({
    mutationFn: (p: DeptMemberAdd) => addDeptMember(orgId, dept.id, p),
    onSuccess: () => {
      toast.success(t('admin.depts.memberAdded'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId, dept.id, 'members'] })
      setAddingUserId('')
      setMemberSearch('')
    },
    onError: () => toast.error(t('common.error')),
  })

  // Update member role
  const muUpdateMember = useMutation({
    mutationFn: ({ userId, payload }: { userId: string; payload: DeptMemberUpdate }) =>
      updateDeptMember(orgId, dept.id, userId, payload),
    onSuccess: () => {
      toast.success(t('admin.depts.memberUpdated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId, dept.id, 'members'] })
    },
    onError: () => toast.error(t('common.error')),
  })

  // Remove member
  const muRemove = useMutation({
    mutationFn: (userId: string) => removeDeptMember(orgId, dept.id, userId),
    onSuccess: () => {
      toast.success(t('admin.depts.memberRemoved'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'depts', orgId, dept.id, 'members'] })
    },
    onError: () => toast.error(t('common.error')),
  })

  function handleAddMember() {
    if (!addingUserId) return
    muAdd.mutate({ user_id: addingUserId, role: addingRole })
  }

  // Filter users not already in the dept
  const existingUserIds = new Set(members.map((m) => m.user_id))
  const availableUsers = allUsers.filter((u) => !existingUserIds.has(u.id))

  return (
    <div className="space-y-4">
      {/* Tab switcher */}
      <div className="flex gap-1 border-b">
        {(['settings', 'members'] as const).map((t_key) => (
          <button
            key={t_key}
            type="button"
            onClick={() => setTab(t_key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              tab === t_key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            {t_key === 'settings' ? t('admin.depts.editTitle') : t('admin.depts.members')}
          </button>
        ))}
      </div>

      {/* ── Settings tab ── */}
      {tab === 'settings' && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            onUpdate({
              name: editForm.name,
              slug: editForm.slug?.trim() || undefined,
              description: editForm.description?.trim() || null,
              is_active: editForm.is_active,
            })
          }}
          className="space-y-4"
        >
          <div className="space-y-1.5">
            <Label>{t('admin.depts.name')}</Label>
            <Input
              value={editForm.name ?? ''}
              onChange={(e) => setEditForm((p) => ({ ...p, name: e.target.value }))}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin.depts.slug')}</Label>
            <Input
              value={editForm.slug ?? ''}
              onChange={(e) =>
                setEditForm((p) => ({ ...p, slug: e.target.value.toLowerCase() }))
              }
              disabled={dept.is_default}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin.depts.description')}</Label>
            <Textarea
              value={editForm.description ?? ''}
              rows={2}
              onChange={(e) => setEditForm((p) => ({ ...p, description: e.target.value }))}
            />
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="dept_is_active"
              checked={editForm.is_active ?? true}
              onChange={(e) => setEditForm((p) => ({ ...p, is_active: e.target.checked }))}
              disabled={dept.is_default}
            />
            <Label htmlFor="dept_is_active">{t('admin.depts.isActive')}</Label>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t('common.close')}
            </Button>
            <Button type="submit" disabled={isUpdating}>
              {isUpdating ? t('common.loading') : t('common.save')}
            </Button>
          </DialogFooter>
        </form>
      )}

      {/* ── Members tab ── */}
      {tab === 'members' && (
        <div className="space-y-4">
          {/* Add member row */}
          <div className="flex gap-2 items-end">
            <div className="flex-1 space-y-1.5">
              <Label>{t('admin.depts.addMember')}</Label>
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  className="pl-9"
                  placeholder={t('admin.depts.searchUsers')}
                  value={memberSearch}
                  onChange={(e) => {
                    setMemberSearch(e.target.value)
                    setAddingUserId('')
                  }}
                />
              </div>
              {availableUsers.length > 0 && (
                <div className="border rounded-md bg-background shadow-sm divide-y max-h-40 overflow-y-auto">
                  {availableUsers.map((u) => (
                    <button
                      key={u.id}
                      type="button"
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-muted/50 transition-colors ${
                        addingUserId === u.id ? 'bg-muted' : ''
                      }`}
                      onClick={() => {
                        setAddingUserId(u.id)
                        setMemberSearch(`${u.full_name} <${u.email}>`)
                      }}
                    >
                      <span className="font-medium">{u.full_name}</span>{' '}
                      <span className="text-muted-foreground">{u.email}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="space-y-1.5">
              <Label>{t('admin.depts.memberRole')}</Label>
              <Select
                value={addingRole}
                onValueChange={(v) => setAddingRole(v as DeptRole)}
              >
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DEPT_ROLES.map((r) => (
                    <SelectItem key={r} value={r}>
                      {t(`admin.depts.role${r.charAt(0).toUpperCase()}${r.slice(1)}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              type="button"
              size="sm"
              disabled={!addingUserId || muAdd.isPending}
              onClick={handleAddMember}
            >
              <Plus className="h-4 w-4" />
            </Button>
          </div>

          {/* Members list */}
          {membersLoading ? (
            <div className="space-y-2">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : members.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              {t('admin.depts.noMembers')}
            </p>
          ) : (
            <div className="rounded-md border divide-y">
              {members.map((m: DeptMemberOut) => (
                <div key={m.id} className="flex items-center gap-3 px-3 py-2">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate">
                      {m.user_full_name ?? m.user_email ?? m.user_id}
                    </p>
                    <p className="text-xs text-muted-foreground truncate">{m.user_email}</p>
                  </div>
                  <Select
                    value={m.role}
                    onValueChange={(v) =>
                      muUpdateMember.mutate({
                        userId: m.user_id,
                        payload: { role: v },
                      })
                    }
                  >
                    <SelectTrigger className="w-24 h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {DEPT_ROLES.map((r) => (
                        <SelectItem key={r} value={r} className="text-xs">
                          {t(`admin.depts.role${r.charAt(0).toUpperCase()}${r.slice(1)}`)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-muted-foreground hover:text-destructive"
                    onClick={() => muRemove.mutate(m.user_id)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t('common.close')}
            </Button>
          </DialogFooter>
        </div>
      )}
    </div>
  )
}
