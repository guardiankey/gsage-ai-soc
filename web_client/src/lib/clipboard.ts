/**
 * Copy ``text`` to the system clipboard.
 *
 * Tries the modern async Clipboard API first (``navigator.clipboard.writeText``),
 * then falls back to a hidden ``<textarea>`` + ``document.execCommand('copy')``
 * for environments where the API is unavailable — most notably non-secure
 * contexts (HTTP outside of ``localhost``) where ``navigator.clipboard`` is
 * ``undefined`` and would throw ``Cannot read properties of undefined``.
 *
 * Returns ``true`` on success, ``false`` otherwise. Never throws.
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (err) {
      console.warn('clipboard.writeText failed, falling back:', err)
    }
  }
  // Legacy fallback for non-secure contexts and older browsers.
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch (err) {
    console.error('clipboard fallback failed:', err)
    return false
  }
}
