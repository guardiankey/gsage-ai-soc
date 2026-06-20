import { Star } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { Prompt } from '@/api/prompts'

interface Props {
  prompt: Prompt
  onSelect?: (prompt: Prompt) => void
  onToggleFavorite?: (promptId: string) => void
  onEdit?: (prompt: Prompt) => void
  onDelete?: (promptId: string) => void
  compact?: boolean
}

export function PromptCard({ prompt, onSelect, onToggleFavorite, onEdit, onDelete, compact }: Props) {
  const { t } = useTranslation()
  const handleFavorite = (e: React.MouseEvent) => {
    e.stopPropagation()
    onToggleFavorite?.(prompt.id)
  }

  return (
    <div
      className={cn(
        'group rounded-lg border bg-card p-3 transition-colors hover:border-primary/50',
        onSelect && 'cursor-pointer',
      )}
      onClick={() => onSelect?.(prompt)}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h4 className="truncate text-sm font-medium">{prompt.title}</h4>
          {prompt.description && (
            <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
              {prompt.description}
            </p>
          )}
          {!compact && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              {prompt.scope !== 'personal' && (
                <span className="rounded bg-muted px-1.5 py-0.5">
                  {prompt.scope === 'organization' ? 'Org' : 'Dept'}
                </span>
              )}
              {prompt.category_name && (
                <span className="truncate">{prompt.category_name}</span>
              )}
              <span className="ml-auto tabular-nums">
                {prompt.usage_count > 0 && `×${prompt.usage_count}`}
              </span>
            </div>
          )}
        </div>

        <Button
          size="icon"
          variant="ghost"
          className={cn(
            'h-7 w-7 shrink-0',
            prompt.is_favorite
              ? 'text-amber-500 hover:text-amber-600'
              : 'text-muted-foreground opacity-0 group-hover:opacity-100',
          )}
          onClick={handleFavorite}
          title={prompt.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
        >
          <Star
            className={cn('h-4 w-4', prompt.is_favorite && 'fill-current')}
          />
        </Button>
      </div>

      {!compact && (onEdit || onDelete) && (
        <div className="mt-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          {onEdit && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-xs"
              onClick={(e) => { e.stopPropagation(); onEdit(prompt) }}
            >
              {t('common.edit')}
            </Button>
          )}
          {onDelete && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-xs text-destructive"
              onClick={(e) => { e.stopPropagation(); onDelete(prompt.id) }}
            >
              {t('common.delete')}
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
