import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { User, Mail, Building2, Pencil, KeyRound, Loader2, ShieldCheck, ShieldOff, RefreshCw } from 'lucide-react'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Separator } from '@/components/ui/separator'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useAuth } from '@/contexts/AuthContext'
import { updateProfile, changePassword, otpStatus, otpDisable, regenerateBackupCodes } from '@/api/auth'
import { toast } from 'sonner'

export default function ProfilePage() {
  const { t } = useTranslation()
  const { user, orgId, refreshUser } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [editOpen, setEditOpen] = useState(false)
  const [editName, setEditName] = useState('')
  const [pwdOpen, setPwdOpen] = useState(false)
  const [currentPwd, setCurrentPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')

  // OTP management state
  const [otpDisableOpen, setOtpDisableOpen] = useState(false)
  const [otpDisablePwd, setOtpDisablePwd] = useState('')
  const [otpDisableCode, setOtpDisableCode] = useState('')
  const [regenOpen, setRegenOpen] = useState(false)
  const [regenPwd, setRegenPwd] = useState('')
  const [regenCode, setRegenCode] = useState('')
  const [newBackupCodes, setNewBackupCodes] = useState<string[]>([])

  if (!user) return null

  const initials = user.full_name
    ?.split(' ')
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2) ?? user.email[0].toUpperCase()

  const currentOrg = user.memberships?.find((m) => m.org_id === orgId)

  const updateMut = useMutation({
    mutationFn: () => updateProfile({ full_name: editName.trim() }),
    onSuccess: async () => {
      await refreshUser()
      toast.success(t('profile.profileUpdated'))
      setEditOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const pwdMut = useMutation({
    mutationFn: () => changePassword({ current_password: currentPwd, new_password: newPwd }),
    onSuccess: () => {
      toast.success(t('profile.passwordChanged'))
      setPwdOpen(false)
      setCurrentPwd('')
      setNewPwd('')
      setConfirmPwd('')
    },
    onError: (err: unknown) => {
      const raw = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      const detail = Array.isArray(raw)
        ? raw.map((e: { msg?: string }) => e.msg ?? String(e)).join('; ')
        : typeof raw === 'string' ? raw : undefined
      toast.error(detail ?? t('common.error'))
    },
  })

  // OTP status query
  const { data: otpData, refetch: refetchOtp } = useQuery({
    queryKey: ['otp-status'],
    queryFn: otpStatus,
    retry: false,
  })

  const otpDisableMut = useMutation({
    mutationFn: () => otpDisable({ password: otpDisablePwd || undefined, code: otpDisableCode || undefined }),
    onSuccess: () => {
      toast.success(t('otp.profile.disabled'))
      setOtpDisableOpen(false)
      setOtpDisablePwd('')
      setOtpDisableCode('')
      void refetchOtp()
      void queryClient.invalidateQueries({ queryKey: ['otp-status'] })
    },
    onError: () => toast.error(t('common.error')),
  })

  const regenMut = useMutation({
    mutationFn: () => regenerateBackupCodes({ password: regenPwd || undefined, code: regenCode || undefined }),
    onSuccess: (result) => {
      setNewBackupCodes(result.backup_codes)
      setRegenPwd('')
      setRegenCode('')
      void refetchOtp()
    },
    onError: () => toast.error(t('common.error')),
  })

  function openEdit() {
    setEditName(user!.full_name ?? '')
    setEditOpen(true)
  }

  function openPwd() {
    setCurrentPwd('')
    setNewPwd('')
    setConfirmPwd('')
    setPwdOpen(true)
  }

  const pwdMismatch = confirmPwd.length > 0 && newPwd !== confirmPwd

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-2xl mx-auto">
        <div className="mb-6">
          <h1 className="text-2xl font-bold">{t('profile.title')}</h1>
          <p className="text-muted-foreground text-sm mt-1">{t('profile.subtitle')}</p>
        </div>

        {/* Profile card */}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-4">
              <Avatar className="h-16 w-16">
                <AvatarFallback className="text-xl bg-[hsl(var(--primary))] text-white">
                  {initials}
                </AvatarFallback>
              </Avatar>
              <div className="flex-1">
                <h2 className="text-xl font-semibold">{user.full_name ?? user.email}</h2>
                <p className="text-muted-foreground text-sm">{user.email}</p>
              </div>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={openEdit}>
                  <Pencil className="h-3.5 w-3.5 me-1.5" />
                  {t('profile.editProfile')}
                </Button>
                <Button variant="outline" size="sm" onClick={openPwd}>
                  <KeyRound className="h-3.5 w-3.5 me-1.5" />
                  {t('profile.changePassword')}
                </Button>
              </div>
            </div>
          </CardHeader>
          <Separator />
          <CardContent className="pt-4 space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="flex items-start gap-3">
                <User className="h-4 w-4 mt-0.5 text-muted-foreground" />
                <div>
                  <p className="text-xs text-muted-foreground">{t('profile.fullName')}</p>
                  <p className="text-sm font-medium">{user.full_name ?? '—'}</p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Mail className="h-4 w-4 mt-0.5 text-muted-foreground" />
                <div>
                  <p className="text-xs text-muted-foreground">{t('profile.email')}</p>
                  <p className="text-sm font-medium">{user.email}</p>
                </div>
              </div>
            </div>

            {currentOrg && (
              <>
                <Separator />
                <div>
                  <p className="text-xs text-muted-foreground mb-2">{t('profile.currentOrg')}</p>
                  <div className="flex items-center gap-2">
                    <Building2 className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm font-medium">{currentOrg.org_name}</span>
                    <Badge variant="secondary" className="text-xs capitalize">
                      {currentOrg.role}
                    </Badge>
                  </div>
                </div>
              </>
            )}

            {user.memberships && user.memberships.length > 1 && (
              <>
                <Separator />
                <div>
                  <p className="text-xs text-muted-foreground mb-2">{t('profile.allOrgs')}</p>
                  <div className="space-y-2">
                    {user.memberships.map((m) => (
                      <div key={m.org_id} className="flex items-center gap-2">
                        <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="text-sm">{m.org_name}</span>
                        <Badge variant="outline" className="text-xs capitalize ms-auto">
                          {m.role}
                        </Badge>
                      </div>
                    ))}
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>

        {/* OTP / 2FA card */}
        <Card className="mt-4">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {otpData?.otp_enabled
                  ? <ShieldCheck className="h-5 w-5 text-green-500" />
                  : <ShieldOff className="h-5 w-5 text-muted-foreground" />}
                <h2 className="text-base font-semibold">{t('otp.profile.title')}</h2>
              </div>
              <Badge variant={otpData?.otp_enabled ? 'default' : 'secondary'}>
                {otpData?.otp_enabled ? t('otp.profile.enabled') : t('otp.profile.disabled_badge')}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {!otpData?.otp_enabled && (
              <div className="flex items-center justify-between">
                <p className="text-sm text-muted-foreground">{t('otp.profile.notEnabledHint')}</p>
                <Button size="sm" onClick={() => navigate('/otp-setup')}>
                  {t('otp.profile.enable')}
                </Button>
              </div>
            )}
            {otpData?.otp_enabled && (
              <div className="space-y-2">
                {otpData.backup_codes_count !== undefined && (
                  <p className="text-sm text-muted-foreground">
                    {t('otp.profile.backupCodesLeft', { count: otpData.backup_codes_count })}
                  </p>
                )}
                <div className="flex gap-2 flex-wrap">
                  <Button variant="outline" size="sm" onClick={() => { setRegenOpen(true); setNewBackupCodes([]) }}>
                    <RefreshCw className="me-1.5 h-3.5 w-3.5" />
                    {t('otp.profile.regenerateCodes')}
                  </Button>
                  <Button variant="outline" size="sm" className="text-destructive border-destructive/50 hover:bg-destructive/10" onClick={() => setOtpDisableOpen(true)}>
                    <ShieldOff className="me-1.5 h-3.5 w-3.5" />
                    {t('otp.profile.disable')}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Edit name dialog */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('profile.editProfile')}</DialogTitle>
            <DialogDescription className="sr-only">{t('profile.subtitle')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div>
              <Label htmlFor="edit-name">{t('profile.fullName')}</Label>
              <Input
                id="edit-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                autoFocus
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={() => updateMut.mutate()}
              disabled={updateMut.isPending || !editName.trim()}
            >
              {updateMut.isPending && <Loader2 className="h-4 w-4 animate-spin me-2" />}
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Change password dialog */}
      <Dialog open={pwdOpen} onOpenChange={setPwdOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('profile.changePassword')}</DialogTitle>
            <DialogDescription className="sr-only">{t('profile.subtitle')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div>
              <Label htmlFor="current-pwd">{t('profile.currentPassword')}</Label>
              <Input
                id="current-pwd"
                type="password"
                value={currentPwd}
                onChange={(e) => setCurrentPwd(e.target.value)}
                autoFocus
              />
            </div>
            <div>
              <Label htmlFor="new-pwd">{t('profile.newPassword')}</Label>
              <Input
                id="new-pwd"
                type="password"
                value={newPwd}
                onChange={(e) => setNewPwd(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="confirm-pwd">{t('profile.confirmPassword')}</Label>
              <Input
                id="confirm-pwd"
                type="password"
                value={confirmPwd}
                onChange={(e) => setConfirmPwd(e.target.value)}
                className={pwdMismatch ? 'border-destructive' : ''}
              />
              {pwdMismatch && (
                <p className="text-xs text-destructive mt-1">{t('profile.passwordMismatch')}</p>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPwdOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={() => pwdMut.mutate()}
              disabled={
                pwdMut.isPending ||
                !currentPwd ||
                !newPwd ||
                newPwd !== confirmPwd
              }
            >
              {pwdMut.isPending && <Loader2 className="h-4 w-4 animate-spin me-2" />}
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Disable OTP dialog */}
      <Dialog open={otpDisableOpen} onOpenChange={setOtpDisableOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('otp.profile.disableTitle')}</DialogTitle>
            <DialogDescription className="sr-only">{t('otp.profile.disableHint')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <p className="text-sm text-muted-foreground">{t('otp.profile.disableHint')}</p>
            <div>
              <Label htmlFor="otp-disable-pwd">{t('profile.password')}</Label>
              <Input
                id="otp-disable-pwd"
                type="password"
                value={otpDisablePwd}
                onChange={(e) => setOtpDisablePwd(e.target.value)}
                placeholder={t('otp.profile.orCode')}
                autoFocus
              />
            </div>
            <div>
              <Label htmlFor="otp-disable-code">{t('otp.profile.otpCode')}</Label>
              <Input
                id="otp-disable-code"
                value={otpDisableCode}
                onChange={(e) => setOtpDisableCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder="000000"
                inputMode="numeric"
                className="text-center font-mono tracking-[0.3em]"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOtpDisableOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => otpDisableMut.mutate()}
              disabled={otpDisableMut.isPending || (!otpDisablePwd && !otpDisableCode)}
            >
              {otpDisableMut.isPending && <Loader2 className="h-4 w-4 animate-spin me-2" />}
              {t('otp.profile.disableConfirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Regenerate backup codes dialog */}
      <Dialog open={regenOpen} onOpenChange={(v) => { setRegenOpen(v); if (!v) setNewBackupCodes([]) }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('otp.profile.regenTitle')}</DialogTitle>
            <DialogDescription className="sr-only">{t('otp.profile.regenHint')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 py-2">
            {newBackupCodes.length === 0 ? (
              <>
                <p className="text-sm text-muted-foreground">{t('otp.profile.regenHint')}</p>
                <div>
                  <Label htmlFor="regen-pwd">{t('profile.password')}</Label>
                  <Input
                    id="regen-pwd"
                    type="password"
                    value={regenPwd}
                    onChange={(e) => setRegenPwd(e.target.value)}
                    autoFocus
                  />
                </div>
                <div>
                  <Label htmlFor="regen-code">{t('otp.profile.otpCode')}</Label>
                  <Input
                    id="regen-code"
                    value={regenCode}
                    onChange={(e) => setRegenCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    placeholder="000000"
                    inputMode="numeric"
                    className="text-center font-mono tracking-[0.3em]"
                  />
                </div>
              </>
            ) : (
              <>
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
                  {t('otp.setup.backupWarning')}
                </div>
                <div className="grid grid-cols-2 gap-1.5 font-mono text-xs">
                  {newBackupCodes.map((bc) => (
                    <div key={bc} className="rounded bg-muted px-2 py-1 text-center tracking-wider">
                      {bc}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
          <DialogFooter>
            {newBackupCodes.length === 0 ? (
              <>
                <Button variant="outline" onClick={() => setRegenOpen(false)}>
                  {t('common.cancel')}
                </Button>
                <Button
                  onClick={() => regenMut.mutate()}
                  disabled={regenMut.isPending || (!regenPwd && !regenCode)}
                >
                  {regenMut.isPending && <Loader2 className="h-4 w-4 animate-spin me-2" />}
                  {t('otp.profile.regenConfirm')}
                </Button>
              </>
            ) : (
              <Button className="flex-1" onClick={() => setRegenOpen(false)}>
                {t('common.done')}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
