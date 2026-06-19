import { Link, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  MessageSquare,
  CheckSquare,
  Files,
  Shield,
  Menu,
  X,
  CalendarClock,
} from 'lucide-react'
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { UserMenu } from './UserMenu'
import { ThemeToggle } from './ThemeToggle'
import { LanguageToggle } from './LanguageToggle'
import { DepartmentSelector } from './DepartmentSelector'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/contexts/AuthContext'
import { usePendingApprovals } from '@/hooks/usePendingApprovals'

const NAV_ITEMS = [
  { to: '/chat', labelKey: 'nav.chat', icon: MessageSquare, requiredPermission: 'sessions:read' },
  { to: '/approvals', labelKey: 'nav.approvals', icon: CheckSquare, requiredPermission: 'approvals:read' },
  { to: '/files', labelKey: 'nav.files', icon: Files, requiredPermission: 'files:upload' },
  { to: '/ai-agents', labelKey: 'nav.aiAgents', icon: CalendarClock, requiredPermission: 'scheduled_jobs:read' },
]

export function TopNav() {
  const { t } = useTranslation()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const { hasPermission } = useAuth()
  const { pendingCount } = usePendingApprovals()

  const visibleItems = NAV_ITEMS.filter(
    (item) => !item.requiredPermission || hasPermission(item.requiredPermission),
  )

  return (
    <header className="sticky top-0 z-40 w-full border-b bg-[hsl(var(--topnav-bg))] text-white shadow-md">
      <div className="flex h-14 items-center px-4 gap-2">
        {/* Logo */}
        <Link to="/chat" className="flex items-center gap-2 me-4 shrink-0">
          <Shield className="h-6 w-6 text-white" />
          <span className="font-bold text-base hidden sm:block tracking-wide">
            gSage AI
          </span>
        </Link>

        {/* Desktop nav */}
        <nav className="hidden md:flex items-center gap-1 flex-1">
          {visibleItems.map(({ to, labelKey, icon: Icon }) => {
            const active = location.pathname.startsWith(to)
            const showBadge = to === '/approvals' && pendingCount > 0
            return (
              <Link
                key={to}
                to={to}
                className={cn(
                  'relative flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors',
                  active
                    ? 'bg-white/20 text-white'
                    : 'text-white/80 hover:bg-white/10 hover:text-white'
                )}
              >
                <Icon className="h-4 w-4" />
                {t(labelKey)}
                {showBadge && (
                  <span className="ml-1 inline-flex items-center justify-center min-w-[1.1rem] h-[1.1rem] rounded-full bg-red-500 text-white text-[0.65rem] font-bold px-1 leading-none">
                    {pendingCount > 99 ? '99+' : pendingCount}
                  </span>
                )}
              </Link>
            )
          })}
        </nav>

        {/* Right side */}
        <div className="ms-auto flex items-center gap-1">
          <DepartmentSelector />
          <ThemeToggle />
          <LanguageToggle />
          <UserMenu />
        </div>

        {/* Mobile hamburger */}
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden text-white hover:bg-white/10"
          onClick={() => setMobileOpen((v) => !v)}
          aria-label="Toggle menu"
        >
          {mobileOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </Button>
      </div>

      {/* Mobile nav dropdown */}
      {mobileOpen && (
        <nav className="md:hidden border-t border-white/20 bg-[hsl(var(--topnav-bg))] px-4 py-2 flex flex-col gap-1">
          {visibleItems.map(({ to, labelKey, icon: Icon }) => {
            const active = location.pathname.startsWith(to)
            const showBadge = to === '/approvals' && pendingCount > 0
            return (
              <Link
                key={to}
                to={to}
                onClick={() => setMobileOpen(false)}
                className={cn(
                  'flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                  active ? 'bg-white/20 text-white' : 'text-white/80 hover:bg-white/10 hover:text-white'
                )}
              >
                <Icon className="h-4 w-4" />
                {t(labelKey)}
                {showBadge && (
                  <span className="ml-auto inline-flex items-center justify-center min-w-[1.1rem] h-[1.1rem] rounded-full bg-red-500 text-white text-[0.65rem] font-bold px-1 leading-none">
                    {pendingCount > 99 ? '99+' : pendingCount}
                  </span>
                )}
              </Link>
            )
          })}
        </nav>
      )}
    </header>
  )
}
