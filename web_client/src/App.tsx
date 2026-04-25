import { Suspense, lazy } from 'react'
import { useDirection } from '@/hooks/useDirection'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { AuthProvider } from '@/contexts/AuthContext'
import { ThemeProvider } from '@/contexts/ThemeContext'
import { AppLayout } from '@/components/layout/AppLayout'
import { AdminLayout } from '@/components/layout/AdminLayout'
import { LoginPage } from '@/pages/LoginPage'
import { RegisterPage } from '@/pages/RegisterPage'
import { OTPVerifyPage } from '@/pages/OTPVerifyPage'
import { OTPSetupPage } from '@/pages/OTPSetupPage'
import { KbDownloadPage } from '@/pages/KbDownloadPage'

// Lazy-loaded pages
const ChatPage = lazy(() => import('@/pages/ChatPage'))
const KnowledgePage = lazy(() => import('@/pages/KnowledgePage'))
const ApprovalsPage = lazy(() => import('@/pages/ApprovalsPage'))
const FilesPage = lazy(() => import('@/pages/FilesPage'))
const TasksPage = lazy(() => import('@/pages/TasksPage'))
const AIAgentsPage = lazy(() => import('@/pages/ScheduledJobsPage'))
const ApprovalRulesPage = lazy(() => import('@/pages/ApprovalRulesPage'))
const ProfilePage = lazy(() => import('@/pages/ProfilePage'))
const ApiKeysPage = lazy(() => import('@/pages/ApiKeysPage'))
const DataStoresPage = lazy(() => import('@/pages/DataStoresPage'))

// Admin pages
const AdminOrganizationPage = lazy(() => import('@/pages/admin/OrganizationPage'))
const AdminUsersPage = lazy(() => import('@/pages/admin/UsersPage'))
const AdminGroupsPage = lazy(() => import('@/pages/admin/GroupsPage'))
const AdminDepartmentsPage = lazy(() => import('@/pages/admin/DepartmentsPage'))
const AdminToolConfigsPage = lazy(() => import('@/pages/admin/ToolConfigsPage'))
const AdminInterfacesPage = lazy(() => import('@/pages/admin/InterfacesPage'))
const AdminEmailAccountsPage = lazy(() => import('@/pages/admin/EmailAccountsPage'))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 30, // 30s
      retry: (failureCount, error: any) => {
        if (error?.status === 401 || error?.status === 403) return false
        return failureCount < 2
      },
    },
  },
})

function PageLoader() {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="flex gap-1.5">
        <span className="w-2.5 h-2.5 rounded-full bg-[hsl(var(--primary))] animate-bounce [animation-delay:0ms]" />
        <span className="w-2.5 h-2.5 rounded-full bg-[hsl(var(--primary))] animate-bounce [animation-delay:150ms]" />
        <span className="w-2.5 h-2.5 rounded-full bg-[hsl(var(--primary))] animate-bounce [animation-delay:300ms]" />
      </div>
    </div>
  )
}

export default function App() {
  useDirection()
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ThemeProvider>
          <BrowserRouter>
            <Routes>
              {/* Public routes */}
              <Route path="/login" element={<LoginPage />} />
              <Route path="/register" element={<RegisterPage />} />
              <Route path="/otp-verify" element={<OTPVerifyPage />} />
              <Route path="/otp-setup" element={<OTPSetupPage />} />

              {/* KB download deep link — handles auth gating and triggers blob download */}
              <Route path="/kb/download/:jobId" element={<KbDownloadPage />} />

              {/* Protected routes */}
              <Route element={<AppLayout />}>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route
                  path="/chat"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ChatPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/chat/:conversationId"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ChatPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/knowledge"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <KnowledgePage />
                    </Suspense>
                  }
                />
                <Route
                  path="/approvals"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ApprovalsPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/files"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <FilesPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/tasks"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <TasksPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/ai-agents"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <AIAgentsPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/approval-rules"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ApprovalRulesPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/profile"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ProfilePage />
                    </Suspense>
                  }
                />
                <Route
                  path="/api-keys"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <ApiKeysPage />
                    </Suspense>
                  }
                />
                <Route
                  path="/datastores"
                  element={
                    <Suspense fallback={<PageLoader />}>
                      <DataStoresPage />
                    </Suspense>
                  }
                />
              </Route>

              {/* Admin routes — nested under AdminLayout (guards non-admins) */}
              <Route element={<AppLayout />}>
                <Route path="/admin" element={<AdminLayout />}>
                  <Route index element={<Navigate to="/admin/organization" replace />} />
                  <Route
                    path="organization"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminOrganizationPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="users"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminUsersPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="groups"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminGroupsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="departments"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminDepartmentsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="tool-configs"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminToolConfigsPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="interfaces"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminInterfacesPage />
                      </Suspense>
                    }
                  />
                  <Route
                    path="email-accounts"
                    element={
                      <Suspense fallback={<PageLoader />}>
                        <AdminEmailAccountsPage />
                      </Suspense>
                    }
                  />
                </Route>
              </Route>

              {/* Catch-all */}
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
          </BrowserRouter>

          <Toaster
            position="top-right"
            richColors
            closeButton
            toastOptions={{ duration: 4000 }}
          />
        </ThemeProvider>
      </AuthProvider>
    </QueryClientProvider>
  )
}
