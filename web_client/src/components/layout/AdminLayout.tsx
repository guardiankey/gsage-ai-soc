import { Navigate, NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  Building2,
  Users,
  Layers,
  FolderTree,
  Wrench,
  MonitorSmartphone,
  Mail,
  ChevronRight,
} from 'lucide-react'
import { useAuth } from '@/contexts/AuthContext'
import { cn } from '@/lib/utils'

const ADMIN_LINKS = [
  { to: '/admin/organization', labelKey: 'admin.nav.organization', icon: Building2 },
  { to: '/admin/users', labelKey: 'admin.nav.users', icon: Users },
  { to: '/admin/groups', labelKey: 'admin.nav.groups', icon: Layers },
  { to: '/admin/departments', labelKey: 'admin.nav.departments', icon: FolderTree },
  { to: '/admin/tool-configs', labelKey: 'admin.nav.toolConfigs', icon: Wrench },
  { to: '/admin/interfaces', labelKey: 'admin.nav.interfaces', icon: MonitorSmartphone },
  { to: '/admin/email-accounts', labelKey: 'admin.nav.emailAccounts', icon: Mail },
]

export function AdminLayout() {
  const { t } = useTranslation()
  const { isOrgAdmin } = useAuth()

  if (!isOrgAdmin) {
    return <Navigate to="/chat" replace />
  }

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 shrink-0 border-r bg-muted/30 overflow-y-auto p-3 flex flex-col gap-1">
        <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground px-2 py-1 mb-1">
          {t('admin.title')}
        </p>
        {ADMIN_LINKS.map(({ to, labelKey, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )
            }
          >
            <Icon className="h-4 w-4 shrink-0" />
            <span className="flex-1 truncate">{t(labelKey)}</span>
            <ChevronRight className="h-3 w-3 opacity-40" />
          </NavLink>
        ))}
      </aside>

      {/* Content */}
      <main className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
