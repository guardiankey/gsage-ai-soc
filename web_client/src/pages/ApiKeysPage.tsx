import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Copy, Check, Eye, EyeOff, Loader2, Key } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Separator } from '@/components/ui/separator'
import {
  listMyApiKeys,
  createMyApiKey,
  deleteMyApiKey,
  listOrgApiKeys,
  createOrgApiKey,
  deleteOrgApiKey,
  type ApiKey,
} from '@/api/api-keys'
import { extractApiError } from '@/api/client'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { toast } from 'sonner'

export default function ApiKeysPage() {
  const { t } = useTranslation()
  const { user, orgId, hasPermission } = useAuth()
  const queryClient = useQueryClient()

  // Personal keys
  const [myKeyOpen, setMyKeyOpen] = useState(false)
  const [myKeyName, setMyKeyName] = useState('')
  const [myNewKey, setMyNewKey] = useState<string | null>(null)
  const [myDeleteId, setMyDeleteId] = useState<string | null>(null)

  // Org keys
  const [orgPage, setOrgPage] = useState(1)
  const [orgKeyOpen, setOrgKeyOpen] = useState(false)
  const [orgKeyName, setOrgKeyName] = useState('')
  const [orgNewKey, setOrgNewKey] = useState<string | null>(null)
  const [orgDeleteId, setOrgDeleteId] = useState<string | null>(null)

  const isAdmin = hasPermission('apikeys:manage')

  const { data: myKeysData, isLoading: myKeysLoading } = useQuery({
    queryKey: ['my-api-keys', orgId],
    queryFn: () => listMyApiKeys(orgId!),
    enabled: !!orgId,
  })

  const { data: orgKeysData, isLoading: orgKeysLoading } = useQuery({
    queryKey: ['org-api-keys', orgId, orgPage],
    queryFn: () => listOrgApiKeys(orgId!, orgPage, 20),
    enabled: !!orgId && isAdmin,
  })

  const createMyMut = useMutation({
    mutationFn: () => createMyApiKey(orgId!, myKeyName.trim()),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['my-api-keys', orgId] })
      setMyNewKey(data.raw_key)
      setMyKeyName('')
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const deleteMyMut = useMutation({
    mutationFn: () => deleteMyApiKey(orgId!, myDeleteId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['my-api-keys', orgId] })
      setMyDeleteId(null)
      toast.success(t('apiKeys.deleted'))
    },
    onError: (err) => {
      setMyDeleteId(null)
      toast.error(extractApiError(err))
    },
  })

  const createOrgMut = useMutation({
    mutationFn: () => createOrgApiKey(orgId!, orgKeyName.trim()),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['org-api-keys', orgId] })
      setOrgNewKey(data.raw_key)
      setOrgKeyName('')
    },
    onError: (err) => toast.error(extractApiError(err)),
  })

  const deleteOrgMut = useMutation({
    mutationFn: () => deleteOrgApiKey(orgId!, orgDeleteId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['org-api-keys', orgId] })
      setOrgDeleteId(null)
      toast.success(t('apiKeys.deleted'))
    },
    onError: (err) => {
      setOrgDeleteId(null)
      toast.error(extractApiError(err))
    },
  })

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-3xl mx-auto">
        <div className="mb-6">
          <h1 className="text-2xl font-bold">{t('apiKeys.title')}</h1>
          <p className="text-muted-foreground text-sm mt-1">{t('apiKeys.subtitle')}</p>
        </div>

        <Tabs defaultValue="personal">
          <TabsList>
            <TabsTrigger value="personal">{t('apiKeys.personal')}</TabsTrigger>
            {isAdmin && <TabsTrigger value="org">{t('apiKeys.organization')}</TabsTrigger>}
          </TabsList>

          {/* Personal keys */}
          <TabsContent value="personal" className="mt-4">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between py-3">
                <CardTitle className="text-base">{t('apiKeys.personalKeys')}</CardTitle>
                <Button size="sm" onClick={() => setMyKeyOpen(true)}>
                  <Plus className="h-4 w-4 me-2" />
                  {t('apiKeys.create')}
                </Button>
              </CardHeader>
              <Separator />
              <CardContent className="p-0">
                {myKeysLoading ? (
                  <div className="p-4 space-y-2">
                    {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
                  </div>
                ) : (myKeysData ?? []).length === 0 ? (
                  <div className="p-8 text-center text-muted-foreground">
                    <Key className="h-8 w-8 mx-auto mb-2 opacity-40" />
                    <p className="text-sm">{t('apiKeys.noKeys')}</p>
                  </div>
                ) : (
                  <ApiKeyList
                    keys={myKeysData ?? []}
                    onDelete={(id) => setMyDeleteId(id)}
                  />
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* Org keys — admin only */}
          {isAdmin && (
            <TabsContent value="org" className="mt-4">
              <Card>
                <CardHeader className="flex flex-row items-center justify-between py-3">
                  <CardTitle className="text-base">{t('apiKeys.orgKeys')}</CardTitle>
                  <Button size="sm" onClick={() => setOrgKeyOpen(true)}>
                    <Plus className="h-4 w-4 me-2" />
                    {t('apiKeys.create')}
                  </Button>
                </CardHeader>
                <Separator />
                <CardContent className="p-0">
                  {orgKeysLoading ? (
                    <div className="p-4 space-y-2">
                      {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
                    </div>
                  ) : (orgKeysData?.items ?? []).length === 0 ? (
                    <div className="p-8 text-center text-muted-foreground">
                      <Key className="h-8 w-8 mx-auto mb-2 opacity-40" />
                      <p className="text-sm">{t('apiKeys.noKeys')}</p>
                    </div>
                  ) : (
                    <ApiKeyList
                      keys={orgKeysData?.items ?? []}
                      onDelete={(id) => setOrgDeleteId(id)}
                    />
                  )}
                </CardContent>
              </Card>
              {orgKeysData && (
                <Pagination
                  page={orgPage}
                  totalPages={calcTotalPages(orgKeysData.total, 20)}
                  onPageChange={setOrgPage}
                  className="mt-3"
                />
              )}
            </TabsContent>
          )}
        </Tabs>
      </div>

      {/* Create personal key dialog */}
      <CreateKeyDialog
        open={myKeyOpen}
        title={t('apiKeys.createPersonal')}
        name={myKeyName}
        onNameChange={setMyKeyName}
        newKey={myNewKey}
        onCreate={() => createMyMut.mutate()}
        onClose={() => { setMyKeyOpen(false); setMyNewKey(null); setMyKeyName('') }}
        isLoading={createMyMut.isPending}
      />

      {/* Create org key dialog */}
      <CreateKeyDialog
        open={orgKeyOpen}
        title={t('apiKeys.createOrg')}
        name={orgKeyName}
        onNameChange={setOrgKeyName}
        newKey={orgNewKey}
        onCreate={() => createOrgMut.mutate()}
        onClose={() => { setOrgKeyOpen(false); setOrgNewKey(null); setOrgKeyName('') }}
        isLoading={createOrgMut.isPending}
      />

      {/* Delete personal key */}
      <DeleteKeyDialog
        open={!!myDeleteId}
        onClose={() => setMyDeleteId(null)}
        onDelete={() => deleteMyMut.mutate()}
        isLoading={deleteMyMut.isPending}
      />

      {/* Delete org key */}
      <DeleteKeyDialog
        open={!!orgDeleteId}
        onClose={() => setOrgDeleteId(null)}
        onDelete={() => deleteOrgMut.mutate()}
        isLoading={deleteOrgMut.isPending}
      />
    </div>
  )
}

function ApiKeyList({ keys, onDelete }: { keys: ApiKey[]; onDelete: (id: string) => void }) {
  const { t } = useTranslation()
  return (
    <div className="divide-y">
      {keys.map((key) => (
        <div key={key.id} className="flex items-center gap-3 px-4 py-3 group">
          <Key className={`h-4 w-4 shrink-0 ${key.is_active ? 'text-muted-foreground' : 'text-muted-foreground/40'}`} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <p className={`text-sm font-medium ${!key.is_active ? 'line-through text-muted-foreground' : ''}`}>{key.name}</p>
              {!key.is_active && (
                <Badge variant="outline" className="text-xs">{t('apiKeys.revoked')}</Badge>
              )}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <code className="text-xs text-muted-foreground">
                {key.key_prefix ? `${key.key_prefix}••••••` : '••••••••'}
              </code>
              <span className="text-xs text-muted-foreground">
                {t('apiKeys.created')}: {formatDistanceToNow(new Date(key.created_at), { addSuffix: true })}
              </span>
            </div>
          </div>
          {key.is_active && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 opacity-0 group-hover:opacity-100 hover:text-destructive"
              onClick={() => onDelete(key.id)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      ))}
    </div>
  )
}

function CreateKeyDialog({
  open,
  title,
  name,
  onNameChange,
  newKey,
  onCreate,
  onClose,
  isLoading,
}: {
  open: boolean
  title: string
  name: string
  onNameChange: (v: string) => void
  newKey: string | null
  onCreate: () => void
  onClose: () => void
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    if (!newKey) return
    await navigator.clipboard.writeText(newKey)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="sr-only">{t('apiKeys.subtitle')}</DialogDescription>
        </DialogHeader>

        {newKey ? (
          <div className="space-y-3">
            <div className="rounded-lg bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 p-3">
              <p className="text-xs text-green-700 dark:text-green-300 font-medium mb-1">
                {t('apiKeys.copyNow')}
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 text-xs break-all">{newKey}</code>
                <Button size="icon" variant="ghost" className="h-7 w-7 shrink-0" onClick={handleCopy}>
                  {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{t('apiKeys.copyWarning')}</p>
          </div>
        ) : (
          <div>
            <Label htmlFor="key-name">{t('apiKeys.keyName')}</Label>
            <Input
              id="key-name"
              autoFocus
              value={name}
              onChange={(e) => onNameChange(e.target.value)}
              placeholder={t('apiKeys.keyNamePlaceholder')}
              onKeyDown={(e) => e.key === 'Enter' && onCreate()}
            />
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{newKey ? t('common.close') : t('common.cancel')}</Button>
          {!newKey && (
            <Button onClick={onCreate} disabled={isLoading || !name.trim()}>
              {isLoading && <Loader2 className="h-4 w-4 animate-spin me-2" />}
              {t('common.create')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DeleteKeyDialog({
  open,
  onClose,
  onDelete,
  isLoading,
}: {
  open: boolean
  onClose: () => void
  onDelete: () => void
  isLoading: boolean
}) {
  const { t } = useTranslation()
  return (
    <AlertDialog open={open} onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('apiKeys.deleteTitle')}</AlertDialogTitle>
          <AlertDialogDescription>{t('apiKeys.deleteDesc')}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
          <AlertDialogAction onClick={onDelete} className="bg-destructive hover:bg-destructive/90">
            {isLoading && <Loader2 className="h-4 w-4 animate-spin me-2" />}
            {t('common.delete')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
