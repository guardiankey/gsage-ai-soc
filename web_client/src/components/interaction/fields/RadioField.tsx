import type { FieldProps } from './TextField'

interface RadioFieldSchema extends FieldProps {
  options?: { value: string; label: string }[]
}

export function RadioFieldComponent({ id, label, required, value, enabled, options, onChange }: RadioFieldSchema) {
  return (
    <fieldset className="space-y-1.5">
      <legend className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70">
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </legend>
      <div className="space-y-1.5">
        {(options ?? []).map((opt) => (
          <label key={opt.value} className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name={id}
              value={opt.value}
              checked={value === opt.value}
              disabled={enabled === false}
              onChange={(e) => onChange(id, e.target.value)}
              className="h-4 w-4"
            />
            <span className="text-sm">{opt.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}
