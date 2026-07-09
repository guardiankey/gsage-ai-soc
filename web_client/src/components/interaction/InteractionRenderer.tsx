import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { FormRenderer, type FieldSchema, type InteractionSchema } from './FormRenderer'
import { useTranslation } from 'react-i18next'
import { useState } from 'react'

interface Props {
  open: boolean
  interactionId: string | null
  title: string
  description: string
  schema: InteractionSchema | null
  submitLabel?: string
  cancelLabel?: string
  size?: string
  isLoading: boolean
  onSubmit: (interactionId: string, responses: Record<string, unknown>) => void
  onCancel: (interactionId: string) => void
  onClose: () => void
}

function sizeClass(size?: string): string {
  switch (size) {
    case 'sm': return 'sm:max-w-sm'
    case 'lg': return 'sm:max-w-lg'
    case 'xl': return 'sm:max-w-xl'
    default:   return 'sm:max-w-md'
  }
}

export function InteractionRenderer({
  open,
  interactionId,
  title,
  description,
  schema,
  submitLabel,
  cancelLabel,
  size,
  isLoading,
  onSubmit,
  onCancel,
  onClose,
}: Props) {
  const { t } = useTranslation()
  const [formRef, setFormRef] = useState<{ submit: () => void } | null>(null)

  const handleFormSubmit = (responses: Record<string, unknown>) => {
    if (interactionId) {
      onSubmit(interactionId, responses)
    }
  }

  const handleCancel = () => {
    if (interactionId) {
      onCancel(interactionId)
    }
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) handleCancel() }}>
      <DialogContent className={sizeClass(size)}>
        <DialogHeader>
          <DialogTitle>{title || t('interaction.title')}</DialogTitle>
          {description && (
            <DialogDescription>{description}</DialogDescription>
          )}
        </DialogHeader>

        {schema && schema.fields.length > 0 && (
          <div className="max-h-[60vh] overflow-y-auto pr-1">
            <FormRenderer
              fields={schema.fields}
              onSubmit={handleFormSubmit}
              isLoading={isLoading}
            />
          </div>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={handleCancel}
            disabled={isLoading}
          >
            {cancelLabel || t('interaction.cancel')}
          </Button>
          <Button
            onClick={() => {
              // Trigger form submit via DOM
              const form = document.querySelector('form')
              if (form) form.requestSubmit()
            }}
            disabled={isLoading}
          >
            {isLoading ? t('interaction.submitting') : (submitLabel || t('interaction.submit'))}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
