import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Shield, Copy, Download, Check, Loader2, KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { otpSetup, otpConfirm } from '@/api/auth'
import type { OTPSetupResponse } from '@/api/auth'
import { extractApiError } from '@/api/client'
import { toast } from 'sonner'

type Step = 'setup' | 'confirm' | 'backup'

export function OTPSetupPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()

  const [step, setStep] = useState<Step>('setup')
  const [setupData, setSetupData] = useState<OTPSetupResponse | null>(null)
  const [backupCodes, setBackupCodes] = useState<string[]>([])
  const [code, setCode] = useState('')
  const [secretCopied, setSecretCopied] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleStart = async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await otpSetup()
      setSetupData(data)
      setStep('setup')
    } catch (err) {
      setError(extractApiError(err))
    } finally {
      setIsLoading(false)
    }
  }

  const handleConfirm = async () => {
    if (!code.trim()) return
    setIsLoading(true)
    setError(null)
    try {
      const result = await otpConfirm(code.trim())
      setBackupCodes(result.backup_codes)
      setStep('backup')
    } catch (err) {
      setError(extractApiError(err))
      setCode('')
    } finally {
      setIsLoading(false)
    }
  }

  const copySecret = async () => {
    if (!setupData) return
    await navigator.clipboard.writeText(setupData.secret)
    setSecretCopied(true)
    toast.success(t('otp.setup.secretCopied'))
    setTimeout(() => setSecretCopied(false), 2000)
  }

  const copyBackupCodes = async () => {
    await navigator.clipboard.writeText(backupCodes.join('\n'))
    toast.success(t('otp.setup.backupCodesCopied'))
  }

  const downloadBackupCodes = () => {
    const blob = new Blob([backupCodes.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'gsage-ai-backup-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleDone = () => {
    navigate('/profile', { replace: true })
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-lg">
        {/* Brand */}
        <div className="flex flex-col items-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[hsl(var(--primary))] flex items-center justify-center shadow-lg mb-4">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">gSage AI</h1>
        </div>

        <Card className="shadow-lg">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="h-5 w-5" />
              {t('otp.setup.title')}
            </CardTitle>
            <CardDescription>
              {step === 'setup' && (setupData ? t('otp.setup.scanDesc') : t('otp.setup.initDesc'))}
              {step === 'confirm' && t('otp.setup.confirmDesc')}
              {step === 'backup' && t('otp.setup.backupDesc')}
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4">
            {error && (
              <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* Step: initial / QR display */}
            {step === 'setup' && !setupData && (
              <p className="text-sm text-muted-foreground">{t('otp.setup.initHint')}</p>
            )}

            {step === 'setup' && setupData && (
              <div className="space-y-4">
                {/* QR Code */}
                <div className="flex justify-center">
                  <img
                    src={setupData.qr_code}
                    alt={t('otp.setup.qrAlt')}
                    className="w-48 h-48 border rounded-lg"
                  />
                </div>
                {/* Manual secret */}
                <div className="space-y-1">
                  <Label className="text-xs text-muted-foreground">{t('otp.setup.manualSecret')}</Label>
                  <div className="flex gap-2">
                    <Input
                      value={setupData.secret}
                      readOnly
                      className="font-mono text-sm"
                    />
                    <Button variant="outline" size="icon" onClick={copySecret}>
                      {secretCopied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                    </Button>
                  </div>
                </div>
                {/* Confirm code input */}
                <div className="space-y-1">
                  <Label htmlFor="confirm-code">{t('otp.setup.enterCode')}</Label>
                  <Input
                    id="confirm-code"
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    placeholder="000000"
                    inputMode="numeric"
                    maxLength={6}
                    className="text-center text-xl tracking-[0.5em] font-mono"
                    autoComplete="one-time-code"
                  />
                </div>
              </div>
            )}

            {/* Step: backup codes */}
            {step === 'backup' && (
              <div className="space-y-3">
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
                  {t('otp.setup.backupWarning')}
                </div>
                <div className="grid grid-cols-2 gap-2 font-mono text-sm">
                  {backupCodes.map((bc) => (
                    <div key={bc} className="rounded bg-muted px-3 py-1.5 text-center tracking-wider">
                      {bc}
                    </div>
                  ))}
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" className="flex-1" onClick={copyBackupCodes}>
                    <Copy className="me-2 h-4 w-4" />
                    {t('otp.setup.copyBackup')}
                  </Button>
                  <Button variant="outline" size="sm" className="flex-1" onClick={downloadBackupCodes}>
                    <Download className="me-2 h-4 w-4" />
                    {t('otp.setup.downloadBackup')}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>

          <CardFooter className="flex gap-2">
            {step === 'setup' && !setupData && (
              <Button className="flex-1" onClick={handleStart} disabled={isLoading}>
                {isLoading && <Loader2 className="me-2 h-4 w-4 animate-spin" />}
                {t('otp.setup.start')}
              </Button>
            )}

            {step === 'setup' && setupData && (
              <>
                <Button
                  variant="outline"
                  onClick={() => navigate('/profile')}
                  className="flex-1"
                >
                  {t('common.cancel')}
                </Button>
                <Button
                  className="flex-1"
                  onClick={handleConfirm}
                  disabled={isLoading || code.length !== 6}
                >
                  {isLoading && <Loader2 className="me-2 h-4 w-4 animate-spin" />}
                  {t('otp.setup.confirm')}
                </Button>
              </>
            )}

            {step === 'backup' && (
              <Button className="flex-1" onClick={handleDone}>
                {t('otp.setup.done')}
              </Button>
            )}
          </CardFooter>
        </Card>
      </div>
    </div>
  )
}
