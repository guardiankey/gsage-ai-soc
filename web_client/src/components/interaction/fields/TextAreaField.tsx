import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import type { FieldProps } from './TextField'

interface TextAreaFieldSchema extends FieldProps {
  rows?: number
  max_length?: number
}

export function TextAreaFieldComponent({ id, label, required, value, placeholder, enabled, rows, max_length, onChange }: TextAreaFieldSchema) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
      <Textarea
        id={id}
        value={typeof value === 'string' ? value : ''}
        placeholder={placeholder}
        disabled={enabled === false}
        rows={rows ?? 4}
        maxLength={max_length}
        onChange={(e) => onChange(id, e.target.value)}
      />
    </div>
  )
}
