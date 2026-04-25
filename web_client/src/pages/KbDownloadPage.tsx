import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Loader2, AlertCircle } from 'lucide-react'
import { useAuth } from '@/contexts/AuthContext'
import { downloadFileByPath } from '@/api/files'
import { extractApiError } from '@/api/client'
import { setPostLoginRedirect } from '@/utils/postLoginRedirect'

/**
 * SPA wrapper for knowledge-base download links emitted by the LLM.
 *
 * The agent emits Markdown links like ``/kb/download/<job_id>`` that
 * users may follow via in-chat click, "open in new tab", copy/paste,
 * or even from external channels (Telegram / e-mail).  This page:
 *
 * 1. If the user is not authenticated, stores the current path in
 *    sessionStorage and redirects to ``/login``.  After login the user
 *    is brought back here and the download proceeds.
 * 2. If authenticated, calls the backend ``/api/kb/download/<job_id>``
 *    endpoint via the shared ``apiClient`` (which attaches the bearer
 *    token automatically) and triggers a blob download.
 * 3. Navigates back to ``/chat`` once the download starts.
 *
 * The /api/kb/download endpoint enforces multi-tenant access control
 * based on the authenticated user.
 */
export function KbDownloadPage() {
  const { t } = useTranslation()
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const { isAuthenticated, isLoading } = useAuth()
  const [error, setError] = useState<string | null>(null)
  // Guard against React 18 StrictMode double-invocation in dev.
  const triggered = useRef(false)

  useEffect(() => {
    if (isLoading) return
    if (!jobId) {
      navigate('/chat', { replace: true })
      return
    }

    const targetPath = `/kb/download/${jobId}`

    if (!isAuthenticated) {
      setPostLoginRedirect(targetPath)
      navigate('/login', { replace: true })
      return
    }

    if (triggered.current) return
    triggered.current = true

    void (async () => {
      try {
        await downloadFileByPath(targetPath, 'download')
        // Send the user back to chat after the download starts.
        navigate('/chat', { replace: true })
      } catch (err) {
        setError(extractApiError(err))
      }
    })()
  }, [isAuthenticated, isLoading, jobId, navigate])

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="flex flex-col items-center gap-3 text-center">
        {error ? (
          <>
            <AlertCircle className="h-8 w-8 text-destructive" />
            <p className="text-sm text-destructive">{error}</p>
            <button
              type="button"
              className="text-sm text-primary underline"
              onClick={() => navigate('/chat', { replace: true })}
            >
              {t('common.close')}
            </button>
          </>
        ) : (
          <>
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            <p className="text-sm text-muted-foreground">
              {t('kbDownload.preparing')}
            </p>
          </>
        )}
      </div>
    </div>
  )
}
