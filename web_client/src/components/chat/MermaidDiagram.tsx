import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, Copy, Download } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/contexts/ThemeContext'
import { copyTextToClipboard } from '@/lib/clipboard'

interface Props {
  code: string
}

type RenderState = 'loading' | 'rendered' | 'error'

let _diagramCounter = 0

export function MermaidDiagram({ code }: Props) {
  const { t } = useTranslation()
  const { theme } = useTheme()
  const idRef = useRef(`md-${(++_diagramCounter).toString(36)}`)
  const [state, setState] = useState<RenderState>('loading')
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let cancelled = false
    setState('loading')
    setSvg('')
    setError('')

    void (async () => {
      const uid = `${idRef.current}-${Date.now()}`
      try {
        const { default: mermaid } = await import('mermaid')
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: theme === 'dark' ? 'dark' : 'default',
        })
        const { svg: out } = await mermaid.render(uid, code)
        if (!cancelled) {
          setSvg(out)
          setState('rendered')
        }
      } catch (err) {
        // Mermaid appends an error element to document.body with the render id
        // when rendering fails — remove it to prevent it showing in the page.
        document.getElementById(uid)?.remove()

        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err)
          setError(msg)
          setState('error')
          toast.warning(t('chat.mermaidRenderError'), { description: msg.slice(0, 120) })
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [code, theme])

  const handleCopy = async () => {
    const ok = await copyTextToClipboard(code)
    if (ok) {
      setCopied(true)
      toast.success(t('chat.mermaidCopySuccess'))
      setTimeout(() => setCopied(false), 2000)
    } else {
      toast.error(t('chat.mermaidCopyError'))
    }
  }

  const handleDownload = () => {
    if (!svg) return
    const blob = new Blob([svg], { type: 'image/svg+xml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'diagram.svg'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (state === 'loading') {
    return (
      <div className="mermaid-loading">
        <span className="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce [animation-delay:0ms]" />
        <span className="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce [animation-delay:150ms]" />
        <span className="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce [animation-delay:300ms]" />
        <span className="text-xs text-muted-foreground ms-2">{t('chat.mermaidLoading')}</span>
      </div>
    )
  }

  if (state === 'error') {
    return (
      <div className="mermaid-error">
        <pre className="text-xs overflow-x-auto whitespace-pre-wrap break-all">
          <code>{code}</code>
        </pre>
      </div>
    )
  }

  return (
    <div className="mermaid-container">
      <div className="mermaid-toolbar">
        <Button
          size="icon"
          variant="ghost"
          className="h-6 w-6"
          onClick={handleCopy}
          title={t('chat.mermaidCopyCode')}
        >
          {copied
            ? <Check className="h-3 w-3 text-green-500" />
            : <Copy className="h-3 w-3" />
          }
        </Button>
        <Button
          size="icon"
          variant="ghost"
          className="h-6 w-6"
          onClick={handleDownload}
          title={t('chat.mermaidDownloadSvg')}
        >
          <Download className="h-3 w-3" />
        </Button>
      </div>
      {/* mermaid securityLevel:'strict' prevents XSS in SVG output */}
      <div className="mermaid-svg-wrapper" dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  )
}
