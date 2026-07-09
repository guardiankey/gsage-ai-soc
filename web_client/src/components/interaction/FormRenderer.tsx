import { useCallback, useState } from 'react'
import { TextFieldComponent } from './fields/TextField'
import { TextAreaFieldComponent } from './fields/TextAreaField'
import { NumberFieldComponent } from './fields/NumberField'
import { SelectFieldComponent } from './fields/SelectField'
import { CheckboxFieldComponent } from './fields/CheckboxField'
import { CheckboxGroupFieldComponent } from './fields/CheckboxGroupField'
import { RadioFieldComponent } from './fields/RadioField'
import { DateFieldComponent } from './fields/DateField'

export interface FieldSchema {
  id: string
  field_type: string
  label: string
  required?: boolean
  value?: unknown
  placeholder?: string
  hint?: string
  description?: string
  default?: unknown
  example?: string
  validation?: Record<string, unknown>
  visible?: boolean
  enabled?: boolean
  // Type-specific extras
  [key: string]: unknown
}

export interface InteractionSchema {
  interaction_type: string
  fields: FieldSchema[]
}

interface Props {
  fields: FieldSchema[]
  onSubmit: (responses: Record<string, unknown>) => void
  isLoading?: boolean
}

const FIELD_COMPONENTS: Record<string, React.ComponentType<{
  id: string
  label: string
  required?: boolean
  value?: unknown
  placeholder?: string
  hint?: string
  enabled?: boolean
  onChange: (id: string, value: unknown) => void
  [key: string]: unknown
}>> = {
  text: TextFieldComponent,
  textarea: TextAreaFieldComponent,
  number: NumberFieldComponent,
  select: SelectFieldComponent,
  checkbox: CheckboxFieldComponent,
  checkbox_group: CheckboxGroupFieldComponent,
  radio: RadioFieldComponent,
  date: DateFieldComponent,
}

export function FormRenderer({ fields, onSubmit, isLoading }: Props) {
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const initial: Record<string, unknown> = {}
    for (const f of fields) {
      initial[f.id] = f.value ?? f.default ?? ''
    }
    return initial
  })

  const handleChange = useCallback((id: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [id]: value }))
  }, [])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()

    // Validate required fields
    const missing: string[] = []
    for (const f of fields) {
      if (f.required && !values[f.id]) {
        missing.push(f.label)
      }
    }
    if (missing.length > 0) {
      // Let the parent handle via onSubmit — the modal shows validation errors
      // For simplicity in V1, we still submit and let the backend validate.
    }

    onSubmit(values)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 py-2">
      {fields
        .filter((f) => f.visible !== false)
        .map((f) => {
          const Component = FIELD_COMPONENTS[f.field_type]
          if (!Component) {
            // Unknown field type — render a fallback text input
            return (
              <TextFieldComponent
                key={f.id}
                id={f.id}
                label={f.label}
                required={f.required}
                value={values[f.id]}
                placeholder={f.placeholder}
                enabled={f.enabled}
                onChange={handleChange}
              />
            )
          }
          return (
            <div key={f.id}>
              <Component
                {...f}
                value={values[f.id]}
                enabled={f.enabled}
                onChange={handleChange}
              />
              {f.hint && (
                <p className="text-xs text-muted-foreground mt-0.5">{f.hint}</p>
              )}
            </div>
          )
        })}
      <button type="submit" className="hidden" disabled={isLoading} />
    </form>
  )
}
