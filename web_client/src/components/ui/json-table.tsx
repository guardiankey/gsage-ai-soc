/**
 * JsonTable — Recursive nested-table renderer for arbitrary JSON values.
 * Objects render as key/value rows; arrays render indexed rows; primitives
 * render inline with light type-based colouring.
 */

type JsonPrimitive = string | number | boolean | null | undefined

function isPrimitive(value: unknown): value is JsonPrimitive {
  return value === null || value === undefined || typeof value !== 'object'
}

function PrimitiveValue({ value }: { value: JsonPrimitive }) {
  if (value === null || value === undefined)
    return <span className="text-muted-foreground italic">null</span>
  if (typeof value === 'boolean')
    return (
      <span className={value ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'}>
        {String(value)}
      </span>
    )
  if (typeof value === 'number')
    return <span className="text-blue-600 dark:text-blue-400">{value}</span>
  return <span className="break-all">{value}</span>
}

function JsonArray({ items, depth }: { items: unknown[]; depth: number }) {
  if (items.length === 0)
    return <span className="text-muted-foreground italic">[]</span>

  // All-primitive arrays: render compact inline list
  if (items.every(isPrimitive)) {
    return (
      <span>
        {(items as JsonPrimitive[]).map((item, i) => (
          <span key={i}>
            <PrimitiveValue value={item} />
            {i < items.length - 1 && <span className="text-muted-foreground">,&nbsp;</span>}
          </span>
        ))}
      </span>
    )
  }

  return (
    <div className="space-y-1 ps-1">
      {items.map((item, i) => (
        <div key={i} className="flex gap-2 items-start">
          <span className="text-muted-foreground shrink-0 select-none font-mono">[{i}]</span>
          <div className="flex-1 min-w-0">
            <JsonValue value={item} depth={depth + 1} />
          </div>
        </div>
      ))}
    </div>
  )
}

function JsonObjectTable({ obj, depth }: { obj: Record<string, unknown>; depth: number }) {
  const entries = Object.entries(obj)
  if (entries.length === 0)
    return <span className="text-muted-foreground italic">{'{}'}</span>

  return (
    <table className="w-full border-collapse text-xs">
      <tbody>
        {entries.map(([key, val]) => (
          <tr key={key} className="border-b border-border/50 last:border-0 align-top">
            <td className="py-1 pe-3 font-medium text-muted-foreground whitespace-nowrap align-top w-px">
              {key}
            </td>
            <td className="py-1 align-top">
              <JsonValue value={val} depth={depth + 1} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function JsonValue({ value, depth }: { value: unknown; depth: number }) {
  if (isPrimitive(value)) return <PrimitiveValue value={value} />
  if (Array.isArray(value)) return <JsonArray items={value} depth={depth} />
  return (
    <div className={depth > 0 ? 'border border-border/40 rounded p-1.5 bg-muted/20' : undefined}>
      <JsonObjectTable obj={value as Record<string, unknown>} depth={depth} />
    </div>
  )
}

interface JsonTableProps {
  data: unknown
  className?: string
}

export function JsonTable({ data, className }: JsonTableProps) {
  if (data === null || data === undefined) return null

  return (
    <div className={className}>
      <JsonValue value={data} depth={0} />
    </div>
  )
}
