import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, Building2 } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { useAuth } from '@/contexts/AuthContext'
import { getAdminOrg, updateAdminOrg, type OrgAdminUpdate } from '@/api/admin'

export default function OrganizationPage() {
  const { t } = useTranslation()
  const { orgId } = useAuth()
  const queryClient = useQueryClient()

  const { data: org, isLoading } = useQuery({
    queryKey: ['admin', 'org', orgId],
    queryFn: () => getAdminOrg(orgId!),
    enabled: !!orgId,
  })

  const [form, setForm] = useState<OrgAdminUpdate>({})
  const [llmApiKey, setLlmApiKey] = useState('')

  const mutation = useMutation({
    mutationFn: (payload: OrgAdminUpdate) => updateAdminOrg(orgId!, payload),
    onSuccess: () => {
      toast.success(t('admin.org.saved'))
      queryClient.invalidateQueries({ queryKey: ['admin', 'org', orgId] })
      setLlmApiKey('')
    },
    onError: () => toast.error(t('common.error')),
  })

  if (isLoading || !org) {
    return (
      <div className="space-y-4 max-w-2xl">
        {[...Array(6)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
      </div>
    )
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const payload: OrgAdminUpdate = { ...form }
    if (llmApiKey) payload.llm_api_key = llmApiKey
    mutation.mutate(payload)
  }

  const field = <K extends keyof OrgAdminUpdate>(key: K, value: OrgAdminUpdate[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  return (
    <div className="max-w-2xl space-y-6">
      <div className="flex items-center gap-3">
        <Building2 className="h-6 w-6 text-muted-foreground" />
        <div>
          <h1 className="text-xl font-semibold">{t('admin.org.title')}</h1>
          <p className="text-sm text-muted-foreground">{t('admin.org.subtitle')}</p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Name */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.name')}</Label>
          <Input
            defaultValue={org.name}
            onChange={(e) => field('name', e.target.value)}
            placeholder={org.name}
          />
        </div>

        {/* Slug */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.slug')}</Label>
          <Input
            defaultValue={org.slug}
            onChange={(e) => field('slug', e.target.value)}
          />
        </div>

        {/* LLM Provider */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.llmProvider')}</Label>
          <Select
            defaultValue={org.llm_provider}
            onValueChange={(v) => field('llm_provider', v)}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {['ollama', 'openai', 'deepseek', 'anthropic', 'gemini','vllm'].map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* LLM API Key */}
        <div className="space-y-1.5">
          <Label>
            {t('admin.org.llmApiKey')}
            {org.llm_api_key_set && (
              <Badge variant="secondary" className="ml-2 text-xs">{t('admin.org.keySet')}</Badge>
            )}
          </Label>
          <Input
            type="password"
            value={llmApiKey}
            onChange={(e) => setLlmApiKey(e.target.value)}
            placeholder={t('admin.org.llmApiKeyPlaceholder')}
          />
        </div>

        {/* Maker model */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.makerModel')}</Label>
          <Input
            defaultValue={org.default_maker_model}
            onChange={(e) => field('default_maker_model', e.target.value)}
          />
        </div>

        {/* Reviewer model */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.reviewerModel')}</Label>
          <Input
            defaultValue={org.default_reviewer_model}
            onChange={(e) => field('default_reviewer_model', e.target.value)}
          />
        </div>

        {/* Agent timeout */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.agentTimeout')}</Label>
          <Input
            type="number"
            defaultValue={org.agent_timeout_seconds}
            min={10}
            max={300}
            onChange={(e) => field('agent_timeout_seconds', parseInt(e.target.value))}
          />
        </div>

        {/* Max context tokens */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.maxContextTokens')}</Label>
          <Input
            type="number"
            defaultValue={org.max_context_tokens}
            min={1000}
            max={128000}
            onChange={(e) => field('max_context_tokens', parseInt(e.target.value))}
          />
        </div>

        {/* System prompt */}
        <div className="space-y-1.5">
          <Label>{t('admin.org.systemPrompt')}</Label>
          <Textarea
            defaultValue={org.system_prompt ?? ''}
            onChange={(e) => field('system_prompt', e.target.value || null)}
            rows={5}
            placeholder={t('admin.org.systemPromptPlaceholder')}
          />
        </div>

        <Button type="submit" disabled={mutation.isPending} className="flex items-center gap-2">
          <Save className="h-4 w-4" />
          {mutation.isPending ? t('common.loading') : t('common.save')}
        </Button>
      </form>
    </div>
  )
}
