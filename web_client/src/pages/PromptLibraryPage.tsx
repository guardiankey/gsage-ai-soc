import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Plus, FolderPlus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Pagination, calcTotalPages } from '@/components/ui/pagination'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { PromptCard } from '@/components/prompts/PromptCard'
import { PromptTree } from '@/components/prompts/PromptTree'
import { PromptForm } from '@/components/prompts/PromptForm'
import {
  usePrompts,
  useFavorites,
  useCategories,
  useCreatePrompt,
  useUpdatePrompt,
  useDeletePrompt,
  useToggleFavorite,
  useCreateCategory,
  useUpdateCategory,
  useDeleteCategory,
} from '@/hooks/usePrompts'
import { useAuth } from '@/contexts/AuthContext'
import type { Prompt, PromptCreatePayload, PromptCategory } from '@/api/prompts'

export default function PromptLibraryPage() {
  const { t } = useTranslation()
  const { orgId, deptId, isOrgAdmin } = useAuth()

  const [page, setPage] = useState(1)
  const [scope, setScope] = useState('')
  const [categoryId, setCategoryId] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [formOpen, setFormOpen] = useState(false)
  const [editingPrompt, setEditingPrompt] = useState<Prompt | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [catDialogOpen, setCatDialogOpen] = useState(false)
  const [newCatName, setNewCatName] = useState('')
  const [newCatParentId, setNewCatParentId] = useState<string | null>(null)
  const [newCatDeptId, setNewCatDeptId] = useState<string | null>(null)  // null = org-level
  const [editingCategory, setEditingCategory] = useState<PromptCategory | null>(null)
  const [deletingCategory, setDeletingCategory] = useState<PromptCategory | null>(null)

  const pageSize = 20

  const { data: categories } = useCategories(orgId || undefined)
  const { data: promptsData, isLoading: promptsLoading } = usePrompts(orgId || undefined, {
    scope: scope || undefined,
    category_id: categoryId || undefined,
    page,
    page_size: pageSize,
  })
  const { data: favsData, isLoading: favsLoading } = useFavorites(orgId || undefined, page, pageSize)

  const createCategoryMutation = useCreateCategory(orgId || undefined)
  const updateCategoryMutation = useUpdateCategory(orgId || undefined)
  const deleteCategoryMutation = useDeleteCategory(orgId || undefined)

  const handleCreateCategory = () => {
    if (!newCatName.trim()) return
    const payload: { name: string; parent_id?: string; dept_id?: string | null } = {
      name: newCatName.trim(),
      parent_id: newCatParentId || undefined,
    }
    // Only send dept_id if user selected department scope (not org-level)
    if (newCatDeptId) {
      payload.dept_id = newCatDeptId
    } else {
      payload.dept_id = null  // explicitly org-level
    }
    createCategoryMutation.mutate(payload, {
      onSuccess: () => {
        setCatDialogOpen(false)
        setNewCatName('')
        setNewCatParentId(null)
        setNewCatDeptId(null)
      },
    })
  }

  const handleEditCategory = (cat: PromptCategory) => {
    setEditingCategory(cat)
    setNewCatName(cat.name)
    setNewCatParentId(cat.parent_id)
    setNewCatDeptId(cat.dept_id)
    setCatDialogOpen(true)
  }

  const handleSaveCategory = () => {
    if (!editingCategory || !newCatName.trim()) return
    updateCategoryMutation.mutate(
      {
        categoryId: editingCategory.id,
        payload: {
          name: newCatName.trim(),
          parent_id: newCatParentId || undefined,
          dept_id: newCatDeptId || null,
        },
      },
      {
        onSuccess: () => {
          setCatDialogOpen(false)
          setEditingCategory(null)
          setNewCatName('')
          setNewCatParentId(null)
          setNewCatDeptId(null)
        },
      },
    )
  }

  const handleDeleteCategory = () => {
    if (deletingCategory) {
      deleteCategoryMutation.mutate(deletingCategory.id, {
        onSuccess: () => setDeletingCategory(null),
      })
    }
  }

  const openNewCategoryDialog = () => {
    setEditingCategory(null)
    setNewCatName('')
    setNewCatParentId(null)
    setNewCatDeptId(null)
    setCatDialogOpen(true)
  }

  // Flatten category tree for parent selector
  const flatCats: { id: string; name: string; depth: number }[] = []
  function flattenCats(cats: typeof categories, depth = 0) {
    if (!cats) return
    for (const cat of cats) {
      flatCats.push({ id: cat.id, name: cat.name, depth })
      if (cat.children) flattenCats(cat.children, depth + 1)
    }
  }
  flattenCats(categories)

  const createMutation = useCreatePrompt(orgId || undefined)
  const updateMutation = useUpdatePrompt(orgId || undefined)
  const deleteMutation = useDeletePrompt(orgId || undefined)
  const toggleFav = useToggleFavorite(orgId || undefined)

  const handleSave = (payload: PromptCreatePayload) => {
    if (editingPrompt) {
      updateMutation.mutate(
        { promptId: editingPrompt.id, payload },
        { onSuccess: () => setFormOpen(false) },
      )
    } else {
      createMutation.mutate(payload, { onSuccess: () => setFormOpen(false) })
    }
  }

  const handleDelete = () => {
    if (deletingId) {
      deleteMutation.mutate(deletingId, { onSuccess: () => setDeletingId(null) })
    }
  }

  const handleEdit = (prompt: Prompt) => {
    setEditingPrompt(prompt)
    setFormOpen(true)
  }

  const handleCreateNew = () => {
    setEditingPrompt(null)
    setFormOpen(true)
  }

  const totalPages = calcTotalPages(promptsData?.total || 0, pageSize)

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-6 py-4">
        <div>
          <h1 className="text-xl font-semibold">{t('prompts.title')}</h1>
          <p className="text-sm text-muted-foreground">{t('prompts.subtitle')}</p>
        </div>
        <Button onClick={handleCreateNew}>
          <Plus className="mr-2 h-4 w-4" />
          {t('prompts.newPrompt')}
        </Button>
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-56 shrink-0 overflow-y-auto border-r p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium">{t('prompts.categories.title')}</h3>
            <Button
              size="icon"
              variant="ghost"
              className="h-7 w-7"
              onClick={openNewCategoryDialog}
              title={t('prompts.categories.newCategory')}
            >
              <FolderPlus className="h-4 w-4" />
            </Button>
          </div>
          {categories && (
            <PromptTree
              categories={categories}
              selectedId={categoryId}
              onSelect={setCategoryId}
              onEditCategory={handleEditCategory}
              onDeleteCategory={(cat) => setDeletingCategory(cat)}
            />
          )}
        </aside>

        {/* Main */}
        <main className="flex flex-1 flex-col overflow-hidden">
          {/* Search + Scope */}
          <div className="border-b p-4">
            <div className="flex gap-4">
              <Input
                className="max-w-sm"
                placeholder={t('prompts.search')}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>

          {/* Tab content */}
          <Tabs
            value={scope}
            onValueChange={(v) => { setScope(v); setPage(1) }}
            className="flex flex-1 flex-col overflow-hidden"
          >
            <TabsList className="mx-4 mt-3">
              <TabsTrigger value="">{t('prompts.scopeAll')}</TabsTrigger>
              <TabsTrigger value="personal">{t('prompts.scopePersonal')}</TabsTrigger>
              <TabsTrigger value="department">{t('prompts.scopeDepartment')}</TabsTrigger>
              <TabsTrigger value="organization">{t('prompts.scopeOrganization')}</TabsTrigger>
            </TabsList>

            <TabsContent value="" className="flex-1 overflow-y-auto p-4">
              {promptsLoading ? (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <Skeleton key={i} className="h-24 rounded-lg" />
                  ))}
                </div>
              ) : !promptsData || promptsData.prompts.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <p className="text-sm text-muted-foreground">{t('prompts.noPrompts')}</p>
                </div>
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {promptsData.prompts.map((p) => (
                      <PromptCard
                        key={p.id}
                        prompt={p}
                        onToggleFavorite={(id) => toggleFav.mutate(id)}
                        onEdit={handleEdit}
                        onDelete={(id) => setDeletingId(id)}
                      />
                    ))}
                  </div>
                  {totalPages > 1 && (
                    <div className="mt-4 flex justify-center">
                      <Pagination
                        page={page}
                        totalPages={totalPages}
                        onPageChange={setPage}
                      />
                    </div>
                  )}
                </>
              )}
            </TabsContent>

            {/* Other scopes reuse the same data filtered client-side */}
            {['personal', 'department', 'organization'].map((s) => (
              <TabsContent key={s} value={s} className="flex-1 overflow-y-auto p-4">
                {/* Content re-fetched via usePrompts with scope filter */}
                {promptsLoading ? (
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {Array.from({ length: 3 }).map((_, i) => (
                      <Skeleton key={i} className="h-24 rounded-lg" />
                    ))}
                  </div>
                ) : (
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {promptsData?.prompts.map((p) => (
                      <PromptCard
                        key={p.id}
                        prompt={p}
                        onToggleFavorite={(id) => toggleFav.mutate(id)}
                        onEdit={handleEdit}
                        onDelete={(id) => setDeletingId(id)}
                      />
                    ))}
                  </div>
                )}
              </TabsContent>
            ))}
          </Tabs>
        </main>
      </div>

      {/* Create / Edit dialog */}
      <PromptForm
        open={formOpen}
        onOpenChange={setFormOpen}
        onSave={handleSave}
        categories={categories || []}
        prompt={editingPrompt}
        isSaving={createMutation.isPending || updateMutation.isPending}
      />

      {/* Delete confirmation */}
      <AlertDialog open={!!deletingId} onOpenChange={() => setDeletingId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('prompts.confirmDelete')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('prompts.confirmDeleteDesc')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {t('common.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* New / Edit Category dialog */}
      <Dialog open={catDialogOpen} onOpenChange={(open) => { setCatDialogOpen(open); if (!open) setEditingCategory(null) }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>
              {editingCategory ? t('prompts.categories.editCategory') : t('prompts.categories.newCategory')}
            </DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => { e.preventDefault(); editingCategory ? handleSaveCategory() : handleCreateCategory() }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="new-cat-name">{t('prompts.form.title')}</Label>
              <Input
                id="new-cat-name"
                value={newCatName}
                onChange={(e) => setNewCatName(e.target.value)}
                placeholder="e.g. Incident Response"
                maxLength={100}
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label>{t('prompts.form.category')}</Label>
              <Select
                value={newCatParentId || '__none__'}
                onValueChange={(v) => setNewCatParentId(v === '__none__' ? null : v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="None (top-level)" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">— {t('prompts.categories.topLevel')} —</SelectItem>
                  {flatCats.map((cat) => (
                    <SelectItem key={cat.id} value={cat.id}>
                      {'\u00A0\u00A0'.repeat(cat.depth)}{cat.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {isOrgAdmin && (
              <div className="space-y-2">
                <Label>{t('prompts.form.scope')}</Label>
                <Select
                  value={newCatDeptId || '__org__'}
                  onValueChange={(v) => setNewCatDeptId(v === '__org__' ? null : v)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__org__">{t('prompts.scopeOrganization')}</SelectItem>
                    <SelectItem value={deptId || '__dept__'}>{t('prompts.scopeDepartment')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => { setCatDialogOpen(false); setNewCatName(''); setEditingCategory(null) }}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="submit"
                disabled={!newCatName.trim() || createCategoryMutation.isPending || updateCategoryMutation.isPending}
              >
                {createCategoryMutation.isPending || updateCategoryMutation.isPending
                  ? t('prompts.form.saving')
                  : editingCategory
                    ? t('prompts.form.update')
                    : t('common.create')}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete category confirmation */}
      <AlertDialog open={!!deletingCategory} onOpenChange={() => setDeletingCategory(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('prompts.categories.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('prompts.categories.deleteDesc', { name: deletingCategory?.name || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteCategory}
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
