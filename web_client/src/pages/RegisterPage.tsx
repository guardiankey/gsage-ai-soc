import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { Shield, Eye, EyeOff, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/contexts/AuthContext'
import { extractApiError } from '@/api/client'
import { usePublicConfig } from '@/hooks/usePublicConfig'

interface RegisterForm {
  full_name: string
  email: string
  password: string
  org_name: string
}

export function RegisterPage() {
  const { t } = useTranslation()
  const { register: registerUser } = useAuth()
  const navigate = useNavigate()
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { allow_self_register, isLoading: configLoading } = usePublicConfig()

  if (!configLoading && !allow_self_register) {
    navigate('/login', { replace: true })
    return null
  }

  const {
    register,
    handleSubmit,
    formState: { isSubmitting, errors },
  } = useForm<RegisterForm>()

  const onSubmit = async (data: RegisterForm) => {
    setError(null)
    try {
      await registerUser(data)
      navigate('/chat')
    } catch (err) {
      setError(extractApiError(err))
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center mb-8">
          <div className="h-14 w-14 rounded-2xl bg-[hsl(var(--primary))] flex items-center justify-center shadow-lg mb-4">
            <Shield className="h-8 w-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">gSage AI</h1>
          <p className="text-sm text-muted-foreground mt-1">{t('auth.registerSubtitle')}</p>
        </div>

        <Card className="shadow-lg">
          <CardHeader>
            <CardTitle>{t('auth.createAccount')}</CardTitle>
            <CardDescription>{t('auth.createAccountDesc')}</CardDescription>
          </CardHeader>
          <form onSubmit={handleSubmit(onSubmit)}>
            <CardContent className="space-y-4">
              {error && (
                <div className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                  {error}
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="full_name">{t('auth.fullName')}</Label>
                <Input
                  id="full_name"
                  type="text"
                  placeholder={t('auth.fullNamePlaceholder')}
                  autoFocus
                  {...register('full_name', { required: true })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="email">{t('auth.email')}</Label>
                <Input
                  id="email"
                  type="email"
                  placeholder="you@example.com"
                  autoComplete="email"
                  {...register('email', { required: true })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="password">{t('auth.password')}</Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="••••••••"
                    autoComplete="new-password"
                    {...register('password', { required: true, minLength: 8 })}
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
                {errors.password?.type === 'minLength' && (
                  <p className="text-xs text-destructive">{t('auth.passwordMinLength')}</p>
                )}
              </div>
              <div className="space-y-2">
                <Label htmlFor="org_name">{t('auth.orgName')}</Label>
                <Input
                  id="org_name"
                  type="text"
                  placeholder={t('auth.orgNamePlaceholder')}
                  {...register('org_name', { required: true })}
                />
              </div>
            </CardContent>
            <CardFooter className="flex-col gap-3">
              <Button type="submit" className="w-full" disabled={isSubmitting}>
                {isSubmitting && <Loader2 className="me-2 h-4 w-4 animate-spin" />}
                {t('auth.createAccount')}
              </Button>
              <p className="text-sm text-muted-foreground text-center">
                {t('auth.hasAccount')}{' '}
                <Link to="/login" className="text-primary font-medium hover:underline">
                  {t('auth.signIn')}
                </Link>
              </p>
            </CardFooter>
          </form>
        </Card>
      </div>
    </div>
  )
}
