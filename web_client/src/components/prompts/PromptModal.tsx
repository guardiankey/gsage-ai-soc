import { useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Search, X } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ScrollArea } from '@/components/ui/scroll-area'
import { PromptCard } from '@/components/prompts/PromptCard'
import { PromptTree } from '@/components/prompts/PromptTree'
import { useSearchPrompts, useToggleFavorite } from '@/hooks/usePrompts'
import { useCategories } from '@/hooks/usePrompts'
import { useAuth } from '@/contexts/AuthContext'
import type { Prompt } from '@/api/prompts'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelectPrompt: (content: string) => void
}

const SCOPE_TABS = [
  { value: '', labelKey: 'prompts.scopeAll' },
  { value: 'personal', labelKey: 'prompts.scopePersonal' },
  { value: 'department', labelKey: 'prompts.scopeDepartment' },
  { value: 'organization', labelKey: 'prompts.scopeOrganization' },
]

export function PromptModal({ open, onOpenChange, onSelectPrompt }: Props) {
  const { t } = useTranslation()
  const { orgId } = useAuth()

  const [query, setQuery] = useState('')
  const [scope, setScope] = useState('')
  const [categoryId, setCategoryId] = useState<string | null>(null)
  const [favoritesOnly, setFavoritesOnly] = useState(false)

  // Debounced search — we just use the query state directly with useSearchPrompts
  const { data: categories } = useCategories(orgId || undefined)

  const searchPayload = {
    query: query || undefined,
    scope: (scope || undefined) as 'personal' | 'department' | 'organization' | undefined,
    category_id: categoryId || undefined,
    favorites_only: favoritesOnly,
    page: 1,
    page_size: 50,
  }

  const { data, isLoading } = useSearchPrompts(orgId || undefined, searchPayload)
  const toggleFav = useToggleFavorite(orgId || undefined)

  const handleSelect = useCallback(
    (prompt: Prompt) => {
      onSelectPrompt(prompt.content)
      onOpenChange(false)
    },
    [onSelectPrompt, onOpenChange],
  )

  const handleCategorySelect = (id: string | null) => {
    setCategoryId(id)
    setFavoritesOnly(false)
  }

  const handleFavoritesClick = () => {
    setFavoritesOnly(!favoritesOnly)
    setCategoryId(null)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl h-[600px] max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>{t('prompts.title')}</DialogTitle>
        </DialogHeader>

        {/* Scope tabs */}
        <Tabs value={scope} onValueChange={setScope}>
          <TabsList className="w-full">
            {SCOPE_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value} className="flex-1">
                {t(tab.labelKey)}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        {/* Search bar */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-9 pr-8"
            placeholder={t('prompts.search')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              onClick={() => setQuery('')}
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        {/* Body: tree + list */}
        <div className="flex flex-1 gap-4 overflow-hidden">
          {/* Category tree */}
          <div className="w-48 shrink-0 overflow-y-auto border-r pr-2">
            {categories && (
              <PromptTree
                categories={categories}
                selectedId={categoryId}
                onSelect={handleCategorySelect}
                showFavorites
                onFavoritesClick={handleFavoritesClick}
                favoritesActive={favoritesOnly}
              />
            )}
          </div>

          {/* Prompt list */}
          <ScrollArea className="flex-1">
            {isLoading ? (
              <div className="flex items-center justify-center h-full">
                <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
              </div>
            ) : !data || data.prompts.length === 0 ? (
              <div className="flex items-center justify-center h-full">
                <p className="text-sm text-muted-foreground">
                  {query
                    ? t('prompts.noSearchResults')
                    : t('prompts.noPrompts')}
                </p>
              </div>
            ) : (
              <div className="grid gap-2 pr-1">
                {data.prompts.map((prompt) => (
                  <PromptCard
                    key={prompt.id}
                    prompt={prompt}
                    compact
                    onSelect={handleSelect}
                    onToggleFavorite={(id) => toggleFav.mutate(id)}
                  />
                ))}
              </div>
            )}
          </ScrollArea>
        </div>
      </DialogContent>
    </Dialog>
  )
}
