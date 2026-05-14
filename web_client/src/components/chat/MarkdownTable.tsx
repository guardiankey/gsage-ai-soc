import type { TableHTMLAttributes } from 'react'

/**
 * Custom `<table>` renderer for ReactMarkdown.
 *
 * Wraps the table in a horizontally scrollable container so wide tables
 * (e.g. long query strings inside cells) never push the chat bubble past
 * the viewport. The `<table>` itself keeps `display: table` to preserve
 * column alignment between rows.
 *
 * The `node` prop injected by react-markdown is stripped so it is not
 * forwarded to the DOM (which would render `node="[object Object]"`).
 */
type Props = TableHTMLAttributes<HTMLTableElement> & { node?: unknown }

export function MarkdownTable({ node: _node, children, ...rest }: Props) {
  void _node
  return (
    <div className="prose-chat-table-wrapper">
      <table {...rest}>{children}</table>
    </div>
  )
}
