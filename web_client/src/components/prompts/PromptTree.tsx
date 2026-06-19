import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight, Folder, FolderOpen, Star, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import type { PromptCategory } from '@/api/prompts'

interface Props {
  categories: PromptCategory[]
  selectedId?: string | null
  onSelect: (categoryId: string | null) => void
  showFavorites?: boolean
  onFavoritesClick?: () => void
  favoritesActive?: boolean
  onEditCategory?: (cat: PromptCategory) => void
  onDeleteCategory?: (cat: PromptCategory) => void
}

function CategoryNode({
  cat,
  depth,
  selectedId,
  onSelect,
  onEditCategory,
  onDeleteCategory,
}: {
  cat: PromptCategory
  depth: number
  selectedId?: string | null
  onSelect: (id: string) => void
  onEditCategory?: (cat: PromptCategory) => void
  onDeleteCategory?: (cat: PromptCategory) => void
}) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(depth < 1)
  const hasChildren = cat.children && cat.children.length > 0
  const isSelected = selectedId === cat.id
  const canDelete = cat.prompt_count === 0 && (!cat.children || cat.children.length === 0)
  const hasMenu = onEditCategory || onDeleteCategory

  return (
    <div>
      <div className="group flex items-center">
        <button
          className={cn(
            'flex flex-1 items-center gap-1.5 rounded-md px-2 py-1 text-sm transition-colors',
            'hover:bg-accent',
            isSelected && 'bg-accent font-medium',
          )}
          style={{ paddingLeft: `${depth * 16 + 8}px` }}
          onClick={() => {
            if (hasChildren) setExpanded(!expanded)
            onSelect(cat.id)
          }}
        >
          {hasChildren ? (
            <ChevronRight
              className={cn(
                'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform',
                expanded && 'rotate-90',
              )}
            />
          ) : (
            <span className="w-3.5 shrink-0" />
          )}
          {expanded ? (
            <FolderOpen className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate">{cat.name}</span>
          {cat.prompt_count > 0 && (
            <span className="ml-auto shrink-0 text-xs tabular-nums text-muted-foreground">
              {cat.prompt_count}
            </span>
          )}
        </button>

        {hasMenu && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className={cn(
                  'h-6 w-6 shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-foreground group-hover:opacity-100',
                )}
                onClick={(e) => e.stopPropagation()}
              >
                <MoreHorizontal className="h-3.5 w-3.5" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-36">
              {onEditCategory && (
                <DropdownMenuItem onClick={() => onEditCategory(cat)}>
                  <Pencil className="mr-2 h-3.5 w-3.5" />
                  {t('common.edit')}
                </DropdownMenuItem>
              )}
              {onDeleteCategory && (
                <DropdownMenuItem
                  onClick={() => onDeleteCategory(cat)}
                  disabled={!canDelete}
                  className={!canDelete ? '' : 'text-destructive'}
                  title={!canDelete ? t('prompts.categories.cannotDeleteNonEmpty') : undefined}
                >
                  <Trash2 className="mr-2 h-3.5 w-3.5" />
                  {t('common.delete')}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {expanded && hasChildren && (
        <div>
          {cat.children.map((child) => (
            <CategoryNode
              key={child.id}
              cat={child}
              depth={depth + 1}
              selectedId={selectedId}
              onSelect={onSelect}
              onEditCategory={onEditCategory}
              onDeleteCategory={onDeleteCategory}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function PromptTree({
  categories,
  selectedId,
  onSelect,
  showFavorites,
  onFavoritesClick,
  favoritesActive,
  onEditCategory,
  onDeleteCategory,
}: Props) {
  const { t } = useTranslation()

  return (
    <div className="space-y-0.5">
      {showFavorites && (
        <button
          className={cn(
            'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-sm transition-colors',
            'hover:bg-accent',
            favoritesActive && 'bg-accent font-medium text-amber-600',
          )}
          onClick={onFavoritesClick}
        >
          <Star
            className={cn(
              'h-3.5 w-3.5 shrink-0',
              favoritesActive ? 'fill-current text-amber-500' : 'text-muted-foreground',
            )}
          />
          <span>{t('prompts.myFavorites')}</span>
        </button>
      )}

      <button
        className={cn(
          'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-sm transition-colors',
          'hover:bg-accent',
          selectedId === null && 'bg-accent font-medium',
        )}
        onClick={() => onSelect(null)}
      >
        <span className="w-3.5 shrink-0" />
        <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span>{t('prompts.allPrompts')}</span>
      </button>

      {categories.map((cat) => (
        <CategoryNode
          key={cat.id}
          cat={cat}
          depth={0}
          selectedId={selectedId}
          onSelect={onSelect}
          onEditCategory={onEditCategory}
          onDeleteCategory={onDeleteCategory}
        />
      ))}
    </div>
  )
}
