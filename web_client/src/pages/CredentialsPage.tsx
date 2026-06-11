import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Link as LinkIcon, KeyRound, Loader2, Check, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Separator } from '@/components/ui/separator'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import { toast } from 'sonner'
import { extractApiError } from '@/api/client'
import {
  listMyCredentials,
  createMyCredential,
  updateMyCredential,
  deleteMyCredential,
  linkCredentialToTool,
  unlinkCredentialFromTool,
  activateCredentialLink,
  listAvailableCredentialTools,
  type Credential,
  type CredentialKind,
  type CredentialIn,
  type CredentialUpdate,
} from '@/api/credentials'

const KIND_OPTIONS: CredentialKind[] = ['basic', 'token', 'api_key', 'oauth2', 'custom']

interface ExtraFieldEntry {
  key: string
  value: string
}

interface FormState {
  label: string
  kind: CredentialKind
  username: string
  password: string
  domain: string
  token: string
  refresh_token: string
  extra_fields: ExtraFieldEntry[]
}

const blankForm: FormState = {
  label: '',
  kind: 'basic',
  username: '',
  password: '',
  domain: '',
  token: '',
  refresh_token: '',
  extra_fields: [],
}

export default function CredentialsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)
  const [form, setForm] = useState<FormState>(blankForm)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [linksFor, setLinksFor] = useState<Credential | null>(null)

  const { data: creds, isLoading } = useQuery({
    queryKey: ['my-credentials', orgId],
    queryFn: () => listMyCredentials(orgId!),
    enabled: !!orgId,
  })

  const { data: availableTools } = useQuery({
    queryKey: ['my-credentials-tools', orgId],
    queryFn: () => listAvailableCredentialTools(orgId!),
    enabled: !!orgId,
  })

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['my-credentials', orgId] })

  const createMut = useMutation({
    mutationFn: (payload: CredentialIn) => createMyCredential(orgId!, payload),
    onSuccess: () => {
      invalidate()
      setDialogOpen(false)
      setForm(blankForm)
      toast.success(t('credentials.created'))
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const updateMut = useMutation({
    mutationFn: (payload: CredentialUpdate) =>
      updateMyCredential(orgId!, editing!.id, payload),
    onSuccess: () => {
      invalidate()
      setDialogOpen(false)
      setEditing(null)
      setForm(blankForm)
      toast.success(t('credentials.updated'))
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const deleteMut = useMutation({
    mutationFn: () => deleteMyCredential(orgId!, deleteId!),
    onSuccess: () => {
      invalidate()
      setDeleteId(null)
      toast.success(t('credentials.deleted'))
    },
    onError: (err) => {
      setDeleteId(null)
      toast.error(extractApiError(err))
    },
  })

  function openCreate() {
    setEditing(null)
    setForm(blankForm)
    setDialogOpen(true)
  }

  function openEdit(c: Credential) {
    setEditing(c)
    // Sensitive fields stay empty — only fill if user wants to replace.
    // Non-sensitive (username, domain) are prefilled from the server.
    // Extra-field values stay empty (only keys are exposed by the API).
    setForm({
      label: c.label,
      kind: c.kind,
      username: c.username ?? '',
      password: '',
      domain: c.domain ?? '',
      token: '',
      refresh_token: '',
      extra_fields: (c.extra_fields_keys ?? []).map((k) => ({ key: k, value: '' })),
    })
    setDialogOpen(true)
  }

  function submit() {
    if (!form.label.trim()) {
      toast.error(t('credentials.errLabel'))
      return
    }
    // Collect extra_fields: keep only entries with a non-empty key.
    // When editing, an entry with empty value means "do not change" so it
    // is dropped (the API treats omitted keys as unchanged on partial
    // update).  On create, empty values become empty strings.
    const extraEntries = form.extra_fields.filter((e) => e.key.trim())
    const duplicateKey = extraEntries.find(
      (e, i) => extraEntries.findIndex((o) => o.key.trim() === e.key.trim()) !== i,
    )
    if (duplicateKey) {
      toast.error(t('credentials.errDuplicateExtraField', { key: duplicateKey.key.trim() }))
      return
    }
    let extra_fields: Record<string, string> | undefined
    if (editing) {
      // Only send entries whose value was filled (means replace) or new keys.
      const existingKeys = new Set(editing.extra_fields_keys ?? [])
      const toSend = extraEntries.filter(
        (e) => e.value !== '' || !existingKeys.has(e.key.trim()),
      )
      if (toSend.length > 0) {
        extra_fields = Object.fromEntries(toSend.map((e) => [e.key.trim(), e.value]))
      }
    } else if (extraEntries.length > 0) {
      extra_fields = Object.fromEntries(extraEntries.map((e) => [e.key.trim(), e.value]))
    }

    const sensitive = {
      username: form.username.trim() || undefined,
      password: form.password || undefined,
      domain: form.domain.trim() || undefined,
      token: form.token || undefined,
      refresh_token: form.refresh_token || undefined,
      extra_fields,
    }
    if (editing) {
      const payload: CredentialUpdate = {
        label: form.label.trim(),
        kind: form.kind,
        ...sensitive,
      }
      updateMut.mutate(payload)
    } else {
      const payload: CredentialIn = {
        label: form.label.trim(),
        kind: form.kind,
        ...sensitive,
      }
      createMut.mutate(payload)
    }
  }

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-3xl mx-auto">
        <div className="mb-6 flex items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">{t('credentials.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('credentials.subtitle')}</p>
          </div>
          <Button onClick={openCreate}>
            <Plus className="h-4 w-4 me-2" />
            {t('credentials.create')}
          </Button>
        </div>

        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        ) : (creds ?? []).length === 0 ? (
          <Card>
            <CardContent className="p-8 text-center text-muted-foreground">
              <KeyRound className="h-8 w-8 mx-auto mb-2 opacity-40" />
              <p className="text-sm">{t('credentials.empty')}</p>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-3">
            {creds!.map((c) => (
              <CredentialCard
                key={c.id}
                cred={c}
                onEdit={() => openEdit(c)}
                onDelete={() => setDeleteId(c.id)}
                onManageLinks={() => setLinksFor(c)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Create / edit dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editing ? t('credentials.editTitle') : t('credentials.createTitle')}
            </DialogTitle>
            <DialogDescription>
              {editing ? t('credentials.editHint') : t('credentials.createHint')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label htmlFor="cred-label">{t('credentials.label')}</Label>
              <Input
                id="cred-label"
                value={form.label}
                onChange={(e) => setForm({ ...form, label: e.target.value })}
                placeholder={t('credentials.labelPlaceholder')}
              />
            </div>
            <div>
              <Label htmlFor="cred-kind">{t('credentials.kind')}</Label>
              <Select
                value={form.kind}
                onValueChange={(v) => setForm({ ...form, kind: v as CredentialKind })}
              >
                <SelectTrigger id="cred-kind">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {KIND_OPTIONS.map((k) => (
                    <SelectItem key={k} value={k}>
                      {t(`credentials.kinds.${k}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <KindFields form={form} setForm={setForm} editing={!!editing} />
            <Separator />
            <ExtraFieldsEditor form={form} setForm={setForm} editing={!!editing} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button onClick={submit} disabled={createMut.isPending || updateMut.isPending}>
              {(createMut.isPending || updateMut.isPending) && (
                <Loader2 className="h-4 w-4 me-2 animate-spin" />
              )}
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={!!deleteId} onOpenChange={(o) => !o && setDeleteId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('credentials.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('credentials.deleteDesc')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => deleteMut.mutate()}>
              {t('common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Tool links manager */}
      {linksFor && (
        <ToolLinksDialog
          cred={linksFor}
          availableTools={availableTools ?? []}
          onClose={() => setLinksFor(null)}
          onChanged={() => invalidate()}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

function CredentialCard({
  cred,
  onEdit,
  onDelete,
  onManageLinks,
}: {
  cred: Credential
  onEdit: () => void
  onDelete: () => void
  onManageLinks: () => void
}) {
  const { t } = useTranslation()
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 py-3">
        <div className="min-w-0 flex-1">
          <CardTitle className="text-base truncate">{cred.label}</CardTitle>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Badge variant="secondary">{t(`credentials.kinds.${cred.kind}`)}</Badge>
            {cred.has_username && <Badge variant="outline">{t('credentials.fields.username')}</Badge>}
            {cred.has_password && <Badge variant="outline">{t('credentials.fields.password')}</Badge>}
            {cred.has_domain && <Badge variant="outline">{t('credentials.fields.domain')}</Badge>}
            {cred.has_token && <Badge variant="outline">{t('credentials.fields.token')}</Badge>}
            {cred.has_refresh_token && (
              <Badge variant="outline">{t('credentials.fields.refreshToken')}</Badge>
            )}
          </div>
          {cred.tool_links.length > 0 && (
            <p className="text-xs text-muted-foreground mt-2">
              {t('credentials.linkedCount', { count: cred.tool_links.length })}
            </p>
          )}
        </div>
        <div className="flex gap-1 shrink-0">
          <Button variant="ghost" size="icon" onClick={onManageLinks} title={t('credentials.manageLinks')}>
            <LinkIcon className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" onClick={onEdit} title={t('common.edit')}>
            <Pencil className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" onClick={onDelete} title={t('common.delete')}>
            <Trash2 className="h-4 w-4 text-destructive" />
          </Button>
        </div>
      </CardHeader>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Conditional fields per kind
// ---------------------------------------------------------------------------

function KindFields({
  form,
  setForm,
  editing,
}: {
  form: FormState
  setForm: (f: FormState) => void
  editing: boolean
}) {
  const { t } = useTranslation()
  const placeholder = editing ? t('credentials.unchangedPlaceholder') : ''

  switch (form.kind) {
    case 'basic':
      return (
        <>
          <FieldText
            id="cred-username"
            label={t('credentials.fields.username')}
            value={form.username}
            onChange={(v) => setForm({ ...form, username: v })}
          />
          <FieldText
            id="cred-password"
            label={t('credentials.fields.password')}
            value={form.password}
            onChange={(v) => setForm({ ...form, password: v })}
            type="password"
            placeholder={placeholder}
          />
          <FieldText
            id="cred-domain"
            label={t('credentials.fields.domain')}
            value={form.domain}
            onChange={(v) => setForm({ ...form, domain: v })}
          />
        </>
      )
    case 'token':
    case 'api_key':
      return (
        <>
          <FieldText
            id="cred-token"
            label={t('credentials.fields.token')}
            value={form.token}
            onChange={(v) => setForm({ ...form, token: v })}
            type="password"
            placeholder={placeholder}
          />
        </>
      )
    case 'oauth2':
      return (
        <>
          <FieldText
            id="cred-token"
            label={t('credentials.fields.token')}
            value={form.token}
            onChange={(v) => setForm({ ...form, token: v })}
            type="password"
            placeholder={placeholder}
          />
          <FieldText
            id="cred-refresh"
            label={t('credentials.fields.refreshToken')}
            value={form.refresh_token}
            onChange={(v) => setForm({ ...form, refresh_token: v })}
            type="password"
            placeholder={placeholder}
          />
        </>
      )
    case 'custom':
    default:
      return (
        <p className="text-xs text-muted-foreground">{t('credentials.customHint')}</p>
      )
  }
}

// ---------------------------------------------------------------------------
// Extra fields editor (available for all credential kinds)
// ---------------------------------------------------------------------------

function ExtraFieldsEditor({
  form,
  setForm,
  editing,
}: {
  form: FormState
  setForm: (f: FormState) => void
  editing: boolean
}) {
  const { t } = useTranslation()

  function update(idx: number, patch: Partial<ExtraFieldEntry>) {
    const next = form.extra_fields.slice()
    next[idx] = { ...next[idx], ...patch }
    setForm({ ...form, extra_fields: next })
  }

  function remove(idx: number) {
    const next = form.extra_fields.slice()
    next.splice(idx, 1)
    setForm({ ...form, extra_fields: next })
  }

  function add() {
    setForm({ ...form, extra_fields: [...form.extra_fields, { key: '', value: '' }] })
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label>{t('credentials.extraFields')}</Label>
        <Button type="button" variant="outline" size="sm" onClick={add}>
          <Plus className="h-3.5 w-3.5 me-1" />
          {t('credentials.addExtraField')}
        </Button>
      </div>
      {form.extra_fields.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t('credentials.extraFieldsHint')}
        </p>
      ) : (
        <div className="space-y-2">
          {form.extra_fields.map((entry, idx) => (
            <div key={idx} className="flex gap-2 items-start">
              <Input
                value={entry.key}
                onChange={(e) => update(idx, { key: e.target.value })}
                placeholder={t('credentials.extraFieldKey')}
                className="flex-1"
                autoComplete="off"
              />
              <Input
                value={entry.value}
                onChange={(e) => update(idx, { value: e.target.value })}
                placeholder={
                  editing && !entry.value
                    ? t('credentials.unchangedPlaceholder')
                    : t('credentials.extraFieldValue')
                }
                className="flex-1"
                type="password"
                autoComplete="off"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => remove(idx)}
                title={t('credentials.removeExtraField')}
              >
                <X className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function FieldText({
  id,
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  type?: 'text' | 'password'
  placeholder?: string
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="off"
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tool links dialog
// ---------------------------------------------------------------------------

function ToolLinksDialog({
  cred,
  availableTools,
  onClose,
  onChanged,
}: {
  cred: Credential
  availableTools: { name: string; summary: string; category: string }[]
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const [toolName, setToolName] = useState<string>('')

  const linkedNames = useMemo(
    () => new Set(cred.tool_links.map((l) => l.tool_name)),
    [cred.tool_links],
  )
  const linkable = useMemo(
    () => availableTools.filter((tool) => !linkedNames.has(tool.name)),
    [availableTools, linkedNames],
  )

  const linkMut = useMutation({
    mutationFn: () => linkCredentialToTool(orgId!, cred.id, { tool_name: toolName }),
    onSuccess: () => {
      onChanged()
      setToolName('')
      toast.success(t('credentials.linkAdded'))
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const unlinkMut = useMutation({
    mutationFn: (linkId: string) => unlinkCredentialFromTool(orgId!, cred.id, linkId),
    onSuccess: () => {
      onChanged()
      toast.success(t('credentials.linkRemoved'))
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const activateMut = useMutation({
    mutationFn: (linkId: string) => activateCredentialLink(orgId!, cred.id, linkId),
    onSuccess: () => {
      onChanged()
      toast.success(t('credentials.linkActivated'))
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('credentials.manageLinksTitle', { label: cred.label })}</DialogTitle>
          <DialogDescription>{t('credentials.manageLinksDesc')}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {cred.tool_links.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('credentials.noLinks')}</p>
          ) : (
            <div className="space-y-2">
              {cred.tool_links.map((link) => (
                <div
                  key={link.id}
                  className="flex items-center justify-between gap-2 p-2 border rounded-md"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-mono truncate">{link.tool_name}</span>
                    {link.is_active && (
                      <Badge variant="default" className="gap-1">
                        <Check className="h-3 w-3" />
                        {t('credentials.active')}
                      </Badge>
                    )}
                  </div>
                  <div className="flex gap-1 shrink-0">
                    {!link.is_active && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => activateMut.mutate(link.id)}
                      >
                        {t('credentials.activate')}
                      </Button>
                    )}
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => unlinkMut.mutate(link.id)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <Separator />

          <div>
            <Label>{t('credentials.addLink')}</Label>
            <div className="flex gap-2 mt-1">
              <Select value={toolName} onValueChange={setToolName}>
                <SelectTrigger className="flex-1">
                  <SelectValue placeholder={t('credentials.selectTool')} />
                </SelectTrigger>
                <SelectContent>
                  {linkable.length === 0 ? (
                    <div className="p-2 text-xs text-muted-foreground">
                      {t('credentials.noToolsAvailable')}
                    </div>
                  ) : (
                    linkable.map((tool) => (
                      <SelectItem key={tool.name} value={tool.name}>
                        {tool.name}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
              <Button
                onClick={() => linkMut.mutate()}
                disabled={!toolName || linkMut.isPending}
              >
                {linkMut.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  t('common.add')
                )}
              </Button>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('common.close')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
