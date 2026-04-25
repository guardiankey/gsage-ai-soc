import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, MonitorSmartphone } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import {
  listInterfaceProfiles,
  createInterfaceProfile,
  updateInterfaceProfile,
  deleteInterfaceProfile,
  listPermissions,
  listDepartments,
  listAdminUsers,
  type InterfaceProfileOut,
  type InterfaceProfileCreate,
  type InterfaceProfileUpdate,
  type PermissionOut,
  type DepartmentOut,
  type AdminUserOut,
} from '@/api/admin'

const INTERFACES = ['web', 'email', 'telegram', 'whatsapp', 'slack', 'api', 'cli']

export default function InterfacesPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<InterfaceProfileOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<InterfaceProfileOut | null>(null)

  const { data: profiles, isLoading } = useQuery({
    queryKey: ['admin', 'interfaces', orgId],
    queryFn: () => listInterfaceProfiles(orgId!),
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

  const { data: usersData } = useQuery({
    queryKey: ['admin', 'users-all', orgId],
    queryFn: () => listAdminUsers(orgId!, { limit: 200 }),
    enabled: !!orgId,
  })
  const allUsers = usersData?.items ?? []

  const muCreate = useMutation({
    mutationFn: (p: InterfaceProfileCreate) => createInterfaceProfile(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.interfaces.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'interfaces', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: InterfaceProfileUpdate }) =>
      updateInterfaceProfile(orgId!, id, payload),
    onSuccess: () => {
      toast.success(t('admin.interfaces.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'interfaces', orgId] })
      setEditTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteInterfaceProfile(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.interfaces.deleted'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'interfaces', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MonitorSmartphone className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.interfaces.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.interfaces.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.interfaces.add')}
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[...Array(3)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.interfaces.interface')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.interfaces.mode')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.interfaces.permissions')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.interfaces.status')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {profiles?.map((p) => (
                <tr key={p.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3 font-mono">{p.interface}</td>
                  <td className="px-4 py-3">
                    <Badge variant={p.mode === 'allowlist' ? 'default' : 'outline'}>{p.mode}</Badge>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {p.tool_permissions.slice(0, 3).map((tag) => (
                        <Badge key={tag} variant="secondary" className="text-xs">{tag}</Badge>
                      ))}
                      {p.tool_permissions.length > 3 && (
                        <Badge variant="secondary" className="text-xs">+{p.tool_permissions.length - 3}</Badge>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={p.is_active ? 'default' : 'secondary'}>
                      {p.is_active ? t('approvalRules.active') : t('approvalRules.inactive')}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" size="icon" onClick={() => setEditTarget(p)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="text-destructive" onClick={() => setDeleteTarget(p)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {profiles?.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">{t('common.noResults')}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('admin.interfaces.createTitle')}</DialogTitle>
          </DialogHeader>
          <InterfaceForm onSubmit={(p) => muCreate.mutate(p)} onCancel={() => setCreateOpen(false)} allPermissions={allPermissions} departments={departments} allUsers={allUsers} isLoading={muCreate.isPending} />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editTarget} onOpenChange={(o) => !o && setEditTarget(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('admin.interfaces.editTitle')}</DialogTitle>
          </DialogHeader>
          {editTarget && (
            <InterfaceEditForm
              initial={editTarget}
              onSubmit={(p) => muUpdate.mutate({ id: editTarget.id, payload: p })}
              onCancel={() => setEditTarget(null)}
              allPermissions={allPermissions}
              departments={departments}
              allUsers={allUsers}
              isLoading={muUpdate.isPending}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.interfaces.deleteTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.interfaces.deleteDesc', { name: deleteTarget?.interface })}
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

function InterfaceForm({ onSubmit, onCancel, allPermissions, departments, allUsers, isLoading }: {
  onSubmit: (p: InterfaceProfileCreate) => void
  onCancel: () => void
  allPermissions: PermissionOut[]
  departments: DepartmentOut[]
  allUsers: AdminUserOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<InterfaceProfileCreate>({
    interface: 'web',
    mode: 'denylist',
    tool_permissions: [],
    is_active: true,
  })
  const [selectedTags, setSelectedTags] = useState<Set<string>>(new Set())
  const [deptId, setDeptId] = useState<string>('__org__')
  const [userId, setUserId] = useState<string>('__none__')
  const [interfaceConfigJson, setInterfaceConfigJson] = useState('{}')
  const [preferencesJson, setPreferencesJson] = useState('{}')
  const [jsonError, setJsonError] = useState<string | null>(null)

  const toggleTag = (tag: string) => {
    setSelectedTags((prev) => {
      const next = new Set(prev)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    let interface_config: Record<string, unknown> | null = null
    let preferences: Record<string, unknown> | null = null
    try {
      const icTrimmed = interfaceConfigJson.trim()
      if (icTrimmed && icTrimmed !== '{}') interface_config = JSON.parse(icTrimmed)
      const prefTrimmed = preferencesJson.trim()
      if (prefTrimmed && prefTrimmed !== '{}') preferences = JSON.parse(prefTrimmed)
      setJsonError(null)
    } catch {
      setJsonError(t('datastores.invalidJson'))
      return
    }
    onSubmit({
      ...form,
      dept_id: deptId === '__org__' ? null : deptId,
      user_id: userId === '__none__' ? null : userId,
      tool_permissions: Array.from(selectedTags),
      interface_config,
      preferences,
    })
  }

  // Group permissions by category
  const grouped = allPermissions.reduce<Record<string, PermissionOut[]>>((acc, p) => {
    const cat = p.category || 'other'
    ;(acc[cat] ??= []).push(p)
    return acc
  }, {})

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="max-h-[65vh] overflow-y-auto pr-1 space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.interface')}</Label>
            <Select value={form.interface} onValueChange={(v) => setForm((p) => ({ ...p, interface: v }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {INTERFACES.map((i) => <SelectItem key={i} value={i}>{i}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.mode')}</Label>
            <Select value={form.mode} onValueChange={(v) => setForm((p) => ({ ...p, mode: v }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="denylist">denylist</SelectItem>
                <SelectItem value="allowlist">allowlist</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.department')}</Label>
            <Select value={deptId} onValueChange={setDeptId}>
              <SelectTrigger><SelectValue placeholder={t('admin.interfaces.allDepartments')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__org__">{t('admin.interfaces.allDepartments')}</SelectItem>
                {departments.map((d) => (
                  <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.user')}</Label>
            <Select value={userId} onValueChange={setUserId}>
              <SelectTrigger><SelectValue placeholder={t('admin.interfaces.allUsers')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">{t('admin.interfaces.allUsers')}</SelectItem>
                {allUsers.map((u) => (
                  <SelectItem key={u.id} value={u.id}>{u.full_name} &lt;{u.email}&gt;</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.permissions')}</Label>
          {allPermissions.length > 0 ? (
            <div className="max-h-48 overflow-y-auto rounded border p-2 space-y-3">
              {Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([cat, perms]) => (
                <div key={cat}>
                  <p className="text-xs font-semibold text-muted-foreground uppercase mb-1">{cat}</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                    {perms.map((perm) => (
                      <label key={perm.id} className="flex items-center gap-2 text-sm cursor-pointer">
                        <input
                          type="checkbox"
                          checked={selectedTags.has(perm.tag)}
                          onChange={() => toggleTag(perm.tag)}
                        />
                        {perm.tag}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">{t('common.noResults')}</p>
          )}
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.systemPrompt')}</Label>
          <Textarea
            value={form.system_prompt ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, system_prompt: e.target.value || null }))}
            rows={3}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.interfaceConfig')}</Label>
          <Textarea
            value={interfaceConfigJson}
            onChange={(e) => setInterfaceConfigJson(e.target.value)}
            rows={3}
            className="font-mono text-xs"
            placeholder={t('admin.interfaces.interfaceConfigPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.preferences')}</Label>
          <Textarea
            value={preferencesJson}
            onChange={(e) => setPreferencesJson(e.target.value)}
            rows={3}
            className="font-mono text-xs"
            placeholder={t('admin.interfaces.preferencesPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.description')}</Label>
          <Input value={form.description ?? ''} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} />
        </div>
        {jsonError && <p className="text-xs text-destructive">{jsonError}</p>}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.create')}</Button>
      </DialogFooter>
    </form>
  )
}

function InterfaceEditForm({ initial, onSubmit, onCancel, allPermissions, departments, allUsers, isLoading }: {
  initial: InterfaceProfileOut
  onSubmit: (p: InterfaceProfileUpdate) => void
  onCancel: () => void
  allPermissions: PermissionOut[]
  departments: DepartmentOut[]
  allUsers: AdminUserOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<InterfaceProfileUpdate>({
    is_active: initial.is_active,
    description: initial.description,
    system_prompt: initial.system_prompt,
    mode: initial.mode,
  })
  const [deptId, setDeptId] = useState<string>(initial.dept_id ?? '__org__')
  const [userId, setUserId] = useState<string>(initial.user_id ?? '__none__')
  const [interfaceConfigJson, setInterfaceConfigJson] = useState(
    initial.interface_config ? JSON.stringify(initial.interface_config, null, 2) : '{}'
  )
  const [preferencesJson, setPreferencesJson] = useState(
    initial.preferences ? JSON.stringify(initial.preferences, null, 2) : '{}'
  )
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [selectedTags, setSelectedTags] = useState<Set<string>>(
    new Set(initial.tool_permissions)
  )

  const toggleTag = (tag: string) => {
    setSelectedTags((prev) => {
      const next = new Set(prev)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    let interface_config: Record<string, unknown> | null = null
    let preferences: Record<string, unknown> | null = null
    try {
      const icTrimmed = interfaceConfigJson.trim()
      if (icTrimmed && icTrimmed !== '{}') interface_config = JSON.parse(icTrimmed)
      const prefTrimmed = preferencesJson.trim()
      if (prefTrimmed && prefTrimmed !== '{}') preferences = JSON.parse(prefTrimmed)
      setJsonError(null)
    } catch {
      setJsonError(t('datastores.invalidJson'))
      return
    }
    onSubmit({
      ...form,
      dept_id: deptId === '__org__' ? null : deptId,
      user_id: userId === '__none__' ? null : userId,
      tool_permissions: Array.from(selectedTags),
      interface_config,
      preferences,
    })
  }

  // Group permissions by category
  const grouped = allPermissions.reduce<Record<string, PermissionOut[]>>((acc, p) => {
    const cat = p.category || 'other'
    ;(acc[cat] ??= []).push(p)
    return acc
  }, {})

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="max-h-[65vh] overflow-y-auto pr-1 space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.mode')}</Label>
            <Select value={form.mode} onValueChange={(v) => setForm((p) => ({ ...p, mode: v }))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="denylist">denylist</SelectItem>
                <SelectItem value="allowlist">allowlist</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin.interfaces.department')}</Label>
            <Select value={deptId} onValueChange={setDeptId}>
              <SelectTrigger><SelectValue placeholder={t('admin.interfaces.allDepartments')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__org__">{t('admin.interfaces.allDepartments')}</SelectItem>
                {departments.map((d) => (
                  <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.user')}</Label>
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger><SelectValue placeholder={t('admin.interfaces.allUsers')} /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">{t('admin.interfaces.allUsers')}</SelectItem>
              {allUsers.map((u) => (
                <SelectItem key={u.id} value={u.id}>{u.full_name} &lt;{u.email}&gt;</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.permissions')}</Label>
          {allPermissions.length > 0 ? (
            <div className="max-h-48 overflow-y-auto rounded border p-2 space-y-3">
              {Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([cat, perms]) => (
                <div key={cat}>
                  <p className="text-xs font-semibold text-muted-foreground uppercase mb-1">{cat}</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                    {perms.map((perm) => (
                      <label key={perm.id} className="flex items-center gap-2 text-sm cursor-pointer">
                        <input
                          type="checkbox"
                          checked={selectedTags.has(perm.tag)}
                          onChange={() => toggleTag(perm.tag)}
                        />
                        {perm.tag}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">{t('common.noResults')}</p>
          )}
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.systemPrompt')}</Label>
          <Textarea
            value={form.system_prompt ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, system_prompt: e.target.value || null }))}
            rows={4}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.interfaceConfig')}</Label>
          <Textarea
            value={interfaceConfigJson}
            onChange={(e) => setInterfaceConfigJson(e.target.value)}
            rows={3}
            className="font-mono text-xs"
            placeholder={t('admin.interfaces.interfaceConfigPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.preferences')}</Label>
          <Textarea
            value={preferencesJson}
            onChange={(e) => setPreferencesJson(e.target.value)}
            rows={3}
            className="font-mono text-xs"
            placeholder={t('admin.interfaces.preferencesPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.interfaces.description')}</Label>
          <Input value={form.description ?? ''} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} />
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="is_active"
            checked={form.is_active}
            onChange={(e) => setForm((p) => ({ ...p, is_active: e.target.checked }))}
          />
          <Label htmlFor="is_active">{t('approvalRules.active')}</Label>
        </div>
        {jsonError && <p className="text-xs text-destructive">{jsonError}</p>}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.save')}</Button>
      </DialogFooter>
    </form>
  )
}
