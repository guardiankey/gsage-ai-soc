import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, TestTube2, Mail } from 'lucide-react'
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
  listEmailAccounts,
  createEmailAccount,
  updateEmailAccount,
  deleteEmailAccount,
  testEmailAccount,
  listDepartments,
  type EmailAccountOut,
  type EmailAccountCreate,
  type EmailAccountUpdate,
  type EmailConnectionTestResult,
  type DepartmentOut,
} from '@/api/admin'

export default function EmailAccountsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<EmailAccountOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<EmailAccountOut | null>(null)
  const [testResult, setTestResult] = useState<{ account: EmailAccountOut; result: EmailConnectionTestResult } | null>(null)

  const { data: accounts, isLoading } = useQuery({
    queryKey: ['admin', 'email-accounts', orgId],
    queryFn: () => listEmailAccounts(orgId!),
    enabled: !!orgId,
  })

  const { data: departments = [] } = useQuery({
    queryKey: ['admin', 'departments', orgId],
    queryFn: () => listDepartments(orgId!),
    enabled: !!orgId,
  })

  const muCreate = useMutation({
    mutationFn: (p: EmailAccountCreate) => createEmailAccount(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.emailAccounts.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'email-accounts', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: EmailAccountUpdate }) =>
      updateEmailAccount(orgId!, id, payload),
    onSuccess: () => {
      toast.success(t('admin.emailAccounts.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'email-accounts', orgId] })
      setEditTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteEmailAccount(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.emailAccounts.deleted'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'email-accounts', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muTest = useMutation({
    mutationFn: (account: EmailAccountOut) => testEmailAccount(orgId!, account.id),
    onSuccess: (result, account) => setTestResult({ account, result }),
    onError: () => toast.error(t('common.error')),
  })

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Mail className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.emailAccounts.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.emailAccounts.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.emailAccounts.add')}
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[...Array(3)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.emailAccounts.displayName')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.emailAccounts.email')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.emailAccounts.imapHost')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.emailAccounts.smtpHost')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('common.status')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {accounts?.map((a) => (
                <tr key={a.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3 font-medium">{a.display_name}</td>
                  <td className="px-4 py-3 text-muted-foreground">{a.email}</td>
                  <td className="px-4 py-3 font-mono text-xs">{a.imap_host}:{a.imap_port}</td>
                  <td className="px-4 py-3 font-mono text-xs">{a.smtp_host}:{a.smtp_port}</td>
                  <td className="px-4 py-3">
                    <Badge variant={a.is_active ? 'default' : 'secondary'}>
                      {a.is_active ? t('approvalRules.active') : t('approvalRules.inactive')}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <Button
                        variant="ghost" size="icon"
                        disabled={muTest.isPending}
                        onClick={() => muTest.mutate(a)}
                        title={t('admin.emailAccounts.testConnection')}
                      >
                        <TestTube2 className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => setEditTarget(a)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="text-destructive" onClick={() => setDeleteTarget(a)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {accounts?.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">{t('common.noResults')}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('admin.emailAccounts.createTitle')}</DialogTitle>
          </DialogHeader>
          <EmailAccountForm onSubmit={(p) => muCreate.mutate(p)} onCancel={() => setCreateOpen(false)} departments={departments} isLoading={muCreate.isPending} />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editTarget} onOpenChange={(o) => !o && setEditTarget(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('admin.emailAccounts.editTitle')}</DialogTitle>
          </DialogHeader>
          {editTarget && (
            <EmailAccountEditForm
              initial={editTarget}
              onSubmit={(p) => muUpdate.mutate({ id: editTarget.id, payload: p })}
              onCancel={() => setEditTarget(null)}
              departments={departments}
              isLoading={muUpdate.isPending}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.emailAccounts.deleteTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.emailAccounts.deleteDesc', { email: deleteTarget?.email })}
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

      {/* Test result dialog */}
      <Dialog open={!!testResult} onOpenChange={(o) => !o && setTestResult(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.emailAccounts.testTitle')}</DialogTitle>
          </DialogHeader>
          {testResult && (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">{testResult.account.display_name} &lt;{testResult.account.email}&gt;</p>
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <Badge variant={testResult.result.imap_ok ? 'default' : 'destructive'}>
                    {t('admin.emailAccounts.imapOk')}: {testResult.result.imap_ok ? '✓' : '✗'}
                  </Badge>
                  {testResult.result.imap_error && (
                    <span className="text-xs text-destructive">{testResult.result.imap_error}</span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={testResult.result.smtp_ok ? 'default' : 'destructive'}>
                    {t('admin.emailAccounts.smtpOk')}: {testResult.result.smtp_ok ? '✓' : '✗'}
                  </Badge>
                  {testResult.result.smtp_error && (
                    <span className="text-xs text-destructive">{testResult.result.smtp_error}</span>
                  )}
                </div>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setTestResult(null)}>{t('common.close')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function EmailAccountForm({ onSubmit, onCancel, departments, isLoading }: {
  onSubmit: (p: EmailAccountCreate) => void
  onCancel: () => void
  departments: DepartmentOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<EmailAccountCreate>({
    display_name: '',
    email: '',
    imap_host: '',
    imap_port: 993,
    imap_username: '',
    imap_password: '',
    imap_use_tls: true,
    imap_verify_ssl: true,
    smtp_host: '',
    smtp_port: 25,
    smtp_username: '',
    smtp_password: '',
    smtp_use_tls: true,
    smtp_verify_ssl: true,
    is_active: true,
  })
  const [deptId, setDeptId] = useState<string>('__org__')

  const set = (key: keyof EmailAccountCreate) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((p) => ({ ...p, [key]: e.target.type === 'number' ? Number(e.target.value) : e.target.value }))

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onSubmit({ ...form, dept_id: deptId === '__org__' ? null : deptId })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.displayName')}</Label>
          <Input required value={form.display_name} onChange={set('display_name')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.email')}</Label>
          <Input required type="email" value={form.email} onChange={set('email')} />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.department')}</Label>
        <Select value={deptId} onValueChange={setDeptId}>
          <SelectTrigger><SelectValue placeholder={t('admin.emailAccounts.allDepartments')} /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__org__">{t('admin.emailAccounts.allDepartments')}</SelectItem>
            {departments.map((d) => (
              <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2 space-y-1.5">
          <Label>{t('admin.emailAccounts.imapHost')}</Label>
          <Input required value={form.imap_host} onChange={set('imap_host')} placeholder="imap.example.com" />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.imapPort')}</Label>
          <Input type="number" value={form.imap_port} onChange={set('imap_port')} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.imapUsername')}</Label>
          <Input required value={form.imap_username} onChange={set('imap_username')} autoComplete="username" />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.imapPassword')}</Label>
          <Input type="password" value={form.imap_password} onChange={set('imap_password')} autoComplete="new-password" />
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="imap_use_tls"
            checked={form.imap_use_tls ?? true}
            onChange={(e) => setForm((p) => ({ ...p, imap_use_tls: e.target.checked }))}
          />
          <Label htmlFor="imap_use_tls">{t('admin.emailAccounts.imapUseTls')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="imap_verify_ssl"
            checked={form.imap_verify_ssl ?? true}
            onChange={(e) => setForm((p) => ({ ...p, imap_verify_ssl: e.target.checked }))}
          />
          <Label htmlFor="imap_verify_ssl">{t('admin.emailAccounts.imapVerifySsl')}</Label>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2 space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpHost')}</Label>
          <Input required value={form.smtp_host} onChange={set('smtp_host')} placeholder="smtp.example.com" />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpPort')}</Label>
          <Input type="number" value={form.smtp_port} onChange={set('smtp_port')} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpUsername')}</Label>
          <Input value={form.smtp_username ?? ''} onChange={set('smtp_username')} autoComplete="username" placeholder={t('admin.emailAccounts.smtpNoAuthPlaceholder')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpPassword')}</Label>
          <Input type="password" value={form.smtp_password ?? ''} onChange={set('smtp_password')} autoComplete="new-password" placeholder={t('admin.emailAccounts.smtpNoAuthPlaceholder')} />
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="smtp_use_tls"
            checked={form.smtp_use_tls ?? true}
            onChange={(e) => setForm((p) => ({ ...p, smtp_use_tls: e.target.checked }))}
          />
          <Label htmlFor="smtp_use_tls">{t('admin.emailAccounts.smtpUseTls')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="smtp_verify_ssl"
            checked={form.smtp_verify_ssl ?? true}
            onChange={(e) => setForm((p) => ({ ...p, smtp_verify_ssl: e.target.checked }))}
          />
          <Label htmlFor="smtp_verify_ssl">{t('admin.emailAccounts.smtpVerifySsl')}</Label>
        </div>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.create')}</Button>
      </DialogFooter>
    </form>
  )
}

function EmailAccountEditForm({ initial, onSubmit, onCancel, departments, isLoading }: {
  initial: EmailAccountOut
  onSubmit: (p: EmailAccountUpdate) => void
  onCancel: () => void
  departments: DepartmentOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<EmailAccountUpdate>({
    display_name: initial.display_name,
    imap_host: initial.imap_host,
    imap_port: initial.imap_port,
    imap_username: initial.imap_username,
    imap_use_tls: initial.imap_use_tls,
    imap_verify_ssl: initial.imap_verify_ssl,
    imap_folder: initial.imap_folder,
    imap_idle_supported: initial.imap_idle_supported,
    smtp_host: initial.smtp_host,
    smtp_port: initial.smtp_port,
    smtp_username: initial.smtp_username,
    smtp_use_tls: initial.smtp_use_tls,
    smtp_verify_ssl: initial.smtp_verify_ssl,
    sender_name: initial.sender_name,
    subject_prefix: initial.subject_prefix,
    reply_footer: initial.reply_footer,
    unknown_sender_folder: initial.unknown_sender_folder,
    max_email_size_bytes: initial.max_email_size_bytes,
    polling_interval_seconds: initial.polling_interval_seconds,
    is_active: initial.is_active,
  })
  const [deptId, setDeptId] = useState<string>(initial.dept_id ?? '__org__')
  const [imapPassword, setImapPassword] = useState('')
  const [smtpPassword, setSmtpPassword] = useState('')

  const set = (key: keyof EmailAccountUpdate) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setForm((p) => ({
        ...p,
        [key]: (e.target as HTMLInputElement).type === 'number'
          ? Number((e.target as HTMLInputElement).value)
          : e.target.value,
      }))

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const payload: EmailAccountUpdate = { ...form, dept_id: deptId === '__org__' ? null : deptId }
    if (imapPassword) payload.imap_password = imapPassword
    if (smtpPassword) payload.smtp_password = smtpPassword
    onSubmit(payload)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.displayName')}</Label>
        <Input value={form.display_name ?? ''} onChange={set('display_name')} />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.department')}</Label>
        <Select value={deptId} onValueChange={setDeptId}>
          <SelectTrigger><SelectValue placeholder={t('admin.emailAccounts.allDepartments')} /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__org__">{t('admin.emailAccounts.allDepartments')}</SelectItem>
            {departments.map((d) => (
              <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* ── IMAP ─────────────────────────────────────────────────── */}
      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide pt-2">
        {t('admin.emailAccounts.sectionImap')}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2 space-y-1.5">
          <Label>{t('admin.emailAccounts.imapHost')}</Label>
          <Input value={form.imap_host ?? ''} onChange={set('imap_host')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.imapPort')}</Label>
          <Input type="number" value={form.imap_port ?? ''} onChange={set('imap_port')} />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.imapUsername')}</Label>
        <Input value={form.imap_username ?? ''} onChange={set('imap_username')} autoComplete="username" />
      </div>
      <div className="space-y-1.5">
        <Label>
          {t('admin.emailAccounts.imapPassword')}
          {initial.imap_password_set && (
            <Badge variant="outline" className="ml-2 text-xs">{t('admin.emailAccounts.passwordSet')}</Badge>
          )}
        </Label>
        <Input
          type="password"
          value={imapPassword}
          onChange={(e) => setImapPassword(e.target.value)}
          placeholder={t('admin.emailAccounts.passwordChangePlaceholder')}
          autoComplete="new-password"
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.imapFolder')}</Label>
          <Input value={form.imap_folder ?? ''} onChange={set('imap_folder')} placeholder="INBOX" />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.unknownSenderFolder')}</Label>
          <Input value={form.unknown_sender_folder ?? ''} onChange={set('unknown_sender_folder')} />
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="imap_use_tls_edit"
            checked={form.imap_use_tls ?? true}
            onChange={(e) => setForm((p) => ({ ...p, imap_use_tls: e.target.checked }))}
          />
          <Label htmlFor="imap_use_tls_edit">{t('admin.emailAccounts.imapUseTls')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="imap_verify_ssl_edit"
            checked={form.imap_verify_ssl ?? true}
            onChange={(e) => setForm((p) => ({ ...p, imap_verify_ssl: e.target.checked }))}
          />
          <Label htmlFor="imap_verify_ssl_edit">{t('admin.emailAccounts.imapVerifySsl')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="imap_idle_supported_edit"
            checked={form.imap_idle_supported ?? true}
            onChange={(e) => setForm((p) => ({ ...p, imap_idle_supported: e.target.checked }))}
          />
          <Label htmlFor="imap_idle_supported_edit">{t('admin.emailAccounts.imapIdleSupported')}</Label>
        </div>
      </div>

      {/* ── SMTP ─────────────────────────────────────────────────── */}
      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide pt-2">
        {t('admin.emailAccounts.sectionSmtp')}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2 space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpHost')}</Label>
          <Input value={form.smtp_host ?? ''} onChange={set('smtp_host')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.smtpPort')}</Label>
          <Input type="number" value={form.smtp_port ?? ''} onChange={set('smtp_port')} />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.smtpUsername')}</Label>
        <Input
          value={form.smtp_username ?? ''}
          onChange={set('smtp_username')}
          autoComplete="username"
          placeholder={t('admin.emailAccounts.smtpNoAuthPlaceholder')}
        />
      </div>
      <div className="space-y-1.5">
        <Label>
          {t('admin.emailAccounts.smtpPassword')}
          {initial.smtp_password_set && (
            <Badge variant="outline" className="ml-2 text-xs">{t('admin.emailAccounts.passwordSet')}</Badge>
          )}
        </Label>
        <Input
          type="password"
          value={smtpPassword}
          onChange={(e) => setSmtpPassword(e.target.value)}
          placeholder={t('admin.emailAccounts.passwordChangePlaceholder')}
          autoComplete="new-password"
        />
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="smtp_use_tls_edit"
            checked={form.smtp_use_tls ?? true}
            onChange={(e) => setForm((p) => ({ ...p, smtp_use_tls: e.target.checked }))}
          />
          <Label htmlFor="smtp_use_tls_edit">{t('admin.emailAccounts.smtpUseTls')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="smtp_verify_ssl_edit"
            checked={form.smtp_verify_ssl ?? true}
            onChange={(e) => setForm((p) => ({ ...p, smtp_verify_ssl: e.target.checked }))}
          />
          <Label htmlFor="smtp_verify_ssl_edit">{t('admin.emailAccounts.smtpVerifySsl')}</Label>
        </div>
      </div>

      {/* ── Delivery & Processing ────────────────────────────────── */}
      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide pt-2">
        {t('admin.emailAccounts.sectionDelivery')}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.senderName')}</Label>
          <Input value={form.sender_name ?? ''} onChange={set('sender_name')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.subjectPrefix')}</Label>
          <Input value={form.subject_prefix ?? ''} onChange={set('subject_prefix')} />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.emailAccounts.replyFooter')}</Label>
        <Textarea
          rows={3}
          value={form.reply_footer ?? ''}
          onChange={set('reply_footer')}
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.maxEmailSizeBytes')}</Label>
          <Input type="number" value={form.max_email_size_bytes ?? ''} onChange={set('max_email_size_bytes')} />
        </div>
        <div className="space-y-1.5">
          <Label>{t('admin.emailAccounts.pollingIntervalSeconds')}</Label>
          <Input type="number" value={form.polling_interval_seconds ?? ''} onChange={set('polling_interval_seconds')} />
        </div>
      </div>

      <div className="flex items-center gap-2 pt-2">
        <input
          type="checkbox"
          id="is_active_edit"
          checked={form.is_active ?? true}
          onChange={(e) => setForm((p) => ({ ...p, is_active: e.target.checked }))}
        />
        <Label htmlFor="is_active_edit">{t('approvalRules.active')}</Label>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>{isLoading ? t('common.loading') : t('common.save')}</Button>
      </DialogFooter>
    </form>
  )
}
