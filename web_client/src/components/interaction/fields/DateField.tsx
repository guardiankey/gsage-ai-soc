import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldProps } from './TextField'

interface DateFieldSchema extends FieldProps {
  min_date?: string
  max_date?: string
}

export function DateFieldComponent({ id, label, required, value, placeholder, enabled, min_date, max_date, onChange }: DateFieldSchema) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
      <Input
        id={id}
        type="date"
        value={typeof value === 'string' ? value : ''}
        placeholder={placeholder}
        disabled={enabled === false}
        min={min_date}
        max={max_date}
        onChange={(e) => onChange(id, e.target.value)}
      />
    </div>
  )
}
