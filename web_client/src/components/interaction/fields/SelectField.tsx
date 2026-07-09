import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { FieldProps } from './TextField'

interface SelectFieldSchema extends FieldProps {
  options?: { value: string; label: string }[]
  multiple?: boolean
}

export function SelectFieldComponent({ id, label, required, value, enabled, options, onChange }: SelectFieldSchema) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
      <Select
        value={typeof value === 'string' ? value : undefined}
        disabled={enabled === false}
        onValueChange={(v) => onChange(id, v)}
      >
        <SelectTrigger id={id}>
          <SelectValue placeholder="Selecione..." />
        </SelectTrigger>
        <SelectContent>
          {(options ?? []).map((opt) => (
            <SelectItem key={opt.value} value={opt.value}>
              {opt.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
