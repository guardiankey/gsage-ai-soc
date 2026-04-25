import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Shield, Loader2, KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { useAuth } from '@/contexts/AuthContext'
import { verifyOtp, getOtpPending, clearOtpPending } from '@/api/auth'
import { extractApiError } from '@/api/client'
import { consumePostLoginRedirect } from '@/utils/postLoginRedirect'

export function OTPVerifyPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { refreshUser } = useAuth()

  const pending = getOtpPending()
  const [code, setCode] = useState('')
  const [rememberDevice, setRememberDevice] = useState(false)
  const [isBackupMode, setIsBackupMode] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // Redirect if no pending OTP token
  useEffect(() => {
    if (!pending.otpToken) {
      navigate('/login', { replace: true })
    } else {
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = async (submittedCode?: string) => {
    const finalCode = submittedCode ?? code
    if (!finalCode.trim() || !pending.otpToken) return
    setError(null)
    setIsSubmitting(true)
    try {
      await verifyOtp({
        otp_token: pending.otpToken,
        code: finalCode.trim(),
        remember_device: rememberDevice,
        user_agent: navigator.userAgent,
      })
      await refreshUser()
      const redirect = consumePostLoginRedirect()
      navigate(redirect ?? '/chat', { replace: true })
    } catch (err) {
      const msg = extractApiError(err)
      // If the otp_token itself is expired/invalid, restart login
      if (msg.toLowerCase().includes('expired') || msg.toLowerCase().includes('invalid or expired')) {
        clearOtpPending()
        navigate('/login', { replace: true, state: { otpExpired: true } })
        return
      }
      setError(msg)
      setCode('')
      inputRef.current?.focus()
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleCodeChange = (value: string) => {
    if (isBackupMode) {
      setCode(value)
      return
    }
    // Only digits for TOTP
    const digits = value.replace(/\D/g, '').slice(0, 6)
    setCode(digits)
    // Auto-submit when 6 digits entered
    if (digits.length === 6) {
      handleSubmit(digits)
    }
  }

  const handleCancel = () => {
    clearOtpPending()
    navigate('/login', { replace: true })
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-md">
        {/* Brand */}
        <div className="flex flex-col items-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[hsl(var(--primary))] flex items-center justify-center shadow-lg mb-4">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">gSage AI</h1>
          <p className="text-sm text-muted-foreground mt-1">{t('auth.loginSubtitle')}</p>
        </div>

        <Card className="shadow-lg">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="h-5 w-5" />
              {t('otp.verify.title')}
            </CardTitle>
            <CardDescription>
              {isBackupMode ? t('otp.verify.descBackup') : t('otp.verify.desc')}
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4">
            {error && (
              <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {pending.notEnrolled && !isBackupMode && (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
                {t('otp.verify.notEnrolledHint')}
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="otp-code">
                {isBackupMode ? t('otp.verify.backupCode') : t('otp.verify.code')}
              </Label>
              <Input
                id="otp-code"
                ref={inputRef}
                value={code}
                onChange={(e) => handleCodeChange(e.target.value)}
                placeholder={isBackupMode ? 'XXXXXXXX-XXXXXXXX' : '000000'}
                inputMode={isBackupMode ? 'text' : 'numeric'}
                maxLength={isBackupMode ? 20 : 6}
                className="text-center text-xl tracking-[0.5em] font-mono"
                autoComplete="one-time-code"
                disabled={isSubmitting}
              />
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="remember-device"
                checked={rememberDevice}
                onCheckedChange={(v: boolean | 'indeterminate') => setRememberDevice(v === true)}
                disabled={isBackupMode}
              />
              <Label htmlFor="remember-device" className="text-sm font-normal cursor-pointer">
                {t('otp.verify.rememberDevice')}
              </Label>
            </div>
          </CardContent>

          <CardFooter className="flex-col gap-2">
            <Button
              className="w-full"
              onClick={() => handleSubmit()}
              disabled={isSubmitting || !code.trim()}
            >
              {isSubmitting && <Loader2 className="me-2 h-4 w-4 animate-spin" />}
              {t('otp.verify.submit')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="w-full text-muted-foreground"
              onClick={() => {
                setIsBackupMode((v) => !v)
                setCode('')
                setError(null)
              }}
            >
              {isBackupMode ? t('otp.verify.useTotp') : t('otp.verify.useBackup')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="w-full text-muted-foreground"
              onClick={handleCancel}
            >
              {t('common.cancel')}
            </Button>
          </CardFooter>
        </Card>
      </div>
    </div>
  )
}
