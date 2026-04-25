import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  MessageSquarePlus,
  Archive,
  ArchiveRestore,
  Search,
  MoreVertical,
  MessageSquare,
  Loader2,
  Pencil,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
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
import { listConversations, createConversation, updateConversation, type Conversation } from '@/api/chat'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'

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
  const [renameConv, setRenameConv] = useState<Conversation | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const [archiveConv, setArchiveConv] = useState<Conversation | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['conversations', orgId, showArchived, page],
    queryFn: () => listConversations(orgId!, page, 30, !showArchived),
    enabled: !!orgId,
    refetchInterval: 30_000,
  })

  const createMut = useMutation({
    mutationFn: () => createConversation(orgId!, newConvTitle.trim() || undefined),
    onSuccess: (conv) => {
      queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
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
      queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations', orgId] })
    },
    onError: () => toast.error(t('common.error')),
  })

  const filtered = (data?.items ?? []).filter((c) =>
    (c.title ?? '').toLowerCase().includes(search.toLowerCase())
  )

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
          <Button
            size="sm"
            className="w-full bg-white/10 hover:bg-white/20 text-white border-0 justify-start gap-2"
            onClick={() => setNewConvOpen(true)}
          >
            <MessageSquarePlus className="h-4 w-4" />
            {t('chat.newConversation')}
          </Button>
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
        <div className="p-2 space-y-0.5 overflow-hidden">
          {isLoading ? (
            Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full rounded-md bg-white/10" />
            ))
          ) : filtered.length === 0 ? (
            <p className="text-xs text-white/50 text-center py-8 px-2">
              {search
                ? t('common.noResults')
                : showArchived
                  ? t('chat.noArchivedConversations')
                  : t('chat.noConversations')}
            </p>
          ) : (
            filtered.map((conv) => (
              <ConversationItem
                key={conv.id}
                conv={conv}
                active={conv.id === conversationId}
                archived={showArchived}
                onRename={() => { setRenameConv(conv); setRenameTitle(conv.title) }}
                onArchive={() => setArchiveConv(conv)}
                onUnarchive={() => unarchiveMut.mutate(conv.id)}
                onNavigate={onClose}
                canArchive={hasPermission('sessions:delete')}
              />
            ))
          )}
        </div>
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

      {/* Rename dialog */}
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

      {/* Archive confirm */}
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
    </div>
    </>
  )
}

function ConversationItem({
  conv,
  active,
  archived,
  onRename,
  onArchive,
  onUnarchive,
  onNavigate,
  canArchive = false,
}: {
  conv: Conversation
  active: boolean
  archived: boolean
  onRename: () => void
  onArchive: () => void
  onUnarchive: () => void
  onNavigate?: () => void
  canArchive?: boolean
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  return (
    <div
      className={cn(
        'group flex items-center gap-2 px-2 py-2 rounded-md text-sm cursor-pointer transition-colors min-w-0 overflow-hidden',
        active
          ? 'bg-[hsl(var(--sidebar-active))] text-white'
          : 'text-white/75 hover:bg-[hsl(var(--sidebar-hover))] hover:text-white'
      )}
      onClick={() => { navigate(`/chat/${conv.id}`); onNavigate?.() }}
    >
      <MessageSquare className="h-4 w-4 shrink-0 opacity-60" />
      <span className="flex-1 min-w-0 truncate leading-tight">{conv.title || 'Untitled'}</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
          <button className="shrink-0 opacity-0 group-hover:opacity-100 hover:bg-white/20 rounded p-0.5 transition-opacity">
            <MoreVertical className="h-3.5 w-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-40">
          <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onRename() }}>
            <Pencil className="h-4 w-4 mr-2" />
            {t('chat.rename')}
          </DropdownMenuItem>
          {archived ? (
            canArchive ? (
              <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onUnarchive() }}>
                <ArchiveRestore className="h-4 w-4 mr-2" />
                {t('chat.unarchive')}
              </DropdownMenuItem>
            ) : null
          ) : (
            canArchive ? (
              <DropdownMenuItem onClick={(e) => { e.stopPropagation(); onArchive() }} className="text-destructive">
                <Archive className="h-4 w-4 mr-2" />
                {t('common.archive')}
              </DropdownMenuItem>
            ) : null
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
