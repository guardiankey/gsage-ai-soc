import { useState, useMemo, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Layers, X } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import {
  listGroups,
  createGroup,
  getGroup,
  updateGroup,
  deleteGroup,
  updateGroupMembers,
  updateGroupPermissions,
  listPermissions,
  listDepartments,
  listAdminUsers,
  type GroupOut,
  type GroupDetail,
  type GroupCreate,
  type GroupUpdate,
  type PermissionOut,
  type AdminUserOut,
  type DepartmentOut,
  type GroupPermissionEntry,
} from '@/api/admin'

export default function GroupsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [detail, setDetail] = useState<GroupDetail | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<GroupOut | null>(null)

  const { data: groups, isLoading } = useQuery({
    queryKey: ['admin', 'groups', orgId],
    queryFn: () => listGroups(orgId!),
    enabled: !!orgId,
  })

  const { data: allPermissions = [] } = useQuery({
    queryKey: ['admin', 'permissions', orgId],
    queryFn: () => listPermissions(orgId!),
    enabled: !!orgId,
  })

  const { data: departments = [] } = useQuery({
    queryKey: ['admin', 'departments', orgId],
    queryFn: () => listDepartments(orgId!),
    enabled: !!orgId,
  })

  const muCreate = useMutation({
    mutationFn: (p: GroupCreate) => createGroup(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.groups.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'groups', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: GroupUpdate }) =>
      updateGroup(orgId!, id, payload),
    onSuccess: (updated) => {
      toast.success(t('admin.groups.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'groups', orgId] })
      if (detail && detail.id === updated.id) {
        setDetail((prev) => prev ? { ...prev, ...updated } : null)
      }
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteGroup(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.groups.deleted'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'groups', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muPerms = useMutation({
    mutationFn: ({ id, permEntries }: { id: string; permEntries: GroupPermissionEntry[] }) =>
      updateGroupPermissions(orgId!, id, permEntries),
    onSuccess: () => {
      toast.success(t('admin.groups.permissionsUpdated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'groups', orgId] })
      setDetail(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muMembers = useMutation({
    mutationFn: ({ id, userIds }: { id: string; userIds: string[] }) =>
      updateGroupMembers(orgId!, id, userIds),
    onSuccess: () => {
      toast.success(t('admin.groups.membersUpdated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'groups', orgId] })
      setDetail(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const openDetail = async (group: GroupOut) => {
    const d = await getGroup(orgId!, group.id)
    setDetail(d)
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Layers className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.groups.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.groups.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.groups.add')}
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[...Array(4)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.groups.name')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.groups.members')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.groups.permissions')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {groups?.map((g) => {
                const managed = g.name.startsWith('_tpl:')
                return (
                <tr key={g.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">
                    <button
                      className="font-medium hover:underline text-left"
                      onClick={() => openDetail(g)}
                    >
                      {g.name}
                    </button>
                    {managed && (
                      <Badge variant="secondary" className="ml-2 text-xs" title={t('admin.groups.managedBySsoHint')}>
                        {t('admin.groups.managedBySso')}
                      </Badge>
                    )}
                    {g.description && (
                      <p className="text-xs text-muted-foreground mt-0.5">{g.description}</p>
                    )}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">{g.member_count}</td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {g.permission_tags.slice(0, 3).map((tag) => (
                        <Badge key={tag} variant="outline" className="text-xs">{tag}</Badge>
                      ))}
                      {g.permission_tags.length > 3 && (
                        <Badge variant="outline" className="text-xs">+{g.permission_tags.length - 3}</Badge>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => openDetail(g)}
                        disabled={managed}
                        title={managed ? t('admin.groups.managedBySsoHint') : undefined}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-destructive"
                        onClick={() => setDeleteTarget(g)}
                        disabled={managed}
                        title={managed ? t('admin.groups.managedBySsoHint') : undefined}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
                )
              })}
              {groups?.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">{t('common.noResults')}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.groups.createTitle')}</DialogTitle>
          </DialogHeader>
          <GroupForm onSubmit={(p) => muCreate.mutate(p)} onCancel={() => setCreateOpen(false)} isLoading={muCreate.isPending} />
        </DialogContent>
      </Dialog>

      {/* Detail / edit dialog */}
      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{detail?.name}</DialogTitle>
          </DialogHeader>
          {detail && (
            <GroupDetailPanel
              detail={detail}
              allPermissions={allPermissions}
              departments={departments}
              orgId={orgId!}
              onSave={(permEntries, memberIds) => {
                muMembers.mutate({ id: detail.id, userIds: memberIds }, {
                  onSuccess: () => muPerms.mutate({ id: detail.id, permEntries }),
                })
              }}
              onCancel={() => setDetail(null)}
              isUpdating={muPerms.isPending || muMembers.isPending || muUpdate.isPending}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.groups.deleteTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.groups.deleteDesc', { name: deleteTarget?.name })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>{t('common.cancel')}</Button>
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

function GroupForm({ onSubmit, onCancel, initial, isLoading }: {
  onSubmit: (p: GroupCreate) => void
  onCancel: () => void
  initial?: GroupUpdate
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<GroupCreate>({ name: initial?.name ?? '', description: initial?.description })
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(form) }} className="space-y-4">
      <div className="space-y-1.5">
        <Label>{t('admin.groups.name')}</Label>
        <Input value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} required />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.groups.description')}</Label>
        <Input value={form.description ?? ''} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} />
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.create')}</Button>
      </DialogFooter>
    </form>
  )
}

function GroupDetailPanel({ detail, allPermissions, departments, orgId, onSave, onCancel, isUpdating }: {
  detail: GroupDetail
  allPermissions: PermissionOut[]
  departments: DepartmentOut[]
  orgId: string
  onSave: (permEntries: GroupPermissionEntry[], memberIds: string[]) => void
  onCancel: () => void
  isUpdating: boolean
}) {
  const { t } = useTranslation()
  // State: list of {permission_id, dept_id} entries (one per permission slot)
  const [permEntries, setPermEntries] = useState<GroupPermissionEntry[]>(
    detail.permissions.map((p) => ({ permission_id: p.id, dept_id: p.dept_id }))
  )
  // Selected dept scope for adding new permissions (__org__ = global/null)
  const [scopeDeptId, setScopeDeptId] = useState<string>('__org__')
  const [memberIds, setMemberIds] = useState<Set<string>>(
    new Set(detail.members.map((m) => m.user_id))
  )
  // Map of userId -> {email, full_name} for all users ever seen (initial members + search picks)
  const [memberInfo, setMemberInfo] = useState<Map<string, { email: string; full_name: string }>>(
    () => new Map(detail.members.map((m) => [m.user_id, { email: m.email, full_name: m.full_name }]))
  )
  const [memberSearch, setMemberSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(memberSearch.trim()), 300)
    return () => clearTimeout(timer)
  }, [memberSearch])

  const { data: searchResults, isFetching: isSearching } = useQuery({
    queryKey: ['admin', 'users', orgId, 'search', debouncedSearch],
    queryFn: () => listAdminUsers(orgId, { page: 1, limit: 100, search: debouncedSearch }),
    enabled: debouncedSearch.length > 0,
    staleTime: 30_000,
  })

  const togglePerm = (id: string) => {
    const deptId = scopeDeptId === '__org__' ? null : scopeDeptId
    setPermEntries((prev) => {
      // Check if this exact (permission_id, dept_id) combo already exists
      const exists = prev.some(
        (e) => e.permission_id === id && e.dept_id === deptId
      )
      if (exists) {
        return prev.filter((e) => !(e.permission_id === id && e.dept_id === deptId))
      }
      return [...prev, { permission_id: id, dept_id: deptId }]
    })
  }

  const isPermChecked = (id: string): boolean => {
    const deptId = scopeDeptId === '__org__' ? null : scopeDeptId
    return permEntries.some((e) => e.permission_id === id && e.dept_id === deptId)
  }

  const addMember = (user: AdminUserOut) => {
    setMemberIds((prev) => new Set([...prev, user.id]))
    setMemberInfo((prev) => new Map([...prev, [user.id, { email: user.email, full_name: user.full_name }]]))
    setMemberSearch('')
  }

  const removeMember = (userId: string) => {
    setMemberIds((prev) => {
      const next = new Set(prev)
      next.delete(userId)
      return next
    })
  }

  const categories = [...new Set(allPermissions.map((p) => p.category))].sort()

  const dropdownUsers = useMemo(() => {
    if (!debouncedSearch) return []
    return (searchResults?.items ?? []).filter((u) => !memberIds.has(u.id))
  }, [searchResults, memberIds, debouncedSearch])

  return (
    <div className="space-y-5">
      {/* Members */}
      <div>
        <p className="text-sm font-medium mb-2">{t('admin.groups.membersList')} ({memberIds.size})</p>
        {/* Current members as removable badges */}
        <div className="flex flex-wrap gap-1.5 mb-3">
          {[...memberIds].map((userId) => {
            const info = memberInfo.get(userId)
            return (
              <Badge key={userId} variant="secondary" className="text-xs flex items-center gap-1">
                {info?.email ?? userId}
                <button type="button" onClick={() => removeMember(userId)} className="ml-0.5 hover:text-destructive">
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            )
          })}
          {memberIds.size === 0 && (
            <span className="text-xs text-muted-foreground">{t('admin.groups.noMembers')}</span>
          )}
        </div>
        {/* Add member search */}
        <div className="space-y-2">
          <Input
            placeholder={t('admin.groups.addMemberPlaceholder')}
            value={memberSearch}
            onChange={(e) => setMemberSearch(e.target.value)}
            className="max-w-sm"
          />
          {memberSearch && (
            <div className="border rounded-md max-h-36 overflow-y-auto">
              {isSearching && (
                <p className="px-3 py-2 text-xs text-muted-foreground">{t('common.loading')}</p>
              )}
              {!isSearching && dropdownUsers.map((u) => (
                <button
                  key={u.id}
                  type="button"
                  className="w-full text-left px-3 py-1.5 text-sm hover:bg-muted/50 flex items-center justify-between"
                  onClick={() => addMember(u)}
                >
                  <span>{u.email}</span>
                  <span className="text-xs text-muted-foreground">{u.full_name}</span>
                </button>
              ))}
              {!isSearching && debouncedSearch && dropdownUsers.length === 0 && (
                <p className="px-3 py-2 text-xs text-muted-foreground">{t('common.noResults')}</p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Permissions */}
      <div>
        <p className="text-sm font-medium mb-2">{t('admin.groups.permissionsList')}</p>
        {/* Dept scope selector */}
        <div className="flex items-center gap-2 mb-3">
          <Label className="text-xs text-muted-foreground shrink-0">{t('admin.groups.deptScope')}</Label>
          <Select value={scopeDeptId} onValueChange={setScopeDeptId}>
            <SelectTrigger className="h-8 text-xs w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__org__">{t('admin.groups.allDepartments')}</SelectItem>
              {departments.map((d) => (
                <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="border rounded-md max-h-64 overflow-y-auto p-3 space-y-3">
          {categories.map((cat) => (
            <div key={cat}>
              <p className="text-xs font-semibold text-muted-foreground uppercase mb-1">{cat}</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                {allPermissions
                  .filter((p) => p.category === cat)
                  .map((p) => (
                    <label key={p.id} className="flex items-center gap-2 cursor-pointer hover:bg-muted/50 rounded px-1 py-0.5">
                      <input
                        type="checkbox"
                        checked={isPermChecked(p.id)}
                        onChange={() => togglePerm(p.id)}
                      />
                      <span className="text-xs font-mono text-primary">{p.tag}</span>
                      {p.description && (
                        <span className="text-xs text-muted-foreground truncate">{p.description}</span>
                      )}
                    </label>
                  ))}
              </div>
            </div>
          ))}
        </div>
        {/* Summary of entries in other scopes */}
        {permEntries.filter((e) => {
          const curDept = scopeDeptId === '__org__' ? null : scopeDeptId
          return e.dept_id !== curDept
        }).length > 0 && (
          <p className="text-xs text-muted-foreground mt-1">
            {permEntries.filter((e) => {
              const curDept = scopeDeptId === '__org__' ? null : scopeDeptId
              return e.dept_id !== curDept
            }).length} {t('admin.groups.entriesOtherScopes')}
          </p>
        )}
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button
          disabled={isUpdating}
          onClick={() => onSave(permEntries, [...memberIds])}
        >
          {isUpdating ? t('common.loading') : t('common.save')}
        </Button>
      </DialogFooter>
    </div>
  )
}
