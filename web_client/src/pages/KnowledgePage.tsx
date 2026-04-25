import { useState, useCallback, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Search, Upload, Plus, Trash2, FileText, Loader2, X, CheckCircle2, AlertCircle, Users, User, Building2, Download } from 'lucide-react'
import { useDropzone } from 'react-dropzone'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  searchKnowledge,
  listKnowledge,
  addKnowledge,
  deleteKnowledge,
  ingestDocument,
  getIngestStatus,
  listIngestJobs,
  downloadIngestOriginal,
  type KnowledgeDocument,
  type KnowledgeSearchResult,
} from '@/api/knowledge'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'

export default function KnowledgePage() {
  const { t } = useTranslation()
  const { orgId, deptId, hasPermission } = useAuth()
  const queryClient = useQueryClient()

  const [tab, setTab] = useState('search')
  const [docsPage, setDocsPage] = useState(1)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[]>([])
  const [isSearching, setIsSearching] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [deleteItem, setDeleteItem] = useState<KnowledgeDocument | null>(null)
  const [ingestJobsPage, setIngestJobsPage] = useState(1)
  const [downloadingJobId, setDownloadingJobId] = useState<string | null>(null)

  // Add text form
  const [newTitle, setNewTitle] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [newContent, setNewContent] = useState('')
  const [newUrl, setNewUrl] = useState('')

  // Ingest – multi-job tracking
  interface IngestTracker {
    jobId: string
    fileName: string
    status: string
    scope: 'org' | 'user' | 'dept'
    error?: string
  }
  const [ingestJobs, setIngestJobs] = useState<IngestTracker[]>([])
  const [ingestScope, setIngestScope] = useState<'org' | 'user' | 'dept'>('org')
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const { data: listData, isLoading: listLoading } = useQuery({
    queryKey: ['knowledge', orgId, docsPage],
    queryFn: () => listKnowledge(orgId!, docsPage, 20),
    enabled: !!orgId && tab === 'documents',
  })

  const { data: ingestJobsData, isLoading: ingestJobsLoading } = useQuery({
    queryKey: ['knowledge-ingest-jobs', orgId, ingestJobsPage],
    queryFn: () => listIngestJobs(orgId!, ingestJobsPage, 20),
    enabled: !!orgId && tab === 'ingest',
  })

  // Poll active ingest jobs
  useEffect(() => {
    const activeJobs = ingestJobs.filter(
      (j) => j.status !== 'completed' && j.status !== 'failed'
    )
    if (activeJobs.length === 0) {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
      return
    }
    const poll = async () => {
      if (!orgId) return
      const updates: IngestTracker[] = [...ingestJobs]
      let changed = false
      for (const job of updates) {
        if (job.status === 'completed' || job.status === 'failed') continue
        try {
          const res = await getIngestStatus(orgId, job.jobId)
          if (res.status !== job.status || res.error_message !== job.error) {
            job.status = res.status
            job.error = res.error_message
            changed = true
            if (res.status === 'completed') {
              queryClient.invalidateQueries({ queryKey: ['knowledge', orgId] })
            }
          }
        } catch {
          // ignore transient errors
        }
      }
      if (changed) setIngestJobs([...updates])
    }
    // Initial poll
    poll()
    pollingRef.current = setInterval(poll, 3000)
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
    }
  }, [ingestJobs.filter((j) => j.status !== 'completed' && j.status !== 'failed').map((j) => j.jobId).join(','), orgId])

  const handleSearch = async () => {
    if (!orgId || !searchQuery.trim()) return
    setIsSearching(true)
    try {
      const res = await searchKnowledge(orgId, searchQuery)
      setSearchResults(res.results ?? [])
    } catch {
      toast.error(t('common.error'))
    } finally {
      setIsSearching(false)
    }
  }

  const addMut = useMutation({
    mutationFn: () =>
      addKnowledge(
        orgId!,
        newTitle.trim(),
        newContent.trim() || undefined,
        newDescription.trim() || undefined,
        newUrl.trim() || undefined,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge', orgId] })
      setAddOpen(false)
      setNewTitle('')
      setNewDescription('')
      setNewContent('')
      setNewUrl('')
      toast.success(t('knowledge.addSuccess'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const deleteMut = useMutation({
    mutationFn: () => deleteKnowledge(orgId!, deleteItem!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge', orgId] })
      setDeleteItem(null)
      toast.success(t('knowledge.deleteSuccess'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const onDrop = useCallback(
    async (files: File[]) => {
      if (!orgId || files.length === 0) return
      const newJobs: IngestTracker[] = []
      for (const file of files) {
        try {
          const res = await ingestDocument(orgId, file, ingestScope)
          newJobs.push({
            jobId: res.job_id,
            fileName: file.name,
            status: res.status ?? 'QUEUED',
            scope: ingestScope,
          })
          toast.info(t('knowledge.ingestStarted') + `: ${file.name}`)
        } catch {
          toast.error(`${t('common.error')}: ${file.name}`)
        }
      }
      if (newJobs.length > 0) {
        setIngestJobs((prev) => [...newJobs, ...prev])
      }
    },
    [orgId, ingestScope, t]
  )

  const onDropRejected = useCallback(
    (fileRejections: any[]) => {
      for (const rejection of fileRejections) {
        const name = rejection.file?.name ?? ''
        const errors = rejection.errors ?? []
        if (errors.some((e: any) => e.code === 'file-too-large')) {
          toast.error(t('knowledge.fileTooLarge', { name }))
        } else if (errors.some((e: any) => e.code === 'file-invalid-type')) {
          toast.error(t('knowledge.unsupportedFormat', { name }))
        } else {
          toast.error(`${t('common.error')}: ${name}`)
        }
      }
    },
    [t]
  )

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    onDropRejected,
    accept: {
      'application/pdf': ['.pdf'],
      'text/plain': ['.txt'],
      'text/markdown': ['.md'],
      'text/csv': ['.csv'],
      'text/html': ['.html', '.htm'],
      'application/msword': ['.doc'],
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['.docx'],
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'application/vnd.openxmlformats-officedocument.presentationml.presentation': ['.pptx'],
      'application/json': ['.json'],
      'application/xml': ['.xml'],
      'text/xml': ['.xml'],
      'message/rfc822': ['.eml'],
      'application/zip': ['.zip'],
      'application/x-tar': ['.tar', '.tar.gz', '.tar.bz2', '.tar.xz'],
      'application/gzip': ['.gz'],
    },
    multiple: true,
    maxSize: 10 * 1024 * 1024, // 10 MB
  })

  const removeJob = useCallback((jobId: string) => {
    setIngestJobs((prev) => prev.filter((j) => j.jobId !== jobId))
  }, [])

  const handleDownload = useCallback(async (jobId: string, filename: string) => {
    if (!orgId) return
    setDownloadingJobId(jobId)
    try {
      await downloadIngestOriginal(orgId, jobId, filename)
    } catch {
      toast.error(t('knowledge.downloadError'))
    } finally {
      setDownloadingJobId(null)
    }
  }, [orgId, t])

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold">{t('knowledge.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('knowledge.subtitle')}</p>
          </div>
          <div className="flex gap-2">
            {hasPermission('knowledge:write') && (
              <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="h-4 w-4 me-2" />
                {t('knowledge.addDocument')}
              </Button>
            )}
          </div>
        </div>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="search">{t('knowledge.search')}</TabsTrigger>
            <TabsTrigger value="documents">{t('knowledge.documents')}</TabsTrigger>
            {hasPermission('knowledge:write') && (
              <TabsTrigger value="ingest">{t('knowledge.ingest')}</TabsTrigger>
            )}
          </TabsList>

          {/* Search tab */}
          <TabsContent value="search" className="mt-4">
            <div className="flex gap-2 mb-4">
              <Input
                placeholder={t('knowledge.searchPlaceholder')}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                className="flex-1"
              />
              <Button onClick={handleSearch} disabled={isSearching || !searchQuery.trim()}>
                {isSearching
                  ? <Loader2 className="h-4 w-4 animate-spin" />
                  : <Search className="h-4 w-4" />
                }
                <span className="ms-2 hidden sm:inline">{t('knowledge.search')}</span>
              </Button>
            </div>

            {searchResults.length > 0 && (
              <div className="space-y-3">
                {searchResults.map((r, i) => (
                  <Card key={i} className="group">
                    <CardContent className="p-4">
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <h3 className="font-medium text-sm truncate">{r.name ?? `${t('knowledge.result')} ${i + 1}`}</h3>
                            {r.score !== undefined && (
                              <Badge variant="secondary" className="text-xs shrink-0">
                                {(r.score * 100).toFixed(0)}%
                              </Badge>
                            )}
                          </div>
                          <p className="text-sm text-muted-foreground line-clamp-4">{r.content}</p>
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}

            {searchResults.length === 0 && !isSearching && searchQuery && (
              <p className="text-center text-muted-foreground py-8 text-sm">{t('common.noResults')}</p>
            )}
          </TabsContent>

          {/* Documents tab */}
          <TabsContent value="documents" className="mt-4">
            {listLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-16 w-full" />)}
              </div>
            ) : (listData?.items ?? []).length === 0 ? (
              <Card>
                <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
                  <FileText className="h-10 w-10 opacity-40" />
                  <p className="text-sm">{t('knowledge.noDocuments')}</p>
                  {hasPermission('knowledge:write') && (
                    <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}>
                      <Plus className="h-4 w-4 me-2" />
                      {t('knowledge.addDocument')}
                    </Button>
                  )}
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-2">
                {(listData?.items ?? []).map((item) => (
                  <Card key={item.id} className="group">
                    <CardContent className="p-4 flex items-center gap-3">
                      <FileText className="h-5 w-5 shrink-0 text-muted-foreground" />
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-sm truncate">{item.name}</p>
                      </div>
                      {item.scope && (
                        <Badge variant="outline" className="text-xs gap-1 shrink-0">
                          {item.scope === 'org'
                            ? <><Users className="h-3 w-3" />{t('knowledge.scopeOrg')}</>
                            : item.scope === 'dept'
                              ? <><Building2 className="h-3 w-3" />{t('knowledge.scopeDept')}</>
                              : <><User className="h-3 w-3" />{t('knowledge.scopeUser')}</>}
                        </Badge>
                      )}
                      {hasPermission('knowledge:delete') && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 opacity-0 group-hover:opacity-100 hover:text-destructive"
                          onClick={() => setDeleteItem(item)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
            {listData && (
              <Pagination
                page={docsPage}
                totalPages={calcTotalPages(listData.total, 20)}
                onPageChange={setDocsPage}
                className="mt-4"
              />
            )}
          </TabsContent>

          {/* Ingest tab */}
          <TabsContent value="ingest" className="mt-4">
            <div className="space-y-4">
              {/* Scope selector */}
              <div className="space-y-1.5">
                <Label>{t('knowledge.scope')}</Label>
                <div className="flex gap-2">
                  {(['org', 'dept', 'user'] as const).filter((s) => s !== 'dept' || !!deptId).map((s) => (
                    <Button
                      key={s}
                      type="button"
                      variant={ingestScope === s ? 'secondary' : 'outline'}
                      size="sm"
                      onClick={() => setIngestScope(s)}
                    >
                      {s === 'org'
                        ? <><Users className="h-3 w-3 me-1" />{t('knowledge.scopeOrg')}</>
                        : s === 'dept'
                          ? <><Building2 className="h-3 w-3 me-1" />{t('knowledge.scopeDept')}</>
                          : <><User className="h-3 w-3 me-1" />{t('knowledge.scopeUser')}</>}
                    </Button>
                  ))}
                </div>
              </div>

              <div
                {...getRootProps()}
                className={cn(
                  'border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-colors',
                  isDragActive
                    ? 'border-primary bg-primary/5'
                    : 'border-muted-foreground/30 hover:border-primary/50 hover:bg-muted/30'
                )}
              >
                <input {...getInputProps()} />
                <Upload className="h-10 w-10 mx-auto mb-3 text-muted-foreground" />
                <p className="font-medium text-sm mb-1">
                  {isDragActive ? t('knowledge.dropHere') : t('knowledge.uploadInstructions')}
                </p>
                <p className="text-xs text-muted-foreground">{t('knowledge.uploadFormats')}</p>
              </div>

              {ingestJobs.length > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium">{t('knowledge.ingestStatus')}</p>
                  {ingestJobs.map((job) => (
                    <Card key={job.jobId}>
                      <CardContent className="p-3 flex items-center gap-3">
                        {job.status === 'completed' ? (
                          <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />
                        ) : job.status === 'failed' ? (
                          <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />
                        ) : (
                          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
                        )}
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">{job.fileName}</p>
                          {job.error && (
                            <p className="text-xs text-destructive mt-0.5">{job.error}</p>
                          )}
                        </div>
                        <Badge variant="outline" className="text-xs gap-1 shrink-0">
                          {job.scope === 'org'
                            ? <><Users className="h-3 w-3" />{t('knowledge.scopeOrg')}</>
                            : job.scope === 'dept'
                              ? <><Building2 className="h-3 w-3" />{t('knowledge.scopeDept')}</>
                              : <><User className="h-3 w-3" />{t('knowledge.scopeUser')}</>}
                        </Badge>
                        <Badge
                          variant={
                            job.status === 'completed'
                              ? 'success'
                              : job.status === 'failed'
                              ? 'destructive'
                              : job.status === 'processing'
                              ? 'warning'
                              : 'secondary'
                          }
                          className="shrink-0"
                        >
                          {job.status}
                        </Badge>
                        {(job.status === 'completed' || job.status === 'failed') && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6 shrink-0"
                            onClick={() => removeJob(job.jobId)}
                          >
                            <X className="h-3 w-3" />
                          </Button>
                        )}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              )}

              {/* Ingest job history with download */}
              <div>
                <p className="text-sm font-medium mb-2">{t('knowledge.ingestHistory')}</p>
                {ingestJobsLoading ? (
                  <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div>
                ) : (ingestJobsData?.items ?? []).length === 0 ? (
                  <p className="text-sm text-muted-foreground">{t('knowledge.noIngestHistory')}</p>
                ) : (
                  <div className="space-y-2">
                    {(ingestJobsData?.items ?? []).map((job) => (
                      <Card key={job.job_id}>
                        <CardContent className="p-3 flex items-center gap-3">
                          {job.status === 'completed' ? (
                            <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />
                          ) : job.status === 'failed' ? (
                            <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />
                          ) : (
                            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
                          )}
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium truncate">{job.filename}</p>
                            {job.error_message && (
                              <p className="text-xs text-destructive mt-0.5">{job.error_message}</p>
                            )}
                            {job.created_at && (
                              <p className="text-xs text-muted-foreground mt-0.5">
                                {new Date(job.created_at).toLocaleString()}
                              </p>
                            )}
                          </div>
                          <Badge variant="outline" className="text-xs gap-1 shrink-0">
                            {job.scope === 'org'
                              ? <><Users className="h-3 w-3" />{t('knowledge.scopeOrg')}</>
                              : job.scope === 'dept'
                                ? <><Building2 className="h-3 w-3" />{t('knowledge.scopeDept')}</>
                                : <><User className="h-3 w-3" />{t('knowledge.scopeUser')}</>}
                          </Badge>
                          {job.storage_key && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8 shrink-0"
                              title={t('knowledge.downloadOriginal')}
                              disabled={downloadingJobId === job.job_id}
                              onClick={() => handleDownload(job.job_id, job.filename)}
                            >
                              {downloadingJobId === job.job_id
                                ? <Loader2 className="h-4 w-4 animate-spin" />
                                : <Download className="h-4 w-4" />}
                            </Button>
                          )}
                        </CardContent>
                      </Card>
                    ))}
                  </div>
                )}
                {ingestJobsData && (
                  <Pagination
                    page={ingestJobsPage}
                    totalPages={calcTotalPages(ingestJobsData.total, 20)}
                    onPageChange={setIngestJobsPage}
                    className="mt-3"
                  />
                )}
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </div>

      {/* Add document dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('knowledge.addDocument')}</DialogTitle>
            <DialogDescription className="sr-only">{t('knowledge.addDocumentDesc')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label htmlFor="kn-title">{t('knowledge.docTitle')}</Label>
              <Input
                id="kn-title"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder={t('knowledge.docTitlePlaceholder')}
              />
            </div>
            <div>
              <Label htmlFor="kn-description">{t('knowledge.docDescription')} ({t('common.optional')})</Label>
              <Input
                id="kn-description"
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
                placeholder={t('knowledge.docDescriptionPlaceholder')}
              />
            </div>
            <div>
              <Label htmlFor="kn-url">{t('knowledge.docUrl')} ({t('common.optional')})</Label>
              <Input
                id="kn-url"
                value={newUrl}
                onChange={(e) => setNewUrl(e.target.value)}
                placeholder="https://..."
              />
              {newUrl && !newContent && (
                <p className="text-xs text-muted-foreground mt-1">{t('knowledge.docUrlHint')}</p>
              )}
            </div>
            <div>
              <Label htmlFor="kn-content">
                {t('knowledge.docContent')}
                {newUrl && <span className="ms-1 text-muted-foreground text-xs">({t('common.optional')})</span>}
              </Label>
              <Textarea
                id="kn-content"
                rows={6}
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                placeholder={newUrl ? t('knowledge.docContentOrUrl') : t('knowledge.docContentPlaceholder')}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddOpen(false)}>{t('common.cancel')}</Button>
            <Button onClick={() => addMut.mutate()} disabled={addMut.isPending || !newTitle || (!newContent && !newUrl)}>
              {addMut.isPending && <Loader2 className="h-4 w-4 animate-spin me-2" />}
              {t('common.add')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={!!deleteItem} onOpenChange={(o) => !o && setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('knowledge.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('knowledge.deleteDesc', { title: deleteItem?.name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => deleteMut.mutate()} className="bg-destructive hover:bg-destructive/90">
              {t('common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
