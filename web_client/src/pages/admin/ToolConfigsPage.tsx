import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Wrench, ChevronDown, ChevronRight } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/contexts/AuthContext'
import {
  listToolConfigs,
  listAvailableTools,
  listDepartments,
  createToolConfig,
  updateToolConfig,
  deleteToolConfig,
  getToolCatalog,
  updateToolSettings,
  type ToolConfigOut,
  type ToolConfigCreate,
  type ToolConfigUpdate,
  type AvailableTool,
  type DepartmentOut,
  type ToolCatalogEntry,
} from '@/api/admin'

export default function ToolConfigsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [expandedNs, setExpandedNs] = useState<Set<string>>(new Set())
  const [createOpen, setCreateOpen] = useState(false)
  const [createFor, setCreateFor] = useState<string>('')
  const [editTarget, setEditTarget] = useState<ToolConfigOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ToolConfigOut | null>(null)
  const [disableTarget, setDisableTarget] = useState<ToolCatalogEntry | null>(null)

  const { data: catalog, isLoading } = useQuery({
    queryKey: ['admin', 'tool-catalog', orgId],
    queryFn: () => getToolCatalog(orgId!),
    enabled: !!orgId,
  })

  const { data: availableTools = [] } = useQuery({
    queryKey: ['admin', 'available-tools', orgId],
    queryFn: () => listAvailableTools(orgId!),
    enabled: !!orgId,
  })

  const { data: departments = [] } = useQuery({
    queryKey: ['admin', 'departments', orgId],
    queryFn: () => listDepartments(orgId!),
    enabled: !!orgId,
  })

  const reload = () => queryClient.invalidateQueries({ queryKey: ['admin', 'tool-catalog', orgId] })

  const muCreate = useMutation({
    mutationFn: (p: ToolConfigCreate) => createToolConfig(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.created'))
      reload()
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ToolConfigUpdate }) =>
      updateToolConfig(orgId!, id, payload),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.updated'))
      reload()
      setEditTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteToolConfig(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.deleted'))
      reload()
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muToggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      updateToolSettings(orgId!, name, { is_enabled: enabled }),
    onSuccess: () => {
      reload()
      setDisableTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const toggleNs = (name: string) => {
    setExpandedNs((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  // Split catalog into namespaces and standalone tools
  const nsEntries = (catalog ?? []).filter((e) => e.is_namespace)
  const toolEntries = (catalog ?? []).filter((e) => !e.is_namespace)

  // Group tools under their namespace
  const nsWithChildren = nsEntries.map((ns) => ({
    ...ns,
    children: toolEntries.filter((t) => t.config_namespace === ns.name),
  }))
  const nsNames = new Set(nsEntries.map((n) => n.name))
  const orphanTools = toolEntries.filter((t) => !nsNames.has(t.config_namespace ?? ''))

  // Build combined tool+namespace options for the config form dropdown
  const namespaceOptions: AvailableTool[] = nsEntries.map((ns) => ({
    name: ns.name,
    display_name: `Namespace: ${ns.name}`,
    category: 'namespace',
  }))
  const allToolOptions = [...availableTools, ...namespaceOptions]

  // Helpers
  const isDisabled = (e: ToolCatalogEntry) => !e.is_enabled
  const rowClass = (e: ToolCatalogEntry) =>
    `hover:bg-muted/30 transition-colors ${isDisabled(e) ? 'opacity-50 bg-muted/30' : ''}`

  const handleToggle = (entry: ToolCatalogEntry) => {
    if (entry.is_enabled) {
      setDisableTarget(entry)
    } else {
      muToggle.mutate({ name: entry.name, enabled: true })
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Wrench className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.toolConfigs.catalogTitle', 'Tool Catalog')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.toolConfigs.subtitle')}</p>
          </div>
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.toolConfigs.toolName')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.toolConfigs.configsColumn', 'Configs')}</th>
                <th className="px-4 py-3 w-20" />
                <th className="px-4 py-3 w-24 font-medium text-center">{t('admin.toolConfigs.enabled')}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {/* Namespace rows with children */}
              {nsWithChildren.map((ns) => (
                <>
                  <tr key={ns.name} className={rowClass(ns)}>
                    <td className="px-4 py-3">
                      <button
                        className="flex items-center gap-1 font-semibold hover:underline cursor-pointer"
                        onClick={() => toggleNs(ns.name)}
                      >
                        {expandedNs.has(ns.name) ? (
                          <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5" />
                        )}
                        Namespace: {ns.name}
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <ConfigCountBadge entry={ns} onExpand={() => toggleNs(ns.name)} />
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => { setCreateFor(ns.name); setCreateOpen(true) }}
                      >
                        <Plus className="h-3 w-3" /> {t('admin.toolConfigs.addConfigInline', 'Add')}
                      </Button>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <Switch checked={ns.is_enabled} onCheckedChange={() => handleToggle(ns)} />
                    </td>
                  </tr>
                  {/* Child tools (visible when expanded) */}
                  {expandedNs.has(ns.name) && ns.children.map((child) => (
                    <tr key={child.name} className={rowClass(child)}>
                      <td className="px-4 py-3 pl-10 text-sm">{child.display_name || child.name}</td>
                      <td className="px-4 py-3">
                        <ConfigCountBadge
                          entry={child}
                          onEdit={(tc) => setEditTarget(tc)}
                          onDelete={(tc) => setDeleteTarget(tc)}
                        />
                      </td>
                      <td className="px-4 py-3">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => { setCreateFor(child.name); setCreateOpen(true) }}
                        >
                          <Plus className="h-3 w-3" /> {t('admin.toolConfigs.addConfigInline', 'Add')}
                        </Button>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <Switch checked={child.is_enabled} onCheckedChange={() => handleToggle(child)} />
                      </td>
                    </tr>
                  ))}
                </>
              ))}

              {/* Divider before orphan tools */}
              {nsWithChildren.length > 0 && orphanTools.length > 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-2 bg-muted/20 text-xs text-muted-foreground font-medium">
                    {t('admin.toolConfigs.noNamespace', 'Tools without namespace')}
                  </td>
                </tr>
              )}

              {/* Orphan / standalone tools */}
              {orphanTools.map((tool) => (
                <tr key={tool.name} className={rowClass(tool)}>
                  <td className="px-4 py-3 text-sm">{tool.display_name || tool.name}</td>
                  <td className="px-4 py-3">
                    <ConfigCountBadge
                      entry={tool}
                      onEdit={(tc) => setEditTarget(tc)}
                      onDelete={(tc) => setDeleteTarget(tc)}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => { setCreateFor(tool.name); setCreateOpen(true) }}
                    >
                      <Plus className="h-3 w-3" /> {t('admin.toolConfigs.addConfigInline', 'Add')}
                    </Button>
                  </td>
                  <td className="px-4 py-3 text-center">
                    <Switch checked={tool.is_enabled} onCheckedChange={() => handleToggle(tool)} />
                  </td>
                </tr>
              ))}

              {catalog?.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">{t('common.noResults')}</td>
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
            <DialogTitle>
              {t('admin.toolConfigs.createTitle')}
              {createFor && ` — ${createFor}`}
            </DialogTitle>
          </DialogHeader>
          <ToolConfigForm
            initialToolName={createFor}
            onSubmit={(p) => muCreate.mutate(p)}
            onCancel={() => { setCreateOpen(false); setCreateFor('') }}
            availableTools={allToolOptions}
            departments={departments}
            isLoading={muCreate.isPending}
          />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editTarget} onOpenChange={(o) => !o && setEditTarget(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('admin.toolConfigs.editTitle')}</DialogTitle>
          </DialogHeader>
          {editTarget && (
            <ToolConfigForm
              initial={editTarget}
              onSubmit={(p) => muUpdate.mutate({ id: editTarget.id, payload: { tool_name: p.tool_name, profile_id: p.profile_id, dept_id: p.dept_id, description: p.description, config: p.config } })}
              onCancel={() => setEditTarget(null)}
              availableTools={allToolOptions}
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
            <DialogTitle>{t('admin.toolConfigs.deleteTitle')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.toolConfigs.deleteDesc', { name: deleteTarget?.tool_name })}
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

      {/* Disable confirmation */}
      <Dialog open={!!disableTarget} onOpenChange={(o) => !o && setDisableTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t('admin.toolConfigs.disableTitle', { name: disableTarget?.display_name || disableTarget?.name })}
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('admin.toolConfigs.disableDesc')}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDisableTarget(null)}>{t('common.cancel')}</Button>
            <Button
              variant="destructive"
              disabled={muToggle.isPending}
              onClick={() => disableTarget && muToggle.mutate({ name: disableTarget.name, enabled: false })}
            >
              {t('admin.toolConfigs.disabled')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ── Config-count badge with optional expand for edit/delete ────────────

function ConfigCountBadge({
  entry,
  onExpand,
  onEdit,
  onDelete,
}: {
  entry: ToolCatalogEntry
  onExpand?: () => void
  onEdit?: (tc: ToolConfigOut) => void
  onDelete?: (tc: ToolConfigOut) => void
}) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)

  if (entry.config_count === 0) {
    return (
      <span className="text-muted-foreground text-xs">
        {t('admin.toolConfigs.notConfigured', 'not configured')}
      </span>
    )
  }

  const toggle = () => {
    if (onEdit && onDelete) {
      setExpanded(!expanded)
    } else if (onExpand) {
      onExpand()
    }
  }

  const profileNames = entry.configs?.map((c) => c.profile_id).join(', ') ?? ''

  return (
    <div className="inline-flex flex-col gap-1">
      <Badge
        variant="outline"
        className="cursor-pointer select-none"
        onClick={toggle}
      >
        {entry.config_count === 1
          ? t('admin.toolConfigs.configCount', { count: entry.config_count })
          : t('admin.toolConfigs.configCount_plural', { count: entry.config_count })}
        {profileNames && <span className="ml-1 text-muted-foreground">({profileNames})</span>}
      </Badge>
      {expanded && onEdit && onDelete && (
        <div className="flex flex-col gap-0.5 pl-1">
          {entry.configs.map((tc) => (
            <div key={tc.id} className="flex items-center gap-1 text-xs">
              <Badge variant="secondary" className="text-xs">{tc.profile_id}</Badge>
              <Button variant="ghost" size="icon" className="h-5 w-5" onClick={() => onEdit({ ...tc, org_id: '', tool_name: entry.name, config: {}, updated_by_user_id: null, created_at: '', updated_at: '' } as unknown as ToolConfigOut)}>
                <Pencil className="h-3 w-3" />
              </Button>
              <Button variant="ghost" size="icon" className="h-5 w-5 text-destructive" onClick={() => onDelete({ ...tc, org_id: '', tool_name: entry.name, config: {}, updated_by_user_id: null, created_at: '', updated_at: '' } as unknown as ToolConfigOut)}>
                <Trash2 className="h-3 w-3" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Reusable create/edit form (unchanged logic, accepts initialToolName) ──

function ToolConfigForm({ onSubmit, onCancel, initial, initialToolName, availableTools, departments, isLoading }: {
  onSubmit: (p: ToolConfigCreate) => void
  onCancel: () => void
  initial?: ToolConfigOut
  initialToolName?: string
  availableTools: AvailableTool[]
  departments: DepartmentOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [toolName, setToolName] = useState(initial?.tool_name ?? initialToolName ?? '')
  const [profileId, setProfileId] = useState(initial?.profile_id ?? 'default')
  const [deptId, setDeptId] = useState<string>(initial?.dept_id ?? '__org__')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [configJson, setConfigJson] = useState(
    initial ? JSON.stringify(initial.config, null, 2) : '{}'
  )
  const [jsonError, setJsonError] = useState<string | null>(null)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    let config: Record<string, unknown>
    try {
      config = JSON.parse(configJson)
      setJsonError(null)
    } catch {
      setJsonError(t('datastores.invalidJson'))
      return
    }
    onSubmit({ tool_name: toolName, profile_id: profileId, dept_id: deptId === '__org__' ? null : deptId, description: description || null, config })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-1.5">
        <Label>{t('admin.toolConfigs.toolName')}</Label>
        <Select value={toolName} onValueChange={setToolName}>
          <SelectTrigger><SelectValue placeholder={t('admin.toolConfigs.toolName')} /></SelectTrigger>
          <SelectContent>
            {availableTools.map((tool) => (
              <SelectItem key={tool.name} value={tool.name}>
                {tool.display_name || tool.name}
                {tool.category && <span className="ml-1 text-muted-foreground text-xs">({tool.category})</span>}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.toolConfigs.profileId')}</Label>
        <Input value={profileId} onChange={(e) => setProfileId(e.target.value)} />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.toolConfigs.department')}</Label>
        <Select value={deptId} onValueChange={setDeptId}>
          <SelectTrigger><SelectValue placeholder={t('admin.toolConfigs.allDepartments')} /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__org__">{t('admin.toolConfigs.allDepartments')}</SelectItem>
            {departments.map((d) => (
              <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.toolConfigs.description')}</Label>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="space-y-1.5">
        <Label>{t('admin.toolConfigs.config')}</Label>
        <Textarea
          value={configJson}
          onChange={(e) => setConfigJson(e.target.value)}
          rows={8}
          className="font-mono text-xs"
        />
        {jsonError && <p className="text-xs text-destructive">{jsonError}</p>}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={isLoading}>
          {isLoading ? t('common.loading') : initial ? t('common.save') : t('common.create')}
        </Button>
      </DialogFooter>
    </form>
  )
}
