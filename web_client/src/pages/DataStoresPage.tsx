import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Database,
  Plus,
  Trash2,
  Pencil,
  ChevronRight,
  ChevronLeft,
  Table2,
  Lock,
  Globe,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuth } from '@/contexts/AuthContext'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import {
  listStores,
  getStore,
  createStore,
  updateStore,
  deleteStore,
  listRecords,
  insertRecord,
  updateRecord,
  deleteRecord,
  type DataStore,
  type DataStoreRecord,
  type DataStoreCreate,
  type DataStoreUpdate,
} from '@/api/datastores'

// ── Helpers ──────────────────────────────────────────────────────────────────

function safeParseJson(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text)
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return null
  } catch {
    return null
  }
}

// ── Store form ────────────────────────────────────────────────────────────────

interface StoreFormState {
  name: string
  description: string
  schema: string
  visibility: 'shared' | 'private'
  maxRecords: string
}

function emptyStoreForm(): StoreFormState {
  return { name: '', description: '', schema: '{}', visibility: 'shared', maxRecords: '500' }
}

function storeToForm(store: DataStore): StoreFormState {
  return {
    name: store.name,
    description: store.description ?? '',
    schema: JSON.stringify(store.schema, null, 2),
    visibility: store.visibility,
    maxRecords: String(store.max_records),
  }
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DataStoresPage() {
  const { t } = useTranslation()
  const { orgId, deptId, hasPermission } = useAuth()
  const queryClient = useQueryClient()

  // list state
  const [page, setPage] = useState(1)

  // selected store for record panel
  const [selectedStore, setSelectedStore] = useState<DataStore | null>(null)
  const [recordsPage, setRecordsPage] = useState(1)

  // dialogs
  const [createOpen, setCreateOpen] = useState(false)
  const [editStore, setEditStore] = useState<DataStore | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<DataStore | null>(null)

  // record dialogs
  const [addRecordOpen, setAddRecordOpen] = useState(false)
  const [editRecord, setEditRecord] = useState<DataStoreRecord | null>(null)
  const [deleteRecordTarget, setDeleteRecordTarget] = useState<DataStoreRecord | null>(null)

  // form state
  const [form, setForm] = useState<StoreFormState>(emptyStoreForm())
  const [recordJson, setRecordJson] = useState('{}')
  const [jsonError, setJsonError] = useState(false)

  // ── Queries ─────────────────────────────────────────────────────────
  const { data: storesData, isLoading: storesLoading } = useQuery({
    queryKey: ['datastores', orgId, deptId, page],
    queryFn: () => listStores(orgId!, deptId!, page, 20),
    enabled: !!orgId && !!deptId,
  })

  const { data: recordsData, isLoading: recordsLoading } = useQuery({
    queryKey: ['datastore-records', orgId, deptId, selectedStore?.id, recordsPage],
    queryFn: () => listRecords(orgId!, deptId!, selectedStore!.id, recordsPage, 20),
    enabled: !!orgId && !!deptId && !!selectedStore,
  })

  // ── Invalidations ───────────────────────────────────────────────────
  const invalidateStores = () =>
    queryClient.invalidateQueries({ queryKey: ['datastores', orgId, deptId] })
  const invalidateRecords = () =>
    queryClient.invalidateQueries({ queryKey: ['datastore-records', orgId, deptId, selectedStore?.id] })

  // ── Store mutations ──────────────────────────────────────────────────
  const createMut = useMutation({
    mutationFn: (payload: DataStoreCreate) => createStore(orgId!, deptId!, payload),
    onSuccess: () => {
      invalidateStores()
      setCreateOpen(false)
      toast.success(t('datastores.createSuccess'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DataStoreUpdate }) =>
      updateStore(orgId!, deptId!, id, payload),
    onSuccess: (updated) => {
      invalidateStores()
      setEditStore(null)
      toast.success(t('datastores.updateSuccess'))
      if (selectedStore?.id === updated.id) setSelectedStore(updated)
    },
    onError: () => toast.error(t('common.error')),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteStore(orgId!, deptId!, id),
    onSuccess: () => {
      invalidateStores()
      setDeleteTarget(null)
      if (selectedStore?.id === deleteTarget?.id) setSelectedStore(null)
      toast.success(t('datastores.deleteSuccess'))
    },
    onError: () => toast.error(t('common.error')),
  })

  // ── Record mutations ─────────────────────────────────────────────────
  const insertMut = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      insertRecord(orgId!, deptId!, selectedStore!.id, data),
    onSuccess: () => {
      invalidateRecords()
      invalidateStores() // refresh record_count
      setAddRecordOpen(false)
      setRecordJson('{}')
      toast.success(t('datastores.recordAdded'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const updateRecordMut = useMutation({
    mutationFn: ({ recordId, data }: { recordId: string; data: Record<string, unknown> }) =>
      updateRecord(orgId!, deptId!, selectedStore!.id, recordId, data),
    onSuccess: () => {
      invalidateRecords()
      setEditRecord(null)
      setRecordJson('{}')
      toast.success(t('datastores.recordUpdated'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const deleteRecordMut = useMutation({
    mutationFn: (recordId: string) => deleteRecord(orgId!, deptId!, selectedStore!.id, recordId),
    onSuccess: () => {
      invalidateRecords()
      invalidateStores()
      setDeleteRecordTarget(null)
      toast.success(t('datastores.recordDeleted'))
    },
    onError: () => toast.error(t('common.error')),
  })

  // ── Helpers ──────────────────────────────────────────────────────────
  const openCreate = () => {
    setForm(emptyStoreForm())
    setCreateOpen(true)
  }

  const openEdit = (store: DataStore) => {
    setForm(storeToForm(store))
    setEditStore(store)
  }

  const openAddRecord = () => {
    setRecordJson('{}')
    setJsonError(false)
    setAddRecordOpen(true)
  }

  const openEditRecord = (record: DataStoreRecord) => {
    setRecordJson(JSON.stringify(record.data, null, 2))
    setJsonError(false)
    setEditRecord(record)
  }

  const buildPayload = (f: StoreFormState): DataStoreCreate => {
    const parsed = safeParseJson(f.schema) ?? {}
    return {
      name: f.name,
      description: f.description || undefined,
      schema: parsed,
      visibility: f.visibility,
      max_records: parseInt(f.maxRecords, 10) || undefined,
    }
  }

  const handleSubmitStore = () => {
    if (!form.name.trim()) return
    const payload = buildPayload(form)
    if (editStore) {
      updateMut.mutate({ id: editStore.id, payload: payload as DataStoreUpdate })
    } else {
      createMut.mutate(payload)
    }
  }

  const handleSubmitRecord = () => {
    const parsed = safeParseJson(recordJson)
    if (!parsed) {
      setJsonError(true)
      return
    }
    setJsonError(false)
    if (editRecord) {
      updateRecordMut.mutate({ recordId: editRecord.id, data: parsed })
    } else {
      insertMut.mutate(parsed)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────

  return (
    <div className="flex-1 overflow-auto p-4 md:p-6">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{t('datastores.title')}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t('datastores.subtitle')}</p>
          </div>
          {hasPermission('datastores:write') && (
            <Button onClick={openCreate} size="sm">
              <Plus className="h-4 w-4 me-1" />
              {t('datastores.newStore')}
            </Button>
          )}
        </div>

        <div className="flex gap-4">
          {/* ── Store list ──────────────────────────────────────────── */}
          <div className={selectedStore ? 'w-1/2' : 'w-full'}>
            {storesLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-20 w-full" />
                ))}
              </div>
            ) : (storesData?.items ?? []).length === 0 ? (
              <Card>
                <CardContent className="p-12 flex flex-col items-center gap-3 text-muted-foreground">
                  <Database className="h-10 w-10 opacity-40" />
                  <p className="text-sm">{t('datastores.noStores')}</p>
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-2">
                {(storesData?.items ?? []).map((store) => (
                  <Card
                    key={store.id}
                    className={`cursor-pointer transition-shadow hover:shadow-sm group ${
                      selectedStore?.id === store.id ? 'ring-2 ring-primary' : ''
                    }`}
                    onClick={() => {
                      setSelectedStore(store)
                      setRecordsPage(1)
                    }}
                  >
                    <CardContent className="p-4 flex items-center gap-3">
                      <Database className="h-5 w-5 shrink-0 text-muted-foreground" />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-sm truncate">{store.name}</span>
                          <Badge variant={store.is_active ? 'success' : 'secondary'}>
                            {store.is_active ? t('datastores.active') : t('datastores.inactive')}
                          </Badge>
                          <Badge variant="outline" className="text-xs">
                            {store.visibility === 'shared' ? (
                              <><Globe className="h-3 w-3 me-1" />{t('datastores.shared')}</>
                            ) : (
                              <><Lock className="h-3 w-3 me-1" />{t('datastores.private')}</>
                            )}
                          </Badge>
                        </div>
                        {store.description && (
                          <p className="text-xs text-muted-foreground truncate mt-0.5">
                            {store.description}
                          </p>
                        )}
                        <p className="text-xs text-muted-foreground mt-0.5">
                          <Table2 className="h-3 w-3 inline me-1" />
                          {store.record_count} {t('datastores.recordCount')}
                        </p>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {hasPermission('datastores:write') && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 opacity-0 group-hover:opacity-100"
                              onClick={(e) => { e.stopPropagation(); openEdit(store) }}
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 opacity-0 group-hover:opacity-100 text-destructive hover:text-destructive"
                              onClick={(e) => { e.stopPropagation(); setDeleteTarget(store) }}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </>
                        )}
                        <ChevronRight className="h-4 w-4 text-muted-foreground" />
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}

            <Pagination
              page={page}
              totalPages={calcTotalPages(storesData?.total ?? 0, 20)}
              onPageChange={setPage}
              className="mt-4"
            />
          </div>

          {/* ── Records panel ───────────────────────────────────────── */}
          {selectedStore && (
            <div className="w-1/2">
              <Card>
                <CardHeader className="pb-3 flex flex-row items-center justify-between">
                  <CardTitle className="text-base flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6"
                      onClick={() => setSelectedStore(null)}
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </Button>
                    {selectedStore.name}
                  </CardTitle>
                  {hasPermission('datastores:write') && (
                    <Button size="sm" variant="outline" onClick={openAddRecord}>
                      <Plus className="h-3.5 w-3.5 me-1" />
                      {t('datastores.addRecord')}
                    </Button>
                  )}
                </CardHeader>
                <CardContent className="p-0">
                  {recordsLoading ? (
                    <div className="space-y-1 p-4">
                      {Array.from({ length: 3 }).map((_, i) => (
                        <Skeleton key={i} className="h-12 w-full" />
                      ))}
                    </div>
                  ) : (recordsData?.items ?? []).length === 0 ? (
                    <div className="p-8 text-center text-muted-foreground text-sm">
                      {t('datastores.noRecords')}
                    </div>
                  ) : (
                    <div className="divide-y">
                      {(recordsData?.items ?? []).map((record) => (
                        <div
                          key={record.id}
                          className="p-3 flex items-start gap-2 group hover:bg-muted/30"
                        >
                          <pre className="flex-1 text-xs font-mono bg-muted rounded p-2 overflow-x-auto max-h-24">
                            {JSON.stringify(record.data, null, 2)}
                          </pre>
                          {hasPermission('datastores:write') && (
                            <div className="flex flex-col gap-1 shrink-0 opacity-0 group-hover:opacity-100">
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6"
                                onClick={() => openEditRecord(record)}
                              >
                                <Pencil className="h-3 w-3" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6 text-destructive hover:text-destructive"
                                onClick={() => setDeleteRecordTarget(record)}
                              >
                                <Trash2 className="h-3 w-3" />
                              </Button>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="p-3 border-t">
                    <Pagination
                      page={recordsPage}
                      totalPages={calcTotalPages(recordsData?.total ?? 0, 20)}
                      onPageChange={setRecordsPage}
                    />
                  </div>
                </CardContent>
              </Card>
            </div>
          )}
        </div>
      </div>

      {/* ── Create / Edit Store Dialog ──────────────────────────────────── */}
      <Dialog
        open={createOpen || !!editStore}
        onOpenChange={(o) => {
          if (!o) { setCreateOpen(false); setEditStore(null) }
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editStore ? t('datastores.editTitle') : t('datastores.createTitle')}
            </DialogTitle>
            <DialogDescription className="sr-only">{t('datastores.subtitle')}</DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <Label htmlFor="ds-name">{t('datastores.storeName')}</Label>
              <Input
                id="ds-name"
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder={t('datastores.storeNamePlaceholder')}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="ds-desc">{t('datastores.storeDescription')}</Label>
              <Input
                id="ds-desc"
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                placeholder={t('datastores.storeDescriptionPlaceholder')}
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>{t('datastores.visibility')}</Label>
                <Select
                  value={form.visibility}
                  onValueChange={(v) =>
                    setForm((f) => ({ ...f, visibility: v as 'shared' | 'private' }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="shared">{t('datastores.shared')}</SelectItem>
                    <SelectItem value="private">{t('datastores.private')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="ds-max">{t('datastores.maxRecords')}</Label>
                <Input
                  id="ds-max"
                  type="number"
                  min={0}
                  value={form.maxRecords}
                  onChange={(e) => setForm((f) => ({ ...f, maxRecords: e.target.value }))}
                />
                <p className="text-xs text-muted-foreground">{t('datastores.maxRecordsHint')}</p>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="ds-schema">{t('datastores.storeSchema')}</Label>
              <Textarea
                id="ds-schema"
                value={form.schema}
                onChange={(e) => setForm((f) => ({ ...f, schema: e.target.value }))}
                className="font-mono text-xs h-24 resize-none"
                placeholder="{}"
              />
              <p className="text-xs text-muted-foreground">{t('datastores.storeSchemaHint')}</p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => { setCreateOpen(false); setEditStore(null) }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              disabled={!form.name.trim() || createMut.isPending || updateMut.isPending}
              onClick={handleSubmitStore}
            >
              {editStore ? t('common.save') : t('common.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete Store Dialog ───────────────────────────────────────────── */}
      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('datastores.deleteStore')}</DialogTitle>
            <DialogDescription className="sr-only">{t('datastores.subtitle')}</DialogDescription>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('datastores.deleteStoreConfirm', { name: deleteTarget?.name ?? '' })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMut.isPending}
              onClick={() => deleteTarget && deleteMut.mutate(deleteTarget.id)}
            >
              {t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Add / Edit Record Dialog ──────────────────────────────────────── */}
      <Dialog
        open={addRecordOpen || !!editRecord}
        onOpenChange={(o) => {
          if (!o) { setAddRecordOpen(false); setEditRecord(null) }
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editRecord ? t('datastores.editRecord') : t('datastores.addRecord')}
            </DialogTitle>
            <DialogDescription className="sr-only">{t('datastores.subtitle')}</DialogDescription>
          </DialogHeader>

          <div className="space-y-1.5 py-2">
            <Label htmlFor="rec-data">{t('datastores.recordData')}</Label>
            <Textarea
              id="rec-data"
              value={recordJson}
              onChange={(e) => {
                setRecordJson(e.target.value)
                setJsonError(false)
              }}
              className="font-mono text-xs h-40 resize-none"
              placeholder={t('datastores.recordDataPlaceholder')}
            />
            {jsonError && (
              <p className="text-xs text-destructive">{t('datastores.invalidJson')}</p>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => { setAddRecordOpen(false); setEditRecord(null) }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              disabled={insertMut.isPending || updateRecordMut.isPending}
              onClick={handleSubmitRecord}
            >
              {editRecord ? t('common.save') : t('common.add')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete Record Dialog ──────────────────────────────────────────── */}
      <Dialog
        open={!!deleteRecordTarget}
        onOpenChange={(o) => !o && setDeleteRecordTarget(null)}
      >
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('datastores.deleteRecord')}</DialogTitle>
            <DialogDescription className="sr-only">{t('datastores.subtitle')}</DialogDescription>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('datastores.deleteRecordConfirm')}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteRecordTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteRecordMut.isPending}
              onClick={() =>
                deleteRecordTarget && deleteRecordMut.mutate(deleteRecordTarget.id)
              }
            >
              {t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
