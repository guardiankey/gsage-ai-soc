import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Info, Lock, AlertTriangle } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { getToolMetadata, type ToolMetadata } from '@/api/admin'

interface ToolInfoModalProps {
  toolName: string
  orgId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export default function ToolInfoModal({ toolName, orgId, open, onOpenChange }: ToolInfoModalProps) {
  const { t } = useTranslation()

  const { data: meta, isLoading, isError } = useQuery({
    queryKey: ['admin', 'tool-metadata', orgId, toolName],
    queryFn: () => getToolMetadata(orgId, toolName),
    enabled: open,
    staleTime: Infinity, // tool metadata is static (from Python ClassVars)
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Info className="h-5 w-5 text-muted-foreground" />
            {isLoading ? (
              <Skeleton className="h-6 w-48" />
            ) : (
              <>
                {meta?.display_name || toolName}
                {meta?.version && (
                  <Badge variant="outline" className="ml-2 text-xs font-normal">
                    v{meta.version}
                  </Badge>
                )}
                {meta?.category && (
                  <Badge variant="secondary" className="text-xs font-normal">
                    {meta.category}
                  </Badge>
                )}
              </>
            )}
          </DialogTitle>
        </DialogHeader>

        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : isError || !meta ? (
          <p className="text-sm text-muted-foreground">
            {t('admin.toolConfigs.metadataError', 'Tool metadata not available.')}
          </p>
        ) : (
          <div className="space-y-5">
            {/* Description */}
            {meta.summary && (
              <p className="text-sm text-muted-foreground">{meta.summary}</p>
            )}
            {meta.description && meta.description !== meta.summary && (
              <div className="text-sm whitespace-pre-wrap leading-relaxed">
                {cleanDocstring(meta.description)}
              </div>
            )}

            {/* Config Fields */}
            {meta.config_schema != null && (meta.config_schema as Record<string, unknown>).properties != null ? (
              <div>
                <h4 className="text-sm font-semibold mb-2 flex items-center gap-1">
                  📋 {t('admin.toolConfigs.configFields', 'Config Fields')}
                  {hasSensitiveFields(meta.config_schema as Record<string, unknown>) && (
                    <span className="text-xs text-muted-foreground font-normal ml-2">
                      🔒 = {t('admin.toolConfigs.sensitive', 'sensitive')}
                    </span>
                  )}
                </h4>
                <div className="rounded-md border overflow-hidden">
                  <table className="w-full text-xs">
                    <thead className="bg-muted/50">
                      <tr>
                        <th className="text-left px-3 py-2 font-medium">
                          {t('admin.toolConfigs.field', 'Field')}
                        </th>
                        <th className="text-left px-3 py-2 font-medium">
                          {t('admin.toolConfigs.type', 'Type')}
                        </th>
                        <th className="text-center px-3 py-2 font-medium w-16">
                          {t('admin.toolConfigs.required', 'Required')}
                        </th>
                        <th className="text-left px-3 py-2 font-medium">
                          {t('admin.toolConfigs.description', 'Description')}
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {Object.entries(
                        (meta.config_schema as Record<string, unknown>).properties as Record<string, Record<string, unknown>>,
                      ).map(([fieldName, fieldSchema]) => {
                          const isSensitive = fieldSchema.sensitive === true
                          const isRequired =
                            Array.isArray((meta.config_schema as Record<string, unknown>).required) &&
                            ((meta.config_schema as Record<string, unknown>).required as string[]).includes(fieldName)
                          const defaultValue: string =
                            meta.config_defaults != null && meta.config_defaults[fieldName] !== undefined
                              ? isSensitive
                                ? '********'
                                : JSON.stringify(meta.config_defaults[fieldName])
                              : '—'

                          return (
                            <tr key={fieldName} className="hover:bg-muted/30">
                              <td className="px-3 py-2 font-mono">
                                {isSensitive && <Lock className="h-3 w-3 inline mr-1 text-amber-500" />}
                                {fieldName}
                              </td>
                              <td className="px-3 py-2 text-muted-foreground">
                                {fieldSchema.type != null ? String(fieldSchema.type) : '—'}
                              </td>
                              <td className="px-3 py-2 text-center">
                                {isRequired ? '✅' : ''}
                              </td>
                              <td className="px-3 py-2 text-muted-foreground max-w-xs truncate" title={fieldSchema.description != null ? String(fieldSchema.description) : ''}>
                                {fieldSchema.description != null ? String(fieldSchema.description) : '—'}
                                {defaultValue !== '—' && !isRequired && (
                                  <span className="block text-muted-foreground/70">
                                    Default: {defaultValue}
                                  </span>
                                )}
                              </td>
                            </tr>
                          )
                        },
                      )}
                    </tbody>
                  </table>
                </div>
                {(meta.config_schema as Record<string, unknown>).additionalProperties === false && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {t('admin.toolConfigs.noExtraFields', 'Extra fields are not allowed.')}
                  </p>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                {t('admin.toolConfigs.noConfigRequired', 'No configuration required.')}
              </p>
            )}

            {/* Environment Variables */}
            {meta.config_schema != null && (meta.config_schema as Record<string, unknown>).properties != null && (
              <div>
                <h4 className="text-sm font-semibold mb-2">
                  🌐 {t('admin.toolConfigs.envVars', 'Environment Variables')}
                </h4>
                <div className="bg-muted/50 rounded-md p-3">
                  <code className="text-xs block space-y-1">
                    {generateEnvVars(
                      toolName,
                      meta.config_schema as Record<string, unknown>,
                      meta.config_namespace,
                    ).map((envVar) => (
                        <span key={envVar} className="block">
                          {envVar}
                        </span>
                      ),
                    )}
                  </code>
                </div>
                {meta.config_namespace && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {t(
                      'admin.toolConfigs.namespaceEnvNote',
                      'Namespace-scoped vars (shared, lower precedence) shown first.',
                    )}
                  </p>
                )}
              </div>
            )}

            {/* Metadata Footer */}
            <div className="text-xs text-muted-foreground space-y-1 border-t pt-3">
              {meta.permissions.length > 0 && (
                <div>
                  <span className="font-medium">
                    {t('admin.toolConfigs.permissions', 'Permissions')}:
                  </span>{' '}
                  {meta.permissions.join(', ')}
                </div>
              )}
              <div>
                <span className="font-medium">
                  {t('admin.toolConfigs.rateLimit', 'Rate limit')}:
                </span>{' '}
                {meta.rate_limit_per_minute}/min
              </div>
              {meta.config_namespace && (
                <div>
                  <span className="font-medium">
                    {t('admin.toolConfigs.configNamespace', 'Config namespace')}:
                  </span>{' '}
                  <code>{meta.config_namespace}</code>
                </div>
              )}
              {meta.requires_approval && (
                <div className="flex items-center gap-1 text-amber-600">
                  <AlertTriangle className="h-3 w-3" />
                  {t('admin.toolConfigs.requiresApproval', 'Requires human approval')}
                </div>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Clean RST/Markdown artifacts from Python docstrings for UI display.
 *  - Strips ``Permission:`` lines (shown separately in the footer)
 *  - Converts `` ``text`` `` → `text`
 *  - Normalises lists: leading ``* `` → ``• ``
 *  - Collapses 3+ blank lines into 2 */
function cleanDocstring(raw: string): string {
  return raw
    .split('\n')
    .filter((line) => !/^\s*Permission:\s*/i.test(line))
    .map((line) => {
      // Replace double backticks with nothing (inline code in UI doesn't need them)
      let cleaned = line.replace(/``/g, '')
      // Convert RST list markers to bullet
      cleaned = cleaned.replace(/^(\s*)\*\s/, '$1• ')
      return cleaned
    })
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function hasSensitiveFields(configSchema: Record<string, unknown>): boolean {
  const properties = configSchema.properties as Record<string, Record<string, unknown>> | undefined
  if (!properties) return false
  return Object.values(properties).some((f) => f.sensitive === true)
}

function generateEnvVars(
  toolName: string,
  configSchema: Record<string, unknown>,
  configNamespace: string | null,
): string[] {
  const properties = configSchema.properties as Record<string, Record<string, unknown>> | undefined
  if (!properties) return []

  const toolPrefix = toolName.toUpperCase()
  const vars: string[] = []

  // Namespace-scoped vars (shared, lower precedence)
  if (configNamespace) {
    const nsPrefix = configNamespace.toUpperCase()
    for (const fieldName of Object.keys(properties)) {
      vars.push(`TOOL_${nsPrefix}__${fieldName.toUpperCase()}`)
    }
  }

  // Tool-scoped vars (per-tool, higher precedence)
  for (const fieldName of Object.keys(properties)) {
    vars.push(`TOOL_${toolPrefix}__${fieldName.toUpperCase()}`)
  }

  return vars
}
