import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { useAuth } from '@/contexts/AuthContext'
import {
  listToolConfigs,
  listAvailableTools,
  listDepartments,
  createToolConfig,
  updateToolConfig,
  deleteToolConfig,
  type ToolConfigOut,
  type ToolConfigCreate,
  type ToolConfigUpdate,
  type AvailableTool,
  type DepartmentOut,
} from '@/api/admin'

export default function ToolConfigsPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<ToolConfigOut | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ToolConfigOut | null>(null)

  const { data: configs, isLoading } = useQuery({
    queryKey: ['admin', 'tool-configs', orgId],
    queryFn: () => listToolConfigs(orgId!),
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

  const muCreate = useMutation({
    mutationFn: (p: ToolConfigCreate) => createToolConfig(orgId!, p),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.created'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'tool-configs', orgId] })
      setCreateOpen(false)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muUpdate = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ToolConfigUpdate }) =>
      updateToolConfig(orgId!, id, payload),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.updated'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'tool-configs', orgId] })
      setEditTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  const muDelete = useMutation({
    mutationFn: (id: string) => deleteToolConfig(orgId!, id),
    onSuccess: () => {
      toast.success(t('admin.toolConfigs.deleted'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'tool-configs', orgId] })
      setDeleteTarget(null)
    },
    onError: () => toast.error(t('common.error')),
  })

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Wrench className="h-6 w-6 text-muted-foreground" />
          <div>
            <h1 className="text-xl font-semibold">{t('admin.toolConfigs.title')}</h1>
            <p className="text-sm text-muted-foreground">{t('admin.toolConfigs.subtitle')}</p>
          </div>
        </div>
        <Button size="sm" onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
          <Plus className="h-4 w-4" />
          {t('admin.toolConfigs.add')}
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[...Array(3)].map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="text-left px-4 py-3 font-medium">{t('admin.toolConfigs.toolName')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.toolConfigs.profileId')}</th>
                <th className="text-left px-4 py-3 font-medium">{t('admin.toolConfigs.description')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y">
              {configs?.map((tc) => (
                <tr key={tc.id} className="hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3 font-mono text-sm">{tc.tool_name}</td>
                  <td className="px-4 py-3">
                    <Badge variant="outline">{tc.profile_id}</Badge>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground truncate max-w-xs">{tc.description ?? '—'}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 justify-end">
                      <Button variant="ghost" size="icon" onClick={() => setEditTarget(tc)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="text-destructive" onClick={() => setDeleteTarget(tc)}>
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {configs?.length === 0 && (
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
            <DialogTitle>{t('admin.toolConfigs.createTitle')}</DialogTitle>
          </DialogHeader>
          <ToolConfigForm onSubmit={(p) => muCreate.mutate(p)} onCancel={() => setCreateOpen(false)} availableTools={availableTools} departments={departments} isLoading={muCreate.isPending} />
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
              availableTools={availableTools}
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
    </div>
  )
}

function ToolConfigForm({ onSubmit, onCancel, initial, availableTools, departments, isLoading }: {
  onSubmit: (p: ToolConfigCreate) => void
  onCancel: () => void
  initial?: ToolConfigOut
  availableTools: AvailableTool[]
  departments: DepartmentOut[]
  isLoading: boolean
}) {
  const { t } = useTranslation()
  const [toolName, setToolName] = useState(initial?.tool_name ?? '')
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
        <Input
          value={profileId}
          onChange={(e) => setProfileId(e.target.value)}
        />
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
