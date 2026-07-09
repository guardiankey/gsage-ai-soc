import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export interface FieldProps {
  id: string
  label: string
  required?: boolean
  value?: unknown
  placeholder?: string
  hint?: string
  enabled?: boolean
  onChange: (id: string, value: unknown) => void
}

interface TextFieldSchema extends FieldProps {
  max_length?: number
  min_length?: number
}

export function TextFieldComponent({ id, label, required, value, placeholder, enabled, max_length, min_length, onChange }: TextFieldSchema) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
      <Input
        id={id}
        type="text"
        value={typeof value === 'string' ? value : ''}
        placeholder={placeholder}
        disabled={enabled === false}
        maxLength={max_length}
        minLength={min_length}
        onChange={(e) => onChange(id, e.target.value)}
      />
    </div>
  )
}
