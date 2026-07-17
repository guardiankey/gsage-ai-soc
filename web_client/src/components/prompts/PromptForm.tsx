import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { Prompt, PromptCreatePayload, PromptCategory } from '@/api/prompts'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSave: (payload: PromptCreatePayload) => void
  categories?: PromptCategory[]
  prompt?: Prompt | null  // null = create mode, Prompt = edit mode
  isSaving?: boolean
}

export function PromptForm({
  open,
  onOpenChange,
  onSave,
  categories,
  prompt,
  isSaving,
}: Props) {
  const { t } = useTranslation()
  const isEdit = !!prompt

  const [title, setTitle] = useState(prompt?.title || '')
  const [content, setContent] = useState(prompt?.content || '')
  const [description, setDescription] = useState(prompt?.description || '')
  const [scope, setScope] = useState<'personal' | 'department' | 'organization'>(
    prompt?.scope || 'personal',
  )
  const [categoryId, setCategoryId] = useState(prompt?.category_id || '')

  // Sync state when prompt prop changes (edit vs create, or switching prompts)
  useEffect(() => {
    setTitle(prompt?.title || '')
    setContent(prompt?.content || '')
    setDescription(prompt?.description || '')
    setScope(prompt?.scope || 'personal')
    setCategoryId(prompt?.category_id || '')
  }, [prompt])

  // Flatten category tree for select
  const flatCategories: { id: string; name: string; depth: number }[] = []
  function flatten(cats: PromptCategory[], depth = 0) {
    for (const cat of cats) {
      flatCategories.push({ id: cat.id, name: cat.name, depth })
      if (cat.children) flatten(cat.children, depth + 1)
    }
  }
  if (categories) flatten(categories)

  // Sentinel value for "no category" — Radix Select.Item cannot have empty value
  const NONE_VALUE = "__none__"

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || !content.trim()) return
    onSave({
      title: title.trim(),
      content: content.trim(),
      description: description.trim() || undefined,
      scope,
      category_id: categoryId && categoryId !== NONE_VALUE ? categoryId : undefined,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? t('prompts.editPrompt') : t('prompts.newPrompt')}
          </DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="prompt-title">{t('prompts.form.title')}</Label>
            <Input
              id="prompt-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Analyze firewall logs"
              maxLength={255}
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="prompt-content">{t('prompts.form.content')}</Label>
            <Textarea
              id="prompt-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Write your prompt template here..."
              rows={6}
              maxLength={10000}
              required
            />
            <p className="text-xs text-muted-foreground">
              {content.length}/10000
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="prompt-desc">{t('prompts.form.description')}</Label>
            <Input
              id="prompt-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short description shown in lists..."
              maxLength={500}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>{t('prompts.form.scope')}</Label>
              <Select value={scope} onValueChange={(v) => setScope(v as typeof scope)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="personal">{t('prompts.form.scopePersonal')}</SelectItem>
                  <SelectItem value="department">{t('prompts.form.scopeDepartment')}</SelectItem>
                  <SelectItem value="organization">{t('prompts.form.scopeOrganization')}</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>{t('prompts.form.category')}</Label>
              <Select value={categoryId} onValueChange={setCategoryId}>
                <SelectTrigger>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">— None —</SelectItem>
                  {flatCategories.map((cat) => (
                    <SelectItem key={cat.id} value={cat.id}>
                      {'\u00A0\u00A0'.repeat(cat.depth)}{cat.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t('prompts.form.cancel')}
            </Button>
            <Button type="submit" disabled={isSaving || !title.trim() || !content.trim()}>
              {isSaving ? t('prompts.form.saving') : isEdit ? t('prompts.form.update') : t('prompts.form.create')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
