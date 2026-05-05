import { apiClient, storeAuthTokens, clearAuthTokens } from './client'

export interface LoginRequest {
  email: string
  password: string
  org_id?: string
}

export interface RegisterRequest {
  email: string
  password: string
  full_name: string
  org_name: string
  org_slug?: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  must_change_password?: boolean
  // OTP two-step flow
  otp_required?: boolean
  otp_not_enrolled?: boolean
  otp_token?: string
  device_token?: string
}

export interface DepartmentMembership {
  dept_id: string
  dept_name: string
  dept_slug: string
  role: string
  is_active: boolean
}

export interface UserMembership {
  org_id: string
  org_name: string
  org_slug: string
  role: string
  is_active: boolean
  permissions: string[]
  departments?: DepartmentMembership[]
}

export interface MeResponse {
  id: string
  email: string
  full_name: string
  is_active: boolean
  is_superuser: boolean
  default_dept_id?: string | null
  memberships: UserMembership[]
}

export interface OTPSetupResponse {
  secret: string
  provisioning_uri: string
  qr_code: string
}

export interface OTPConfirmResponse {
  backup_codes: string[]
}

export interface OTPStatusResponse {
  otp_enabled: boolean
  otp_confirmed_at?: string
  backup_codes_count?: number
}

export interface OrgOTPConfigResponse {
  policy: 'disabled' | 'optional' | 'required'
  trusted_networks: string[]
  remember_device_days: number
  issuer_name: string
}

export interface OrgOTPConfigRequest {
  policy?: 'disabled' | 'optional' | 'required'
  trusted_networks?: string[]
  remember_device_days?: number
  issuer_name?: string
}

// ---- Device token persistence ----

export function getDeviceToken(): string | null {
  return localStorage.getItem('device_token')
}

export function storeDeviceToken(token: string) {
  localStorage.setItem('device_token', token)
}

export function clearDeviceToken() {
  localStorage.removeItem('device_token')
}

// ---- OTP pending state (sessionStorage — cleared on tab close) ----

export function storeOtpPending(otpToken: string, mustChangePassword: boolean, notEnrolled: boolean) {
  sessionStorage.setItem('otp_token', otpToken)
  sessionStorage.setItem('otp_must_change_password', String(mustChangePassword))
  sessionStorage.setItem('otp_not_enrolled', String(notEnrolled))
}

export function getOtpPending() {
  return {
    otpToken: sessionStorage.getItem('otp_token'),
    mustChangePassword: sessionStorage.getItem('otp_must_change_password') === 'true',
    notEnrolled: sessionStorage.getItem('otp_not_enrolled') === 'true',
  }
}

export function clearOtpPending() {
  sessionStorage.removeItem('otp_token')
  sessionStorage.removeItem('otp_must_change_password')
  sessionStorage.removeItem('otp_not_enrolled')
}

// ---- Token parsing ----

function parseOrgFromToken(accessToken: string): string | null {
  try {
    const parts = accessToken.split('.')
    if (parts.length < 2) return null
    const padded = parts[1] + '='.repeat((4 - (parts[1].length % 4)) % 4)
    const claims = JSON.parse(atob(padded.replace(/-/g, '+').replace(/_/g, '/')))
    return claims.org_id ?? null
  } catch {
    return null
  }
}

function finalizeLogin(tokens: TokenResponse) {
  storeAuthTokens(tokens.access_token, tokens.refresh_token)
  const orgId = parseOrgFromToken(tokens.access_token)
  if (orgId) localStorage.setItem('org_id', orgId)
  if (tokens.device_token) storeDeviceToken(tokens.device_token)
}

// ---- Auth endpoints ----

export async function login(data: LoginRequest): Promise<TokenResponse> {
  const deviceToken = getDeviceToken()
  const response = await apiClient.post<TokenResponse>('/v1/auth/login', data, {
    headers: deviceToken ? { 'X-Device-Token': deviceToken } : {},
    _skipRedirectOn401: true,
  } as never)
  const tokens = response.data
  if (!tokens.otp_required) {
    finalizeLogin(tokens)
  }
  return tokens
}

export async function register(data: RegisterRequest): Promise<TokenResponse> {
  const response = await apiClient.post<TokenResponse>('/v1/auth/register', data, {
    _skipRedirectOn401: true,
  } as never)
  const tokens = response.data
  finalizeLogin(tokens)
  return tokens
}

export async function getMe(): Promise<MeResponse> {
  const response = await apiClient.get<MeResponse>('/v1/auth/me')
  return response.data
}

export async function updateProfile(data: {
  full_name?: string
  default_dept_id?: string | null
}): Promise<MeResponse> {
  const response = await apiClient.patch<MeResponse>('/v1/auth/me', data)
  return response.data
}

export async function changePassword(data: {
  current_password: string
  new_password: string
}): Promise<void> {
  await apiClient.post('/v1/auth/me/change-password', data)
}

export function logout() {
  clearAuthTokens()
  // Device token is intentionally preserved so the trusted-device bypass
  // survives explicit logouts within the remember-device window (30 days).
}

// ---- OTP endpoints ----

export async function verifyOtp(data: {
  otp_token: string
  code: string
  remember_device?: boolean
  user_agent?: string
}): Promise<TokenResponse> {
  const response = await apiClient.post<TokenResponse>('/v1/auth/otp/verify', data, {
    _skipRedirectOn401: true,
  } as never)
  const tokens = response.data
  finalizeLogin(tokens)
  clearOtpPending()
  return tokens
}

export async function otpSetup(): Promise<OTPSetupResponse> {
  const response = await apiClient.post<OTPSetupResponse>('/v1/auth/otp/setup')
  return response.data
}

export async function otpConfirm(code: string): Promise<OTPConfirmResponse> {
  const response = await apiClient.post<OTPConfirmResponse>('/v1/auth/otp/confirm', { code })
  return response.data
}

export async function otpDisable(data: { password?: string; code?: string }): Promise<void> {
  await apiClient.delete('/v1/auth/otp', { data })
}

export async function otpStatus(): Promise<OTPStatusResponse> {
  const response = await apiClient.get<OTPStatusResponse>('/v1/auth/otp/status')
  return response.data
}

export async function regenerateBackupCodes(data: { password?: string; code?: string }): Promise<OTPConfirmResponse> {
  const response = await apiClient.post<OTPConfirmResponse>('/v1/auth/otp/backup-codes/regenerate', data)
  return response.data
}

export async function getOrgOtpConfig(orgId: string): Promise<OrgOTPConfigResponse> {
  const response = await apiClient.get<OrgOTPConfigResponse>(`/v1/orgs/${orgId}/settings/otp`)
  return response.data
}

export async function updateOrgOtpConfig(orgId: string, data: OrgOTPConfigRequest): Promise<OrgOTPConfigResponse> {
  const response = await apiClient.put<OrgOTPConfigResponse>(`/v1/orgs/${orgId}/settings/otp`, data)
  return response.data
}

// ---- SSO discovery + completion ----

export interface SsoProviderInfo {
  name: string
  display_name: string
  start_url: string
}

export interface AuthLookupResponse {
  org_slug: string | null
  allow_password_login: boolean
  sso_providers: SsoProviderInfo[]
}

export async function lookupAuth(email: string): Promise<AuthLookupResponse> {
  const response = await apiClient.post<AuthLookupResponse>(
    '/v1/auth/lookup',
    { email },
    { _skipRedirectOn401: true } as never,
  )
  return response.data
}

export async function completeSsoLogin(sessionToken: string): Promise<TokenResponse> {
  const response = await apiClient.post<TokenResponse>(
    '/v1/auth/sso/complete',
    { session_token: sessionToken },
    { _skipRedirectOn401: true } as never,
  )
  const tokens = response.data
  finalizeLogin(tokens)
  return tokens
}

