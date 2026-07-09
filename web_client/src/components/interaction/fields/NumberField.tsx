import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { FieldProps } from './TextField'

interface NumberFieldSchema extends FieldProps {
  min?: number
  max?: number
  step?: number
}

export function NumberFieldComponent({ id, label, required, value, placeholder, enabled, min, max, step, onChange }: NumberFieldSchema) {
  const displayValue = value === null || value === undefined ? '' : String(value)
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
      <Input
        id={id}
        type="number"
        value={displayValue}
        placeholder={placeholder}
        disabled={enabled === false}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(id, e.target.value)}
      />
    </div>
  )
}
