import React, { createContext, useContext, useEffect, useState, useCallback, useMemo } from 'react'
import { login as apiLogin, register as apiRegister, getMe, logout as apiLogout, storeOtpPending } from '@/api/auth'
import type { LoginRequest, RegisterRequest, MeResponse } from '@/api/auth'
import { clearAuthTokens } from '@/api/client'

interface AuthState {
  user: MeResponse | null
  orgId: string | null
  deptId: string | null
  isAuthenticated: boolean
  isLoading: boolean
}

export interface LoginResult {
  otpRequired: boolean
  otpNotEnrolled: boolean
}

interface AuthContextValue extends AuthState {
  login: (data: LoginRequest) => Promise<LoginResult>
  register: (data: RegisterRequest) => Promise<void>
  logout: () => void
  refreshUser: () => Promise<void>
  switchOrg: (orgId: string) => void
  switchDept: (deptId: string | null) => void
  permissions: string[]
  hasPermission: (permission: string) => boolean
  isOrgAdmin: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    orgId: localStorage.getItem('org_id'),
    deptId: localStorage.getItem('dept_id'),
    isAuthenticated: !!localStorage.getItem('access_token'),
    isLoading: !!localStorage.getItem('access_token'), // load user if token exists
  })

  const refreshUser = useCallback(async () => {
    try {
      const user = await getMe()
      // If we don't have an org yet, pick the first active membership
      let orgId = localStorage.getItem('org_id')
      if (!orgId && user.memberships.length > 0) {
        orgId = user.memberships.find((m) => m.is_active)?.org_id ?? user.memberships[0].org_id
        localStorage.setItem('org_id', orgId)
      }
      // If we don't have a dept yet, pick the default dept of the current org
      let deptId = localStorage.getItem('dept_id')
      if (!deptId && orgId) {
        const membership = user.memberships.find((m) => m.org_id === orgId)
        const defaultDept = membership?.departments?.find((d) => d.is_active)
        if (defaultDept) {
          deptId = defaultDept.dept_id
          localStorage.setItem('dept_id', deptId)
        }
      }
      setState({ user, orgId, deptId, isAuthenticated: true, isLoading: false })
    } catch {
      // Invalid token
      clearAuthTokens()
      setState({ user: null, orgId: null, deptId: null, isAuthenticated: false, isLoading: false })
    }
  }, [])

  // Load user on mount if token exists
  useEffect(() => {
    if (localStorage.getItem('access_token')) {
      refreshUser()
    }
  }, [refreshUser])

  const login = useCallback(async (data: LoginRequest): Promise<LoginResult> => {
    const tokens = await apiLogin(data)
    if (tokens.otp_required) {
      // Store pending OTP state and signal caller to navigate to /otp-verify
      storeOtpPending(
        tokens.otp_token ?? '',
        tokens.must_change_password ?? false,
        tokens.otp_not_enrolled ?? false,
      )
      return { otpRequired: true, otpNotEnrolled: tokens.otp_not_enrolled ?? false }
    }
    await refreshUser()
    return { otpRequired: false, otpNotEnrolled: false }
  }, [refreshUser])

  const register = useCallback(async (data: RegisterRequest) => {
    await apiRegister(data)
    await refreshUser()
  }, [refreshUser])

  const logout = useCallback(() => {
    apiLogout()
    setState({ user: null, orgId: null, deptId: null, isAuthenticated: false, isLoading: false })
  }, [])

  const switchOrg = useCallback((orgId: string) => {
    localStorage.setItem('org_id', orgId)
    // Clear dept selection when switching orgs
    localStorage.removeItem('dept_id')
    setState((prev) => ({ ...prev, orgId, deptId: null }))
  }, [])

  const switchDept = useCallback((deptId: string | null) => {
    if (deptId) {
      localStorage.setItem('dept_id', deptId)
    } else {
      localStorage.removeItem('dept_id')
    }
    setState((prev) => ({ ...prev, deptId }))
  }, [])

  const permissions: string[] = useMemo(() => {
    if (!state.user || !state.orgId) return []
    const membership = state.user.memberships.find((m) => m.org_id === state.orgId)
    return membership?.permissions ?? []
  }, [state.user, state.orgId])

  const hasPermission = useCallback(
    (permission: string) => permissions.includes(permission),
    [permissions],
  )

  const isOrgAdmin = useMemo(() => permissions.includes('admin:access'), [permissions])

  return (
    <AuthContext.Provider value={{ ...state, login, register, logout, refreshUser, switchOrg, switchDept, permissions, hasPermission, isOrgAdmin }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}
