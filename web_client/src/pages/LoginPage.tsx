import { useState } from 'react'
import { Link, useNavigate, useLocation, useSearchParams } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { Shield, Eye, EyeOff, Loader2, ArrowLeft } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/contexts/AuthContext'
import { extractApiError } from '@/api/client'
import { usePublicConfig } from '@/hooks/usePublicConfig'
import { consumePostLoginRedirect } from '@/utils/postLoginRedirect'
import { lookupAuth, type AuthLookupResponse } from '@/api/auth'

type Step = 'email' | 'password'

interface EmailForm {
  email: string
}

interface PasswordForm {
  password: string
}

export function LoginPage() {
  const { t } = useTranslation()
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const otpExpired = (location.state as { otpExpired?: boolean } | null)?.otpExpired ?? false
  const ssoErrorParam = searchParams.get('sso_error')

  const [step, setStep] = useState<Step>('email')
  const [email, setEmail] = useState('')
  const [lookup, setLookup] = useState<AuthLookupResponse | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(
    ssoErrorParam ? t('auth.ssoError', { error: ssoErrorParam }) : null,
  )
  const { allow_self_register } = usePublicConfig()

  const emailForm = useForm<EmailForm>()
  const passwordForm = useForm<PasswordForm>()

  const onEmailSubmit = async (data: EmailForm) => {
    setError(null)
    try {
      const result = await lookupAuth(data.email)
      setEmail(data.email)
      setLookup(result)
      setStep('password')
    } catch (err) {
      setError(extractApiError(err))
    }
  }

  const onPasswordSubmit = async (data: PasswordForm) => {
    setError(null)
    try {
      const result = await login({ email, password: data.password })
      if (result.otpRequired) {
        navigate('/otp-verify')
      } else {
        const redirect = consumePostLoginRedirect()
        navigate(redirect ?? '/chat')
      }
    } catch (err) {
      setError(extractApiError(err))
    }
  }

  const handleSsoStart = (startUrl: string) => {
    const next = consumePostLoginRedirect()
    const url = next ? `${startUrl}?next=${encodeURIComponent(next)}` : startUrl
    window.location.href = url
  }

  const goBackToEmail = () => {
    setStep('email')
    setError(null)
    setLookup(null)
    passwordForm.reset()
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[hsl(var(--primary))] flex items-center justify-center shadow-lg mb-4">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">gSage AI</h1>
          <p className="text-sm text-muted-foreground mt-1">{t('auth.loginSubtitle')}</p>
        </div>

        <Card className="shadow-lg">
          <CardHeader>
            <CardTitle>{t('auth.signIn')}</CardTitle>
            <CardDescription>
              {step === 'email' ? t('auth.continueWithEmail') : t('auth.signInDesc')}
            </CardDescription>
          </CardHeader>

          {step === 'email' ? (
            <form onSubmit={emailForm.handleSubmit(onEmailSubmit)}>
              <CardContent className="space-y-4">
                {otpExpired && (
                  <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
                    {t('otp.verify.sessionExpired')}
                  </div>
                )}
                {error && (
                  <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                    {error}
                  </div>
                )}
                <div className="space-y-2">
                  <Label htmlFor="email">{t('auth.email')}</Label>
                  <Input
                    id="email"
                    type="email"
                    placeholder="you@example.com"
                    autoComplete="email"
                    autoFocus
                    {...emailForm.register('email', { required: true })}
                  />
                </div>
              </CardContent>
              <CardFooter className="flex-col gap-3">
                <Button type="submit" className="w-full" disabled={emailForm.formState.isSubmitting}>
                  {emailForm.formState.isSubmitting && <Loader2 className="me-2 h-4 w-4 animate-spin" />}
                  {t('auth.continue')}
                </Button>
                {allow_self_register && (
                  <p className="text-sm text-muted-foreground text-center">
                    {t('auth.noAccount')}{' '}
                    <Link to="/register" className="text-primary font-medium hover:underline">
                      {t('auth.createAccount')}
                    </Link>
                  </p>
                )}
              </CardFooter>
            </form>
          ) : (
            <>
              <CardContent className="space-y-4">
                <div className="flex items-center justify-between rounded-md border bg-muted/40 px-3 py-2 text-sm">
                  <span className="truncate">{email}</span>
                  <button
                    type="button"
                    onClick={goBackToEmail}
                    className="text-primary hover:underline inline-flex items-center"
                  >
                    <ArrowLeft className="me-1 h-3.5 w-3.5" />
                    {t('auth.changeEmail')}
                  </button>
                </div>

                {error && (
                  <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                    {error}
                  </div>
                )}

                {lookup && lookup.sso_providers.length > 0 && (
                  <div className="space-y-2">
                    {lookup.sso_providers.map((p) => (
                      <Button
                        key={p.name}
                        type="button"
                        variant="outline"
                        className="w-full"
                        onClick={() => handleSsoStart(p.start_url)}
                      >
                        {p.name === 'entra_oidc'
                          ? t('auth.signInWithMicrosoft')
                          : t('auth.signInWithProvider', { provider: p.display_name })}
                      </Button>
                    ))}
                    {lookup.allow_password_login && (
                      <div className="relative my-2 flex items-center">
                        <div className="flex-grow border-t border-muted-foreground/20" />
                        <span className="mx-3 text-xs uppercase text-muted-foreground">
                          {t('auth.or')}
                        </span>
                        <div className="flex-grow border-t border-muted-foreground/20" />
                      </div>
                    )}
                  </div>
                )}

                {(!lookup || lookup.allow_password_login) && (
                  <form onSubmit={passwordForm.handleSubmit(onPasswordSubmit)} className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="password">{t('auth.password')}</Label>
                      <div className="relative">
                        <Input
                          id="password"
                          type={showPassword ? 'text' : 'password'}
                          placeholder="••••••••"
                          autoComplete="current-password"
                          autoFocus
                          {...passwordForm.register('password', { required: true })}
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword((v) => !v)}
                          className="absolute end-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                          tabIndex={-1}
                        >
                          {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                    </div>
                    <Button type="submit" className="w-full" disabled={passwordForm.formState.isSubmitting}>
                      {passwordForm.formState.isSubmitting && (
                        <Loader2 className="me-2 h-4 w-4 animate-spin" />
                      )}
                      {t('auth.signIn')}
                    </Button>
                  </form>
                )}
              </CardContent>
              <CardFooter className="flex-col gap-3">
                {allow_self_register && (
                  <p className="text-sm text-muted-foreground text-center">
                    {t('auth.noAccount')}{' '}
                    <Link to="/register" className="text-primary font-medium hover:underline">
                      {t('auth.createAccount')}
                    </Link>
                  </p>
                )}
              </CardFooter>
            </>
          )}
        </Card>
      </div>
    </div>
  )
}
