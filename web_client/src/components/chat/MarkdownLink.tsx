import type { AnchorHTMLAttributes } from 'react'
import { toast } from 'sonner'
import { downloadFileByPath } from '@/api/files'

// Matches the download path in relative form (/v1/orgs/...) or full URL form.
// The LLM may prepend the origin, so we extract just the path in both cases.
const FILE_DOWNLOAD_RE = /\/v1\/orgs\/[0-9a-f-]{36}\/files\/[0-9a-f-]{36}\/download/

// Matches the short knowledge-base download alias emitted by
// ``search_knowledge_base`` (backend: src/backend_api/app/api/v1/knowledge.py).
// Accepts both the relative form (``/kb/download/<uuid>``) and the form
// prefixed with ``/api`` (LLMs sometimes include it).
const KB_DOWNLOAD_RE = /\/kb\/download\/[0-9a-f-]{36}/

/** Extract a known download path from an href, if any. */
function extractDownloadPath(href: string): string | null {
  const fileMatch = href.match(FILE_DOWNLOAD_RE)
  if (fileMatch) return fileMatch[0]
  const kbMatch = href.match(KB_DOWNLOAD_RE)
  return kbMatch ? kbMatch[0] : null
}

/**
 * Custom `<a>` renderer for ReactMarkdown.
 *
 * Intercepts links whose `href` matches the authenticated file download
 * endpoint pattern and triggers a blob download via the API client
 * (which attaches the bearer token automatically).
 *
 * All other links are rendered as normal anchors opening in a new tab.
 */
export function MarkdownLink(props: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const { href, children, ...rest } = props

  const downloadPath = href ? extractDownloadPath(href) : null

  if (downloadPath) {
    const handleClick = async (e: React.MouseEvent) => {
      e.preventDefault()
      try {
        const filename =
          typeof children === 'string'
            ? children
            : Array.isArray(children)
              ? String(children[0] ?? 'download')
              : 'download'
        await downloadFileByPath(downloadPath, filename)
      } catch {
        toast.error('Download failed')
      }
    }

    return (
      <a href={href} onClick={handleClick} {...rest}>
        {children}
      </a>
    )
  }

  return (
    <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
      {children}
    </a>
  )
}
