import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Loader2, Shield } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/contexts/AuthContext'
import { completeSsoLogin } from '@/api/auth'
import { extractApiError } from '@/api/client'

export function SsoCallbackPage() {
  const { t } = useTranslation()
  const { refreshUser } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [error, setError] = useState<string | null>(null)
  const calledRef = useRef(false)

  useEffect(() => {
    if (calledRef.current) return
    calledRef.current = true

    const token = searchParams.get('token')
    const next = searchParams.get('next') || '/chat'

    if (!token) {
      setError(t('auth.ssoFailed'))
      return
    }

    ;(async () => {
      try {
        await completeSsoLogin(token)
        await refreshUser()
        // Only allow same-origin path redirects
        const safeNext = next.startsWith('/') && !next.startsWith('//') ? next : '/chat'
        navigate(safeNext, { replace: true })
      } catch (err) {
        const message = extractApiError(err)
        navigate(`/login?sso_error=${encodeURIComponent(message)}`, { replace: true })
      }
    })()
  }, [searchParams, navigate, refreshUser, t])

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[hsl(var(--primary))] flex items-center justify-center shadow-lg mb-4">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">gSage AI</h1>
        </div>
        <Card className="shadow-lg">
          <CardHeader>
            <CardTitle>{t('auth.completingSso')}</CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-center py-8">
            {error ? (
              <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            ) : (
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
