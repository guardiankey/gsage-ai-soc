import { useEffect } from 'react'
import i18n from '@/lib/i18n'

const RTL_LANGUAGES = ['ar']

function applyDirection(lang: string) {
  const dir = RTL_LANGUAGES.includes(lang) ? 'rtl' : 'ltr'
  document.documentElement.dir = dir
  document.documentElement.lang = lang
}

export function useDirection() {
  useEffect(() => {
    // Apply immediately based on current language (from localStorage)
    applyDirection(i18n.language || i18n.resolvedLanguage || 'en')

    i18n.on('languageChanged', applyDirection)
    return () => {
      i18n.off('languageChanged', applyDirection)
    }
  }, [])
}
