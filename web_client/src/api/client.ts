/// <reference types="vite/client" />
import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || '/api'

export const apiClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 120_000,
  headers: { 'Content-Type': 'application/json' },
})

// Attach access token + dept header to every request
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const deptId = localStorage.getItem('dept_id')
  if (deptId) {
    config.headers['X-Department-Id'] = deptId
  }
  return config
})

let isRefreshing = false
let failedQueue: Array<{
  resolve: (value: string) => void
  reject: (reason: unknown) => void
}> = []

function processQueue(error: unknown, token: string | null) {
  failedQueue.forEach(({ resolve, reject }) => {
    if (error) reject(error)
    else resolve(token!)
  })
  failedQueue = []
}

// Auto-refresh JWT on 401
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean; _skipRedirectOn401?: boolean }

    if (error.response?.status === 401 && !originalRequest._retry) {
      // Requests that handle 401 themselves (e.g. OTP verify) opt out of the redirect
      if (originalRequest._skipRedirectOn401) {
        return Promise.reject(error)
      }
      const refreshToken = localStorage.getItem('refresh_token')
      if (!refreshToken) {
        // No refresh token — clear auth and redirect
        clearAuthTokens()
        window.location.href = '/login'
        return Promise.reject(error)
      }

      if (isRefreshing) {
        // Queue parallel requests while refreshing
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject })
        }).then((token) => {
          originalRequest.headers = { ...originalRequest.headers, Authorization: `Bearer ${token}` }
          return apiClient(originalRequest)
        })
      }

      originalRequest._retry = true
      isRefreshing = true

      try {
        const { data } = await axios.post(`${BASE_URL}/v1/auth/refresh`, {
          refresh_token: refreshToken,
        })
        const newAccessToken: string = data.access_token
        localStorage.setItem('access_token', newAccessToken)
        if (data.refresh_token) {
          localStorage.setItem('refresh_token', data.refresh_token)
        }
        processQueue(null, newAccessToken)
        originalRequest.headers = { ...originalRequest.headers, Authorization: `Bearer ${newAccessToken}` }
        return apiClient(originalRequest)
      } catch (refreshError) {
        processQueue(refreshError, null)
        clearAuthTokens()
        window.location.href = '/login'
        return Promise.reject(refreshError)
      } finally {
        isRefreshing = false
      }
    }

    return Promise.reject(error)
  }
)

export function storeAuthTokens(accessToken: string, refreshToken: string) {
  localStorage.setItem('access_token', accessToken)
  localStorage.setItem('refresh_token', refreshToken)
}

export function clearAuthTokens() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
  localStorage.removeItem('org_id')
  localStorage.removeItem('dept_id')
  localStorage.removeItem('user_info')
}

export function getAccessToken(): string | null {
  return localStorage.getItem('access_token')
}

export function getOrgId(): string | null {
  return localStorage.getItem('org_id')
}

export function getDeptId(): string | null {
  return localStorage.getItem('dept_id')
}

export function extractApiError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map((d: { msg: string }) => d.msg).join('; ')
    return error.message
  }
  if (error instanceof Error) return error.message
  return 'Unknown error'
}

// Typed wrapper for multipart/form-data uploads
export function uploadRequest(url: string, formData: FormData): Promise<AxiosResponse> {
  return apiClient.post(url, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export const SSE_URL = BASE_URL
