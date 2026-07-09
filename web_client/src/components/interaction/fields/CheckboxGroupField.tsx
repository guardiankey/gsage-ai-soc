import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import type { FieldProps } from './TextField'

interface CheckboxGroupFieldSchema extends FieldProps {
  options?: { value: string; label: string }[]
}

export function CheckboxGroupFieldComponent({ id, label, required, value, enabled, options, onChange }: CheckboxGroupFieldSchema) {
  const selected: string[] = (() => {
    if (Array.isArray(value)) return value
    if (typeof value === 'string' && value.length > 0) {
      try { return JSON.parse(value) as string[] } catch { /* fall through */ }
    }
    return []
  })()

  const handleToggle = (optValue: string, checked: boolean) => {
    let next: string[]
    if (checked) {
      next = [...selected, optValue]
    } else {
      next = selected.filter((v) => v !== optValue)
    }
    onChange(id, next)
  }

  return (
    <fieldset className="space-y-1.5">
      <legend className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70">
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </legend>
      <div className="space-y-2">
        {(options ?? []).map((opt) => {
          const isChecked = selected.includes(opt.value)
          return (
            <label key={opt.value} className="flex items-center gap-2 cursor-pointer">
              <Checkbox
                checked={isChecked}
                disabled={enabled === false}
                onCheckedChange={(checked) => handleToggle(opt.value, !!checked)}
              />
              <span className="text-sm">{opt.label}</span>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}
