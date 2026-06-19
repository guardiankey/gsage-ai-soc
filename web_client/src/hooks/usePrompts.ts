import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import {
  listCategories,
  createCategory,
  updateCategory,
  deleteCategory,
  listPrompts,
  searchPrompts,
  getPrompt,
  createPrompt,
  updatePrompt,
  deletePrompt,
  toggleFavorite,
  listFavorites,
  type SearchPayload,
  type PromptCreatePayload,
  type PromptUpdatePayload,
} from '@/api/prompts'

// ---------------------------------------------------------------------------
// Categories
// ---------------------------------------------------------------------------

export function useCategories(orgId: string | undefined) {
  return useQuery({
    queryKey: ['promptCategories', orgId],
    queryFn: () => listCategories(orgId!),
    enabled: !!orgId,
    staleTime: 5 * 60 * 1000,
  })
}

export function useCreateCategory(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (payload: { name: string; parent_id?: string; dept_id?: string | null; description?: string }) =>
      createCategory(orgId!, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['promptCategories', orgId] })
      toast.success(t('prompts.categoryCreated'))
    },
    onError: () => {
      toast.error(t('prompts.categoryCreateError'))
    },
  })
}

export function useUpdateCategory(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: ({ categoryId, payload }: { categoryId: string; payload: { name?: string; parent_id?: string; dept_id?: string | null; description?: string; is_active?: boolean } }) =>
      updateCategory(orgId!, categoryId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['promptCategories', orgId] })
      toast.success(t('prompts.categoryUpdated'))
    },
    onError: () => {
      toast.error(t('prompts.categoryUpdateError'))
    },
  })
}

export function useDeleteCategory(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (categoryId: string) => deleteCategory(orgId!, categoryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['promptCategories', orgId] })
    },
    onError: () => {
      toast.error(t('prompts.categoryDeleteError'))
    },
  })
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

export function usePrompts(
  orgId: string | undefined,
  params?: {
    scope?: string
    category_id?: string
    favorites_only?: boolean
    page?: number
    page_size?: number
  },
) {
  return useQuery({
    queryKey: ['prompts', orgId, params],
    queryFn: () => listPrompts(orgId!, params),
    enabled: !!orgId,
    staleTime: 30 * 1000,
  })
}

export function useSearchPrompts(orgId: string | undefined, payload: SearchPayload) {
  return useQuery({
    queryKey: ['promptSearch', orgId, payload],
    queryFn: () => searchPrompts(orgId!, payload),
    enabled: !!orgId && (!!payload.query || !!payload.scope || !!payload.category_id || !!payload.favorites_only),
    staleTime: 30 * 1000,
  })
}

export function usePrompt(orgId: string | undefined, promptId: string | undefined) {
  return useQuery({
    queryKey: ['prompt', orgId, promptId],
    queryFn: () => getPrompt(orgId!, promptId!),
    enabled: !!orgId && !!promptId,
  })
}

export function useCreatePrompt(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (payload: PromptCreatePayload) => createPrompt(orgId!, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['prompts', orgId] })
      toast.success(t('prompts.createSuccess'))
    },
    onError: () => {
      toast.error(t('prompts.createError'))
    },
  })
}

export function useUpdatePrompt(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: ({ promptId, payload }: { promptId: string; payload: PromptUpdatePayload }) =>
      updatePrompt(orgId!, promptId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['prompts', orgId] })
      toast.success(t('prompts.updateSuccess'))
    },
    onError: () => {
      toast.error(t('prompts.updateError'))
    },
  })
}

export function useDeletePrompt(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (promptId: string) => deletePrompt(orgId!, promptId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['prompts', orgId] })
      toast.success(t('prompts.deleteSuccess'))
    },
    onError: () => {
      toast.error(t('prompts.deleteError'))
    },
  })
}

// ---------------------------------------------------------------------------
// Favorites
// ---------------------------------------------------------------------------

export function useToggleFavorite(orgId: string | undefined) {
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (promptId: string) => toggleFavorite(orgId!, promptId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['prompts', orgId] })
      queryClient.invalidateQueries({ queryKey: ['promptSearch', orgId] })
      if (data.favorited) {
        toast.success(t('prompts.favoriteAdded'))
      } else {
        toast.success(t('prompts.favoriteRemoved'))
      }
    },
  })
}

export function useFavorites(
  orgId: string | undefined,
  page = 1,
  pageSize = 20,
) {
  return useQuery({
    queryKey: ['promptFavorites', orgId, page],
    queryFn: () => listFavorites(orgId!, page, pageSize),
    enabled: !!orgId,
    staleTime: 30 * 1000,
  })
}
