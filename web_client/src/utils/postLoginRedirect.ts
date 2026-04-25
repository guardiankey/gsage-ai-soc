/**
 * Helpers to persist a desired post-login destination across the
 * login + optional OTP flow.
 *
 * Used primarily by deep-link pages (e.g. /kb/download/:jobId) so that
 * an unauthenticated user is sent to /login and, after successful
 * authentication, returned to the original page instead of the default
 * /chat landing.
 *
 * The path is kept in sessionStorage to survive page reloads but be
 * scoped to the current tab.  It is consumed (and cleared) by
 * LoginPage / OTPVerifyPage upon successful login.
 */
const KEY = 'post_login_redirect'

/**
 * Validate that a given path is a safe internal redirect target.
 * Rejects absolute URLs, protocol-relative URLs and anything that
 * does not start with a single forward slash.
 */
export function isSafeInternalPath(path: string | null | undefined): path is string {
  if (!path) return false
  if (!path.startsWith('/')) return false
  if (path.startsWith('//')) return false
  return true
}

export function setPostLoginRedirect(path: string): void {
  if (!isSafeInternalPath(path)) return
  try {
    sessionStorage.setItem(KEY, path)
  } catch {
    // sessionStorage may be disabled — fall through silently.
  }
}

/**
 * Read and clear the stored redirect path.  Returns the path if one
 * was set and is safe, otherwise null.
 */
export function consumePostLoginRedirect(): string | null {
  try {
    const value = sessionStorage.getItem(KEY)
    sessionStorage.removeItem(KEY)
    return isSafeInternalPath(value) ? value : null
  } catch {
    return null
  }
}
