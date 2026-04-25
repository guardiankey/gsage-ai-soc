import { useState, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, FileText, Search, Upload, Trash2, Users, User, Building2 } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import {
  listFiles,
  downloadFile,
  uploadFile,
  deleteFile,
  type GeneratedFile,
} from '@/api/files'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { formatFileSize } from '@/lib/utils'
import { toast } from 'sonner'

type TabKey = 'generated' | 'template' | 'attachment'

export default function FilesPage() {
  const { t } = useTranslation()
  const { orgId, deptId, hasPermission } = useAuth()
  const queryClient = useQueryClient()

  const [tab, setTab] = useState<TabKey>('generated')
  const [search, setSearch] = useState('')
  const [toolFilter, setToolFilter] = useState('')
  const [page, setPage] = useState(1)

  // Upload dialog state
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadFile_, setUploadFile_] = useState<File | null>(null)
  const [uploadDescription, setUploadDescription] = useState('')
  const [uploadScope, setUploadScope] = useState<'user' | 'organization' | 'department'>('user')
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Delete confirm state
  const [deleteItem, setDeleteItem] = useState<GeneratedFile | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['files', orgId, tab, page, toolFilter],
    queryFn: () => listFiles(orgId!, page, 20, toolFilter || undefined, false, tab),
    enabled: !!orgId,
  })

  const uploadMut = useMutation({
    mutationFn: () => {
      if (!uploadFile_ || !orgId) throw new Error('No file selected')
      return uploadFile(orgId, uploadFile_, uploadDescription || undefined, uploadScope)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['files', orgId] })
      setUploadOpen(false)
      setUploadFile_(null)
      setUploadDescription('')
      setUploadScope('user')
      toast.success(t('files.uploadSuccess'))
    },
    onError: (err: Error) => {
      toast.error(err.message || t('common.error'))
    },
  })

  const deleteMut = useMutation({
    mutationFn: (fileId: string) => deleteFile(orgId!, fileId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['files', orgId] })
      setDeleteItem(null)
      toast.success(t('files.deleteSuccess'))
    },
    onError: (err: Error) => {
      toast.error(err.message || t('common.error'))
    },
  })

  const handleDownload = async (file: GeneratedFile) => {
    try {
      await downloadFile(orgId!, file.id, file.filename)
    } catch {
      toast.error(t('common.error'))
    }
  }

  const items = (data?.items ?? []).filter((f) =>
    !search || f.filename.toLowerCase().includes(search.toLowerCase())
  )

  const uniqueTools = Array.from(
    new Set((data?.items ?? []).map((f) => f.tool_name).filter(Boolean))
  )

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-5xl mx-auto">
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">{t('files.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('files.subtitle')}</p>
          </div>
          {tab === 'template' && hasPermission('files:upload') && (
            <Button onClick={() => setUploadOpen(true)} size="sm" className="shrink-0">
              <Upload className="h-4 w-4 me-1.5" />
              {t('files.upload')}
            </Button>
          )}
        </div>

        {/* Tabs */}
        <div className="flex gap-1.5 mb-5">
          {(['generated', 'template', 'attachment'] as TabKey[]).map((key) => (
            <Button
              key={key}
              variant={tab === key ? 'secondary' : 'outline'}
              size="sm"
              onClick={() => { setTab(key); setPage(1); setToolFilter('') }}
            >
              {t(`files.${key}`)}
            </Button>
          ))}
        </div>

        {/* Filters */}
        <div className="flex gap-2 mb-4 flex-wrap">
          <div className="relative flex-1 min-w-48">
            <Search className="absolute start-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder={t('files.searchPlaceholder')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="ps-8"
            />
          </div>
          {tab === 'generated' && uniqueTools.length > 0 && (
            <div className="flex gap-1.5 flex-wrap">
              <Button
                variant={!toolFilter ? 'secondary' : 'outline'}
                size="sm"
                onClick={() => { setToolFilter(''); setPage(1) }}
              >
                {t('common.all')}
              </Button>
              {uniqueTools.map((tool) => (
                <Button
                  key={tool}
                  variant={toolFilter === tool ? 'secondary' : 'outline'}
                  size="sm"
                  onClick={() => { setToolFilter(tool!); setPage(1) }}
                >
                  {tool}
                </Button>
              ))}
            </div>
          )}
        </div>

        {/* File list */}
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-16 w-full" />)}
          </div>
        ) : items.length === 0 ? (
          <Card>
            <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
              <FileText className="h-10 w-10 opacity-40" />
              <p className="text-sm">
                {search ? t('common.noResults') : tab === 'template' ? t('files.noTemplates') : tab === 'attachment' ? t('files.noAttachments') : t('files.noFiles')}
              </p>
              {tab === 'template' && !search && hasPermission('files:upload') && (
                <Button variant="outline" size="sm" onClick={() => setUploadOpen(true)}>
                  <Upload className="h-4 w-4 me-1.5" />
                  {t('files.upload')}
                </Button>
              )}
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-2">
            {items.map((file) => (
              <Card key={file.id} className="group hover:shadow-sm transition-shadow">
                <CardContent className="p-4 flex items-center gap-3">
                  <FileText className="h-5 w-5 shrink-0 text-muted-foreground" />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm truncate">{file.filename}</p>
                    <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                      {file.tool_name && tab === 'generated' && (
                        <code className="text-xs bg-muted rounded px-1.5 py-0.5">{file.tool_name}</code>
                      )}
                      {file.size_bytes && (
                        <span className="text-xs text-muted-foreground">{formatFileSize(file.size_bytes)}</span>
                      )}
                      {tab === 'template' && file.scope && (
                        <Badge variant="outline" className="text-xs gap-1">
                          {file.scope === 'organization'
                            ? <><Users className="h-3 w-3" />{t('files.scopeOrg')}</>
                            : file.scope === 'department'
                              ? <><Building2 className="h-3 w-3" />{t('files.scopeDept')}</>
                              : <><User className="h-3 w-3" />{t('files.scopeUser')}</>}
                        </Badge>
                      )}
                      {file.expires_at && (
                        <Badge variant="warning" className="text-xs">
                          {t('files.expires')}: {formatDistanceToNow(new Date(file.expires_at), { addSuffix: true })}
                        </Badge>
                      )}
                    </div>
                  </div>
                  <span className="text-xs text-muted-foreground shrink-0">
                    {formatDistanceToNow(new Date(file.created_at), { addSuffix: true })}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 opacity-60 group-hover:opacity-100 hover:text-primary"
                    onClick={() => handleDownload(file)}
                    title={t('files.download')}
                  >
                    <Download className="h-4 w-4" />
                  </Button>
                  {(tab === 'template' || tab === 'attachment') && ( hasPermission('files:delete:all') || hasPermission('files:delete')) && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 opacity-60 group-hover:opacity-100 hover:text-destructive"
                      onClick={() => setDeleteItem(file)}
                      title={t('files.deleteTemplate')}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  )}
                </CardContent>
              </Card>
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

      {/* Upload dialog */}
      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('files.uploadTitle')}</DialogTitle>
            <DialogDescription className="sr-only">{t('files.subtitle')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <Label>{t('files.selectFile')}</Label>
              <div
                className="border-2 border-dashed rounded-md p-6 text-center cursor-pointer hover:border-primary/60 transition-colors"
                onClick={() => fileInputRef.current?.click()}
              >
                {uploadFile_ ? (
                  <p className="text-sm font-medium">{uploadFile_.name}</p>
                ) : (
                  <p className="text-sm text-muted-foreground">{t('files.uploadDescription')}</p>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                className="hidden"
                onChange={(e) => setUploadFile_(e.target.files?.[0] ?? null)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>{t('files.description')}</Label>
              <Input
                placeholder={t('files.descriptionPlaceholder')}
                value={uploadDescription}
                onChange={(e) => setUploadDescription(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>{t('files.scope')}</Label>
              <div className="flex gap-2">
                {(['user', 'organization', 'department'] as const).map((s) => (
                  <Button
                    key={s}
                    type="button"
                    variant={uploadScope === s ? 'secondary' : 'outline'}
                    size="sm"
                    onClick={() => setUploadScope(s)}
                  >
                    {s === 'organization' ? t('files.scopeOrg') : s === 'department' ? t('files.scopeDept') : t('files.scopeUser')}
                  </Button>
                ))}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{t('files.allowedTypes')}</p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUploadOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={() => uploadMut.mutate()}
              disabled={!uploadFile_ || uploadMut.isPending}
            >
              {uploadMut.isPending ? t('common.loading') : t('files.upload')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={!!deleteItem} onOpenChange={(open) => !open && setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('files.deleteTemplate')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('files.deleteConfirm', { name: deleteItem?.filename ?? '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteItem && deleteMut.mutate(deleteItem.id)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {t('common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
