import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useSensor,
  useSensors,
  useDraggable,
  useDroppable,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core'
import {
  MessageSquarePlus,
  Archive,
  ArchiveRestore,
  Search,
  MoreVertical,
  MessageSquare,
  Loader2,
  Pencil,
  Folder,
  FolderPlus,
  FolderOpen,
  FolderInput,
  FolderMinus,
  ChevronRight,
  ChevronDown,
  Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
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
  listConversations,
  createConversation,
  updateConversation,
  listFolders,
  createFolder,
  updateFolder,
  deleteFolder,
  type Conversation,
  type Folder as ConversationFolder,
} from '@/api/chat'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'

const UNGROUPED_DROP_ID = 'ungrouped'
const FOLDER_DROP_PREFIX = 'folder:'

export function ConversationList({
  mobileOpen = false,
  onClose,
}: {
  mobileOpen?: boolean
  onClose?: () => void
}) {
  const { t } = useTranslation()
  const { orgId, hasPermission } = useAuth()
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [page, setPage] = useState(1)
  const [newConvOpen, setNewConvOpen] = useState(false)
  const [newConvTitle, setNewConvTitle] = useState('')
  const [newConvFolderId, setNewConvFolderId] = useState<string | null>(null)
  const [renameConv, setRenameConv] = useState<Conversation | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const [archiveConv, setArchiveConv] = useState<Conversation | null>(null)
  // Folder state
  const [newFolderOpen, setNewFolderOpen] = useState(false)
  const [newFolderName, setNewFolderName] = useState('')
  const [renameFolder, setRenameFolder] = useState<ConversationFolder | null>(null)
  const [renameFolderName, setRenameFolderName] = useState('')
  const [archiveFolder, setArchiveFolder] = useState<ConversationFolder | null>(null)
  const [deleteFolderTarget, setDeleteFolderTarget] = useState<ConversationFolder | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [activeDragId, setActiveDragId] = useState<string | null>(null)

  const canArchive = hasPermission('sessions:delete')

  // Currently selected folder — the destination for new conversations created
  // via the global "New conversation" button. Persisted per-org in localStorage.
  const storageKey = orgId ? `gsage:chat:selectedFolder:${orgId}` : null
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null)

  useEffect(() => {
    if (!storageKey) return
    try {
      setSelectedFolderId(localStorage.getItem(storageKey))
    } catch {
      /* localStorage unavailable */
    }
  }, [storageKey])

  const selectFolder = (id: string | null) => {
    setSelectedFolderId(id)
    if (!storageKey) return
    try {
      if (id) localStorage.setItem(storageKey, id)
      else localStorage.removeItem(storageKey)
    } catch {
      /* localStorage unavailable */
    }
  }

  const { data, isLoading } = useQuery({
    queryKey: ['conversations', orgId, showArchived, page],
    queryFn: () => listConversations(orgId!, page, 30, !showArchived),
    enabled: !!orgId,
    refetchInterval: 30_000,
  })

  const { data: folders = [] } = useQuery({
    queryKey: ['folders', orgId, showArchived],
    queryFn: () => listFolders(orgId!, !showArchived),
    enabled: !!orgId,
  })

  // Drop the selection if the selected folder is no longer visible
  // (e.g. it was archived or deleted).
  useEffect(() => {
    if (selectedFolderId && !folders.some((f) => f.id === selectedFolderId)) {
      selectFolder(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folders, selectedFolderId])

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
    queryClient.invalidateQueries({ queryKey: ['folders', orgId] })
  }

  const createMut = useMutation({
    mutationFn: () =>
      createConversation(orgId!, newConvTitle.trim() || undefined, 'assistant', newConvFolderId),
    onSuccess: (conv) => {
      invalidateAll()
      // Make sure the destination folder is expanded so the new item is visible.
      if (newConvFolderId) {
        setCollapsed((prev) => {
          const next = new Set(prev)
          next.delete(newConvFolderId)
          return next
        })
      }
      setNewConvOpen(false)
      setNewConvTitle('')
      navigate(`/chat/${conv.id}`)
      onClose?.()
    },
    onError: () => toast.error(t('chat.createConvError')),
  })

  const renameMut = useMutation({
    mutationFn: () => updateConversation(orgId!, renameConv!.id, { title: renameTitle }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
      setRenameConv(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const archiveMut = useMutation({
    mutationFn: (convId: string) => updateConversation(orgId!, convId, { is_active: false }),
    onSuccess: (_result, archivedId) => {
      setArchiveConv(null)
      invalidateAll()
      // Navigate away if the archived conversation is the current one,
      // or if the active list will become empty after this archive.
      const remaining = (data?.items ?? []).filter((c) => c.id !== archivedId)
      if (archivedId === conversationId || remaining.length === 0) {
        navigate('/chat')
      }
    },
    onError: () => toast.error(t('common.error')),
  })

  const unarchiveMut = useMutation({
    mutationFn: (convId: string) => updateConversation(orgId!, convId, { is_active: true }),
    onSuccess: () => invalidateAll(),
    onError: () => toast.error(t('common.error')),
  })

  const moveMut = useMutation({
    mutationFn: ({ convId, folderId }: { convId: string; folderId: string | null }) =>
      folderId === null
        ? updateConversation(orgId!, convId, { clear_folder: true })
        : updateConversation(orgId!, convId, { folder_id: folderId }),
    onSuccess: () => invalidateAll(),
    onError: () => toast.error(t('common.error')),
  })

  const createFolderMut = useMutation({
    mutationFn: () => createFolder(orgId!, newFolderName.trim()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', orgId] })
      setNewFolderOpen(false)
      setNewFolderName('')
    },
    onError: () => toast.error(t('common.error')),
  })

  const renameFolderMut = useMutation({
    mutationFn: () => updateFolder(orgId!, renameFolder!.id, { name: renameFolderName.trim() }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', orgId] })
      setRenameFolder(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const archiveFolderMut = useMutation({
    mutationFn: ({ folderId, active }: { folderId: string; active: boolean }) =>
      updateFolder(orgId!, folderId, { is_active: active }),
    onSuccess: () => {
      setArchiveFolder(null)
      invalidateAll()
    },
    onError: () => toast.error(t('common.error')),
  })

  const deleteFolderMut = useMutation({
    mutationFn: (folderId: string) => deleteFolder(orgId!, folderId),
    onSuccess: () => {
      setDeleteFolderTarget(null)
      invalidateAll()
    },
    onError: () => toast.error(t('common.error')),
  })

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  )

  const filtered = useMemo(
    () =>
      (data?.items ?? []).filter((c) =>
        (c.title ?? '').toLowerCase().includes(search.toLowerCase())
      ),
    [data?.items, search]
  )

  // Group the loaded conversations by folder. Folders come from their own
  // query so they appear even when none of their conversations are on the
  // current page.
  const { byFolder, ungrouped } = useMemo(() => {
    const folderIds = new Set(folders.map((f) => f.id))
    const map = new Map<string, Conversation[]>()
    const loose: Conversation[] = []
    for (const c of filtered) {
      if (c.folder_id && folderIds.has(c.folder_id)) {
        const list = map.get(c.folder_id) ?? []
        list.push(c)
        map.set(c.folder_id, list)
      } else {
        loose.push(c)
      }
    }
    return { byFolder: map, ungrouped: loose }
  }, [filtered, folders])

  const toggleCollapse = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const handleDragStart = (e: DragStartEvent) => setActiveDragId(String(e.active.id))

  const handleDragEnd = (e: DragEndEvent) => {
    setActiveDragId(null)
    const { active, over } = e
    if (!over) return
    const convId = String(active.id)
    const conv = filtered.find((c) => c.id === convId)
    if (!conv) return
    const overId = String(over.id)
    if (overId === UNGROUPED_DROP_ID) {
      if (conv.folder_id) moveMut.mutate({ convId, folderId: null })
    } else if (overId.startsWith(FOLDER_DROP_PREFIX)) {
      const folderId = overId.slice(FOLDER_DROP_PREFIX.length)
      if (conv.folder_id !== folderId) moveMut.mutate({ convId, folderId })
    }
  }

  const activeConv = activeDragId ? filtered.find((c) => c.id === activeDragId) : null

  const renderItem = (conv: Conversation) => (
    <ConversationItem
      key={conv.id}
      conv={conv}
      active={conv.id === conversationId}
      archived={showArchived}
      folders={folders}
      onRename={() => { setRenameConv(conv); setRenameTitle(conv.title) }}
      onArchive={() => setArchiveConv(conv)}
      onUnarchive={() => unarchiveMut.mutate(conv.id)}
      onMove={(folderId) => moveMut.mutate({ convId: conv.id, folderId })}
      onNavigate={onClose}
      canArchive={canArchive}
    />
  )

  const hasAnything = filtered.length > 0 || folders.length > 0

  return (
    <>
      {/* Backdrop — mobile only */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <div
        className={cn(
          'flex flex-col h-full w-64 shrink-0 border-r bg-[hsl(var(--sidebar-bg))] text-[hsl(var(--sidebar-fg))]',
          // Mobile: fixed drawer that slides in/out
          'fixed inset-y-0 start-0 z-50 transition-transform duration-200 ease-in-out',
          mobileOpen ? 'translate-x-0' : 'ltr:-translate-x-full rtl:translate-x-full',
          // Desktop: static sidebar, always visible, reset mobile positioning
          // NOTE: md:ltr:/md:rtl: needed to match specificity of ltr:/rtl: variants above
          'md:relative md:ltr:translate-x-0 md:rtl:translate-x-0 md:transition-none md:z-auto',
        )}
      >
      {/* Header */}
      <div className="p-3 border-b border-[hsl(var(--sidebar-border))]">
        {hasPermission('agents:run') && (
          <div className="flex gap-2">
            <Button
              size="sm"
              className="flex-1 bg-white/10 hover:bg-white/20 text-white border-0 justify-start gap-2"
              onClick={() => { setNewConvFolderId(selectedFolderId); setNewConvOpen(true) }}
            >
              <MessageSquarePlus className="h-4 w-4" />
              {t('chat.newConversation')}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="bg-white/10 hover:bg-white/20 text-white border-0 px-2"
              title={t('chat.newFolder')}
              aria-label={t('chat.newFolder')}
              onClick={() => setNewFolderOpen(true)}
            >
              <FolderPlus className="h-4 w-4" />
            </Button>
          </div>
        )}
        <div className="relative mt-2">
          <Search className="absolute start-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-white/50" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('common.search')}
            className="ps-8 h-8 text-xs bg-white/10 border-white/20 text-white placeholder:text-white/50 focus-visible:ring-white/30"
          />
        </div>
      </div>

      {/* List */}
      <ScrollArea className="flex-1 [&>div>div]:!block">
        <DndContext
          sensors={sensors}
          collisionDetection={pointerWithin}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
        >
          <div className="p-2 space-y-0.5 overflow-hidden">
            {isLoading ? (
              Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full rounded-md bg-white/10" />
              ))
            ) : !hasAnything ? (
              <p className="text-xs text-white/50 text-center py-8 px-2">
                {search
                  ? t('common.noResults')
                  : showArchived
                    ? t('chat.noArchivedConversations')
                    : t('chat.noConversations')}
              </p>
            ) : (
              <>
                {folders.map((folder) => (
                  <FolderGroup
                    key={folder.id}
                    folder={folder}
                    collapsed={collapsed.has(folder.id)}
                    archived={showArchived}
                    canArchive={canArchive}
                    selected={folder.id === selectedFolderId}
                    canCreate={hasPermission('agents:run')}
                    onSelect={() => {
                      const isSel = selectedFolderId === folder.id
                      selectFolder(isSel ? null : folder.id)
                      if (!isSel) {
                        setCollapsed((prev) => {
                          const next = new Set(prev)
                          next.delete(folder.id)
                          return next
                        })
                      }
                    }}
                    onNewConversation={() => {
                      selectFolder(folder.id)
                      setNewConvFolderId(folder.id)
                      setNewConvOpen(true)
                    }}
                    onToggle={() => toggleCollapse(folder.id)}
                    onRename={() => { setRenameFolder(folder); setRenameFolderName(folder.name) }}
                    onArchive={() => setArchiveFolder(folder)}
                    onUnarchive={() => archiveFolderMut.mutate({ folderId: folder.id, active: true })}
                    onDelete={() => setDeleteFolderTarget(folder)}
                  >
                    {(byFolder.get(folder.id) ?? []).map(renderItem)}
                  </FolderGroup>
                ))}
                <UngroupedZone hasFolders={folders.length > 0}>
                  {ungrouped.map(renderItem)}
                </UngroupedZone>
              </>
            )}
          </div>
          <DragOverlay>
            {activeConv ? (
              <div className="flex items-center gap-2 px-2 py-2 rounded-md text-sm bg-[hsl(var(--sidebar-active))] text-white shadow-lg max-w-[15rem]">
                <MessageSquare className="h-4 w-4 shrink-0 opacity-60" />
                <span className="flex-1 min-w-0 truncate">{activeConv.title || t('chat.untitled')}</span>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      </ScrollArea>

      {/* Footer — toggle archived + pagination */}
      <div className="border-t border-[hsl(var(--sidebar-border))]">        
        {data && data.total > 30 && (
          <Pagination
            page={page}
            totalPages={calcTotalPages(data.total, 30)}
            onPageChange={setPage}
            className="py-1.5 px-2"
          />
        )}
        <div className="p-2">
        <Button
          variant="ghost"
          size="sm"
          className={cn(
            'w-full justify-start gap-2 text-xs',
            showArchived ? 'text-white' : 'text-white/60 hover:text-white'
          )}
          onClick={() => { setShowArchived(!showArchived); setPage(1) }}
        >
          <Archive className="h-3.5 w-3.5" />
          {showArchived ? t('chat.showActive') : t('chat.showArchived')}
        </Button>
        </div>
      </div>

      {/* New conversation dialog */}
      <Dialog open={newConvOpen} onOpenChange={setNewConvOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('chat.newConversation')}</DialogTitle>
            <DialogDescription className="sr-only">{t('chat.newConvDesc')}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            placeholder={t('chat.conversationTitlePlaceholder')}
            value={newConvTitle}
            onChange={(e) => setNewConvTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && createMut.mutate()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setNewConvOpen(false)}>{t('common.cancel')}</Button>
            <Button onClick={() => createMut.mutate()} disabled={createMut.isPending}>
              {createMut.isPending && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
              {t('common.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rename conversation dialog */}
      <Dialog open={!!renameConv} onOpenChange={(o) => !o && setRenameConv(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('chat.renameConversation')}</DialogTitle>
            <DialogDescription className="sr-only">{t('chat.renameConvDesc')}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={renameTitle}
            onChange={(e) => setRenameTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && renameMut.mutate()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameConv(null)}>{t('common.cancel')}</Button>
            <Button onClick={() => renameMut.mutate()} disabled={renameMut.isPending}>
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Archive conversation confirm */}
      <AlertDialog open={!!archiveConv} onOpenChange={(o) => !o && setArchiveConv(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('chat.archiveTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('chat.archiveDesc')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => archiveMut.mutate(archiveConv!.id)}>
              {t('common.archive')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* New folder dialog */}
      <Dialog open={newFolderOpen} onOpenChange={setNewFolderOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('chat.newFolder')}</DialogTitle>
            <DialogDescription className="sr-only">{t('chat.newFolderDesc')}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            placeholder={t('chat.folderNamePlaceholder')}
            value={newFolderName}
            onChange={(e) => setNewFolderName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && newFolderName.trim() && createFolderMut.mutate()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setNewFolderOpen(false)}>{t('common.cancel')}</Button>
            <Button onClick={() => createFolderMut.mutate()} disabled={createFolderMut.isPending || !newFolderName.trim()}>
              {createFolderMut.isPending && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
              {t('common.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rename folder dialog */}
      <Dialog open={!!renameFolder} onOpenChange={(o) => !o && setRenameFolder(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('chat.renameFolder')}</DialogTitle>
            <DialogDescription className="sr-only">{t('chat.renameFolderDesc')}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            value={renameFolderName}
            onChange={(e) => setRenameFolderName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && renameFolderName.trim() && renameFolderMut.mutate()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameFolder(null)}>{t('common.cancel')}</Button>
            <Button onClick={() => renameFolderMut.mutate()} disabled={renameFolderMut.isPending || !renameFolderName.trim()}>
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Archive folder confirm (cascades to conversations) */}
      <AlertDialog open={!!archiveFolder} onOpenChange={(o) => !o && setArchiveFolder(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('chat.archiveFolderTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('chat.archiveFolderDesc')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => archiveFolderMut.mutate({ folderId: archiveFolder!.id, active: false })}>
              {t('common.archive')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete folder confirm */}
      <AlertDialog open={!!deleteFolderTarget} onOpenChange={(o) => !o && setDeleteFolderTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('chat.deleteFolderTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('chat.deleteFolderDesc')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => deleteFolderMut.mutate(deleteFolderTarget!.id)}
            >
              {t('common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
    </>
  )
}

function FolderGroup({
  folder,
  collapsed,
  archived,
  canArchive,
  selected,
  canCreate,
  onSelect,
  onNewConversation,
  onToggle,
  onRename,
  onArchive,
  onUnarchive,
  onDelete,
  children,
}: {
  folder: ConversationFolder
  collapsed: boolean
  archived: boolean
  canArchive: boolean
  selected: boolean
  canCreate: boolean
  onSelect: () => void
  onNewConversation: () => void
  onToggle: () => void
  onRename: () => void
  onArchive: () => void
  onUnarchive: () => void
  onDelete: () => void
  children: React.ReactNode
}) {
  const { t } = useTranslation()
  const { setNodeRef, isOver } = useDroppable({ id: `${FOLDER_DROP_PREFIX}${folder.id}` })

  return (
    <div
      ref={setNodeRef}
      className={cn(
        'rounded-md transition-colors',
        selected && 'ring-1 ring-white/30 bg-white/5',
        isOver && 'bg-white/10 ring-1 ring-white/30'
      )}
    >
      <div
        className={cn(
          'group flex items-center gap-1.5 px-2 py-1.5 rounded-md text-sm cursor-pointer hover:bg-[hsl(var(--sidebar-hover))] hover:text-white',
          selected ? 'text-white' : 'text-white/80'
        )}
        onClick={onSelect}
      >
        <button
          type="button"
          className="shrink-0 rounded p-0.5 hover:bg-white/20"
          aria-label={collapsed ? t('common.expand') : t('common.collapse')}
          onClick={(e) => { e.stopPropagation(); onToggle() }}
        >
          {collapsed ? (
            <ChevronRight className="h-3.5 w-3.5 opacity-70" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 opacity-70" />
          )}
        </button>
        {collapsed ? (
          <Folder className="h-4 w-4 shrink-0 opacity-70" />
        ) : (
          <FolderOpen className="h-4 w-4 shrink-0 opacity-70" />
        )}
        <span className="flex-1 min-w-0 truncate font-medium leading-tight">{folder.name}</span>
        <span className="shrink-0 text-[10px] text-white/40 tabular-nums">{folder.conversation_count}</span>
        {canCreate && !archived && (
          <button
            type="button"
            className="shrink-0 opacity-0 group-hover:opacity-100 hover:bg-white/20 rounded p-0.5 transition-opacity"
            title={t('chat.newConversationInFolder')}
            aria-label={t('chat.newConversationInFolder')}
            onClick={(e) => { e.stopPropagation(); onNewConversation() }}
          >
            <MessageSquarePlus className="h-3.5 w-3.5" />
          </button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
            <button className="shrink-0 opacity-0 group-hover:opacity-100 hover:bg-white/20 rounded p-0.5 transition-opacity">
              <MoreVertical className="h-3.5 w-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onRename() }}>
              <Pencil className="h-4 w-4 mr-2" />
              {t('chat.rename')}
            </DropdownMenuItem>
            {canArchive && (
              archived ? (
                <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onUnarchive() }}>
                  <ArchiveRestore className="h-4 w-4 mr-2" />
                  {t('chat.unarchive')}
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onArchive() }}>
                  <Archive className="h-4 w-4 mr-2" />
                  {t('common.archive')}
                </DropdownMenuItem>
              )
            )}
            {canArchive && (
              <DropdownMenuItem
                onClick={(e) => { e.stopPropagation(); onDelete() }}
                className="text-destructive"
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {t('common.delete')}
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {!collapsed && (
        <div className="ms-3 ps-1 border-s border-white/10 space-y-0.5">
          {children}
        </div>
      )}
    </div>
  )
}

function UngroupedZone({
  hasFolders,
  children,
}: {
  hasFolders: boolean
  children: React.ReactNode
}) {
  const { setNodeRef, isOver } = useDroppable({ id: UNGROUPED_DROP_ID })
  return (
    <div
      ref={setNodeRef}
      className={cn(
        'rounded-md transition-colors space-y-0.5',
        hasFolders && 'mt-1 pt-1',
        isOver && 'bg-white/10 ring-1 ring-white/30'
      )}
    >
      {children}
    </div>
  )
}

function ConversationItem({
  conv,
  active,
  archived,
  folders,
  onRename,
  onArchive,
  onUnarchive,
  onMove,
  onNavigate,
  canArchive = false,
}: {
  conv: Conversation
  active: boolean
  archived: boolean
  folders: ConversationFolder[]
  onRename: () => void
  onArchive: () => void
  onUnarchive: () => void
  onMove: (folderId: string | null) => void
  onNavigate?: () => void
  canArchive?: boolean
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { setNodeRef, attributes, listeners, isDragging } = useDraggable({ id: conv.id })

  return (
    <div
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      className={cn(
        'group flex items-center gap-2 px-2 py-2 rounded-md text-sm cursor-pointer transition-colors min-w-0 overflow-hidden',
        isDragging && 'opacity-40',
        active
          ? 'bg-[hsl(var(--sidebar-active))] text-white'
          : 'text-white/75 hover:bg-[hsl(var(--sidebar-hover))] hover:text-white'
      )}
      onClick={() => { navigate(`/chat/${conv.id}`); onNavigate?.() }}
    >
      <MessageSquare className="h-4 w-4 shrink-0 opacity-60" />
      <span className="flex-1 min-w-0 truncate leading-tight">{conv.title || t('chat.untitled')}</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
          <button className="shrink-0 opacity-0 group-hover:opacity-100 hover:bg-white/20 rounded p-0.5 transition-opacity">
            <MoreVertical className="h-3.5 w-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-44">
          <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onRename() }}>
            <Pencil className="h-4 w-4 mr-2" />
            {t('chat.rename')}
          </DropdownMenuItem>

          {!archived && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <FolderInput className="h-4 w-4 mr-2" />
                {t('chat.moveToFolder')}
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="max-h-64 overflow-y-auto">
                {folders.length === 0 ? (
                  <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
                    {t('chat.noFolders')}
                  </DropdownMenuLabel>
                ) : (
                  folders.map((f) => (
                    <DropdownMenuItem
                      key={f.id}
                      disabled={f.id === conv.folder_id}
                      onClick={(e) => { e.stopPropagation(); onMove(f.id) }}
                    >
                      <Folder className="h-4 w-4 mr-2" />
                      <span className="truncate">{f.name}</span>
                    </DropdownMenuItem>
                  ))
                )}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          )}

          {!archived && conv.folder_id && (
            <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onMove(null) }}>
              <FolderMinus className="h-4 w-4 mr-2" />
              {t('chat.removeFromFolder')}
            </DropdownMenuItem>
          )}

          {canArchive && <DropdownMenuSeparator />}
          {canArchive && (
            archived ? (
              <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onUnarchive() }}>
                <ArchiveRestore className="h-4 w-4 mr-2" />
                {t('chat.unarchive')}
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onArchive() }} className="text-destructive">
                <Archive className="h-4 w-4 mr-2" />
                {t('common.archive')}
              </DropdownMenuItem>
            )
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
