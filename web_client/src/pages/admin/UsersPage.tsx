import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, RefreshCw, KeyRound, Users, Search, Pencil } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import {
  listAdminUsers,
  createAdminUser,
  updateAdminUser,
  deleteAdminUser,
  resetUserPassword,
  resetUserOtp,
  type AdminUserOut,
  type AdminUserCreate,
  type AdminUserUpdate,
} from '@/api/admin'

export default function UsersPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)
  const [editUser, setEditUser] = useState<AdminUserOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminUserOut | null>(null)
  const [tempPassword, setTempPassword] = useState<string | null>(null)
  const [resetPwTarget, setResetPwTarget] = useState<AdminUserOut | null>(null)
  const [resetOtpTarget, setResetOtpTarget] = useState<AdminUserOut | null>(null)

  const LIMIT = 20

  const { data, isLoading } = useQuery({
    queryKey: ['admin', 'users', orgId, page, search],
    queryFn: () => listAdminUsers(orgId!, { page, limit: LIMIT, search: search || undefined }),
    enabled: !!orgId,
  })

  const muCreate = useMutation({
    mutationFn: (p: AdminUserCreate) => createAdminUser(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.users.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'users', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: AdminUserUpdate }) =>
      updateAdminUser(orgId!, id, payload),
    onSuccess: () => {
      toast.success(t('admin.users.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'users', orgId] })
      setEditUser(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteAdminUser(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.users.removed'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'users', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muResetPw = useMutation({
    mutationFn: (id: string) => resetUserPassword(orgId!, id),
    onSuccess: (res) => setTempPassword(res.temporary_password),
    onError: () => toast.error(t('common.error')),
  })

  const muResetOtp = useMutation({
    mutationFn: (id: string) => resetUserOtp(orgId!, id),
    onSuccess: () => toast.success(t('admin.users.otpReset')),
    onError: () => toast.error(t('common.error')),
  })

  const totalPages = data ? Math.ceil(data.total / LIMIT) : 1

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Users className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.users.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.users.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.users.add')}
        </Button>
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          className="pl-9"
          placeholder={t('common.search')}
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
        />
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.users.email')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.users.name')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.users.role')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.users.status')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {data?.items.map((user) => (
                <tr key={user.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">{user.email}</td>
                  <td className="px-4 py-3">{user.full_name}</td>
                  <td className="px-4 py-3">
                    <Badge variant="outline">{user.role_in_org}</Badge>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={user.is_active ? 'default' : 'secondary'}>
                      {user.is_active ? t('approvalRules.active') : t('approvalRules.inactive')}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" size="icon" onClick={() => setEditUser(user)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => setResetPwTarget(user)}>
                        <KeyRound className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => setResetOtpTarget(user)}>
                        <RefreshCw className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="text-destructive" onClick={() => setDeleteTarget(user)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {data?.items.length === 0 && (
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

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center gap-2 justify-end">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            {t('common.prev')}
          </Button>
          <span className="text-sm text-muted-foreground">
            {t('common.page')} {page} {t('common.of')} {totalPages}
          </span>
          <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            {t('common.next')}
          </Button>
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.createTitle')}</DialogTitle>
          </DialogHeader>
          <CreateUserForm onSubmit={(p) => muCreate.mutate(p)} onCancel={() => setCreateOpen(false)} isLoading={muCreate.isPending} />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editUser} onOpenChange={(o) => !o && setEditUser(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('admin.users.editTitle')}</DialogTitle>
          </DialogHeader>
          {editUser && (
            <EditUserForm
              user={editUser}
              onSubmit={(p) => muUpdate.mutate({ id: editUser.id, payload: p })}
              onCancel={() => setEditUser(null)}
              isLoading={muUpdate.isPending}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.removeTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.users.removeDesc', { email: deleteTarget?.email })}
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

      {/* Confirm reset password */}
      <Dialog open={!!resetPwTarget} onOpenChange={(o) => !o && setResetPwTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.resetPasswordTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.users.resetPasswordDesc', { email: resetPwTarget?.email })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetPwTarget(null)}>{t('common.cancel')}</Button>
            <Button
              disabled={muResetPw.isPending}
              onClick={() => {
                if (resetPwTarget) {
                  muResetPw.mutate(resetPwTarget.id)
                  setResetPwTarget(null)
                }
              }}
            >
              {t('common.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Confirm reset OTP */}
      <Dialog open={!!resetOtpTarget} onOpenChange={(o) => !o && setResetOtpTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.resetOtpTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.users.resetOtpDesc', { email: resetOtpTarget?.email })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetOtpTarget(null)}>{t('common.cancel')}</Button>
            <Button
              disabled={muResetOtp.isPending}
              onClick={() => {
                if (resetOtpTarget) {
                  muResetOtp.mutate(resetOtpTarget.id)
                  setResetOtpTarget(null)
                }
              }}
            >
              {t('common.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Temp password dialog */}
      <Dialog open={!!tempPassword} onOpenChange={(o) => !o && setTempPassword(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.tempPasswordTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">{t('admin.users.tempPasswordDesc')}</p>
          <Input readOnly value={tempPassword ?? ''} className="font-mono" />
          <DialogFooter>
            <Button onClick={() => setTempPassword(null)}>{t('common.close')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function CreateUserForm({ onSubmit, onCancel, isLoading }: { onSubmit: (p: AdminUserCreate) => void; onCancel: () => void; isLoading: boolean }) {
  const { t } = useTranslation()
  const [form, setForm] = useState<AdminUserCreate>({ email: '', full_name: '', password: '', role: 'member' })

  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(form) }} className="space-y-4">
      <div className="space-y-1.5">
        <Label>{t('auth.email')}</Label>
        <Input type="email" value={form.email} onChange={(e) => setForm((p) => ({ ...p, email: e.target.value }))} required />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.users.name')}</Label>
        <Input value={form.full_name} onChange={(e) => setForm((p) => ({ ...p, full_name: e.target.value }))} required />
      </div>
      <div className="space-y-1.5">
        <Label>{t('auth.password')}</Label>
        <Input type="password" value={form.password} minLength={8} onChange={(e) => setForm((p) => ({ ...p, password: e.target.value }))} required />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.users.role')}</Label>
        <Select value={form.role} onValueChange={(v) => setForm((p) => ({ ...p, role: v }))}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            {['viewer', 'member', 'admin'].map((r) => (
              <SelectItem key={r} value={r}>{r}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.create')}</Button>
      </DialogFooter>
    </form>
  )
}

function EditUserForm({ user, onSubmit, onCancel, isLoading }: { user: AdminUserOut; onSubmit: (p: AdminUserUpdate) => void; onCancel: () => void; isLoading: boolean }) {
  const { t } = useTranslation()
  const [form, setForm] = useState<AdminUserUpdate>({
    full_name: user.full_name,
    is_active: user.is_active,
    role: user.role_in_org,
    telegram_id: user.telegram_id ?? '',
    teams_aad_object_id: user.teams_aad_object_id ?? '',
    secondary_emails: user.secondary_emails ?? '',
    ai_instructions: user.ai_instructions ?? '',
    otp_enabled: user.otp_enabled,
  })

  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(form) }} className="space-y-4">
      <div className="max-h-[65vh] overflow-y-auto pr-1 space-y-4">
        <div className="space-y-1.5">
          <Label>{t('admin.users.name')}</Label>
          <Input value={form.full_name ?? ''} onChange={(e) => setForm((p) => ({ ...p, full_name: e.target.value }))} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.users.role')}</Label>
          <Select value={form.role} onValueChange={(v) => setForm((p) => ({ ...p, role: v }))}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {['viewer', 'member', 'admin'].map((r) => (
                <SelectItem key={r} value={r}>{r}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.users.telegramId')}</Label>
          <Input
            value={form.telegram_id ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, telegram_id: e.target.value || null }))}
            placeholder={t('admin.users.telegramIdPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.users.teamsAadObjectId')}</Label>
          <Input
            value={form.teams_aad_object_id ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, teams_aad_object_id: e.target.value || null }))}
            placeholder={t('admin.users.teamsAadObjectIdPlaceholder')}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.users.secondaryEmails')}</Label>
          <Textarea
            value={form.secondary_emails ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, secondary_emails: e.target.value || null }))}
            placeholder={t('admin.users.secondaryEmailsPlaceholder')}
            rows={3}
          />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.users.aiInstructions')}</Label>
          <Textarea
            value={form.ai_instructions ?? ''}
            onChange={(e) => setForm((p) => ({ ...p, ai_instructions: e.target.value || null }))}
            placeholder={t('admin.users.aiInstructionsPlaceholder')}
            rows={4}
          />
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="otp_enabled"
            checked={form.otp_enabled ?? false}
            onChange={(e) => setForm((p) => ({ ...p, otp_enabled: e.target.checked }))}
          />
          <Label htmlFor="otp_enabled">{t('admin.users.otpEnabled')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="is_active"
            checked={form.is_active}
            onChange={(e) => setForm((p) => ({ ...p, is_active: e.target.checked }))}
          />
          <Label htmlFor="is_active">{t('admin.users.active')}</Label>
        </div>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.save')}</Button>
      </DialogFooter>
    </form>
  )
}
