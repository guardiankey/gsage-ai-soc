import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import type { FieldProps } from './TextField'

export function CheckboxFieldComponent({ id, label, required, value, enabled, onChange }: FieldProps) {
  return (
    <div className="flex items-center gap-2">
      <Checkbox
        id={id}
        checked={!!value}
        disabled={enabled === false}
        onCheckedChange={(checked) => onChange(id, checked ? 'true' : 'false')}
      />
      <Label htmlFor={id} className="cursor-pointer">
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </Label>
    </div>
  )
}
