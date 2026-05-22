import React, { type ComponentPropsWithoutRef } from 'react'
import { useTranslation } from 'react-i18next'
import type { ExtraProps } from 'react-markdown'
import { MermaidDiagram } from './MermaidDiagram'

type Props = ComponentPropsWithoutRef<'pre'> &
  ExtraProps & {
    streamingMode?: boolean
  }

/**
 * Recursively flatten any React node tree into its plain-text content.
 *
 * react-markdown v9 normally passes a single string as the children of a
 * fenced ``<code>`` element, but some configurations (rehype plugins,
 * whitespace splitting) deliver an array of strings or even nested
 * elements. ``String()`` on such inputs produces garbage like
 * ``"line1,line2"`` or ``"[object Object]"`` — corrupting the copy-to-
 * clipboard payload while still rendering "well enough" for mermaid.
 */
function flattenChildrenToString(node: unknown): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(flattenChildrenToString).join('')
  if (React.isValidElement(node)) {
    const children = (node.props as { children?: unknown }).children
    return flattenChildrenToString(children)
  }
  return ''
}

/**
 * Custom `pre` renderer for react-markdown.
 *
 * Intercepts fenced ```mermaid blocks and renders them as interactive
 * Mermaid SVG diagrams. All other code blocks are rendered as standard
 * `<pre><code>` elements, preserving existing `.prose-chat` CSS styles.
 *
 * Pass `streamingMode={true}` during SSE streaming to skip mermaid rendering
 * (syntax may be incomplete) and show raw source with a hint instead.
 */
export function MarkdownCode({
  children,
  streamingMode = false,
  node: _node,
  ...preProps
}: Props) {
  const { t } = useTranslation()

  // Extract the inner <code> element rendered by react-markdown
  const child = React.isValidElement(children)
    ? (children as React.ReactElement<{ className?: string; children?: unknown }>)
    : null
  const childClass = child?.props?.className ?? ''

  if (childClass.includes('language-mermaid')) {
    const code = flattenChildrenToString(child?.props?.children).replace(/\n$/, '')

    if (streamingMode) {
      return (
        <div className="mermaid-streaming-hint">
          <pre {...preProps}>{children}</pre>
          <p className="text-[11px] text-muted-foreground italic mt-1">
            {t('chat.mermaidStreamingHint')}
          </p>
        </div>
      )
    }

    return <MermaidDiagram code={code} />
  }

  return <pre {...preProps}>{children}</pre>
}

/**
 * Streaming variant: wraps MarkdownCode with streamingMode=true.
 * Defined at module level to avoid re-creating on every render.
 */
export function StreamingMarkdownCode(props: ComponentPropsWithoutRef<'pre'> & ExtraProps) {
  return <MarkdownCode {...props} streamingMode />
}
