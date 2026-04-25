import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '@/contexts/AuthContext'
import { TopNav } from './TopNav'
import { Skeleton } from '@/components/ui/skeleton'

export function AppLayout() {
  const { isAuthenticated, isLoading } = useAuth()

  if (isLoading) {
    return (
      <div className="flex flex-col h-screen">
        <div className="h-14 bg-[hsl(var(--topnav-bg))]" />
        <div className="flex flex-1 p-4 gap-4">
          <Skeleton className="w-64 h-full rounded-lg" />
          <Skeleton className="flex-1 h-full rounded-lg" />
        </div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  return (
    <div className="flex flex-col h-svh overflow-hidden">
      <TopNav />
      <div className="flex flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  )
}
