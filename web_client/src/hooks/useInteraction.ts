import { useState, useEffect, useCallback } from 'react'
import { useAuth } from '@/contexts/AuthContext'
import { apiClient } from '@/api/client'
import type { InteractionSchema } from '@/components/interaction/FormRenderer'

export interface InteractionEvent {
  interaction_id: string
  interaction_type: string
  title: string
  description: string
  schema: InteractionSchema | null
  resume_mode: string
  timeout_seconds: number
  submit_label?: string
  cancel_label?: string
  size?: string
  execution_id?: string
  tool_call_id?: string
}

export interface InteractionState {
  visible: boolean
  interactionId: string | null
  interactionType: string | null
  title: string
  description: string
  schema: InteractionSchema | null
  resumeMode: string | null
  submitLabel?: string
  cancelLabel?: string
  size?: string
  executionId: string | null
  toolCallId: string | null
}

interface UseInteractionReturn {
  state: InteractionState
  submit: (interactionId: string, responses: Record<string, unknown>) => Promise<void>
  cancel: (interactionId: string) => Promise<void>
  dismiss: () => void
  handleEvent: (event: InteractionEvent) => void
}

export function useInteraction(orgId: string | null): UseInteractionReturn {
  const [state, setState] = useState<InteractionState>({
    visible: false,
    interactionId: null,
    interactionType: null,
    title: '',
    description: '',
    schema: null,
    resumeMode: null,
    executionId: null,
    toolCallId: null,
  })

  const dismiss = useCallback(() => {
    setState((prev) => ({ ...prev, visible: false }))
  }, [])

  const handleEvent = useCallback((event: InteractionEvent) => {
    setState({
      visible: true,
      interactionId: event.interaction_id,
      interactionType: event.interaction_type,
      title: event.title,
      description: event.description,
      schema: event.schema,
      resumeMode: event.resume_mode,
      submitLabel: event.submit_label || undefined,
      cancelLabel: event.cancel_label || undefined,
      size: event.size || undefined,
      executionId: event.execution_id || null,
      toolCallId: event.tool_call_id || null,
    })
  }, [])

  const submit = useCallback(
    async (interactionId: string, responses: Record<string, unknown>) => {
      if (!orgId) return
      await apiClient.post(
        `/v1/orgs/${orgId}/interactions/${interactionId}/submit`,
        { responses }
      )
      dismiss()
    },
    [orgId, dismiss]
  )

  const cancel = useCallback(
    async (interactionId: string) => {
      if (!orgId) return
      await apiClient.post(
        `/v1/orgs/${orgId}/interactions/${interactionId}/cancel`,
        {}
      )
      dismiss()
    },
    [orgId, dismiss]
  )

  return { state, submit, cancel, dismiss, handleEvent }
}
