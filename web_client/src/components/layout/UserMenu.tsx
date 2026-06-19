import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LogOut, User, Key, KeyRound, ChevronDown, Building2, Layers, BookOpen, ShieldCheck, Database, Activity, Library, Settings } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
} from '@/components/ui/dropdown-menu'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { useAuth } from '@/contexts/AuthContext'

function getInitials(name: string): string {
  return name
    .split(' ')
    .slice(0, 2)
    .map((n) => n[0]?.toUpperCase() ?? '')
    .join('')
}

export function UserMenu() {
  const { user, orgId, deptId, logout, switchOrg, switchDept, hasPermission } = useAuth()
  const { t } = useTranslation()
  const navigate = useNavigate()

  if (!user) return null

  const initials = getInitials(user.full_name || user.email)
  const currentOrg = user.memberships.find((m) => m.org_id === orgId)
  const hasMultipleOrgs = user.memberships.length > 1
  const currentOrgDepts = currentOrg?.departments?.filter((d) => d.is_active) ?? []
  const hasMultipleDepts = currentOrgDepts.length > 1

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="flex items-center gap-2 rounded-full px-2 py-1 hover:bg-white/10 transition-colors outline-none">
          <Avatar className="h-7 w-7">
            <AvatarFallback className="bg-white/20 text-white text-xs font-semibold">
              {initials}
            </AvatarFallback>
          </Avatar>
          <span className="hidden sm:block text-sm text-white/90 max-w-[120px] truncate">
            {user.full_name || user.email}
          </span>
          <ChevronDown className="h-3 w-3 text-white/70 hidden sm:block" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-56" align="end">
        <DropdownMenuLabel className="font-normal">
          <div className="flex flex-col space-y-1">
            <p className="text-sm font-medium leading-none">{user.full_name}</p>
            <p className="text-xs leading-none text-muted-foreground">{user.email}</p>
            {currentOrg && (
              <p className="text-xs leading-none text-muted-foreground mt-1">
                {currentOrg.org_name} · {currentOrg.role}
              </p>
            )}
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/profile" className="flex items-center gap-2 cursor-pointer">
            <User className="h-4 w-4" />
            {t('nav.profile')}
          </Link>
        </DropdownMenuItem>
        {hasPermission('apikeys:personal') && (
          <DropdownMenuItem asChild>
            <Link to="/api-keys" className="flex items-center gap-2 cursor-pointer">
              <Key className="h-4 w-4" />
              {t('nav.apiKeys')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('credentials:personal') && (
          <DropdownMenuItem asChild>
            <Link to="/credentials" className="flex items-center gap-2 cursor-pointer">
              <KeyRound className="h-4 w-4" />
              {t('nav.credentials')}
            </Link>
          </DropdownMenuItem>
        )}

        {/* Secondary navigation items */}
        {hasPermission('knowledge:read') && (
          <DropdownMenuItem asChild>
            <Link to="/knowledge" className="flex items-center gap-2 cursor-pointer">
              <BookOpen className="h-4 w-4" />
              {t('nav.knowledge')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('approval_rules:read') && (
          <DropdownMenuItem asChild>
            <Link to="/approval-rules" className="flex items-center gap-2 cursor-pointer">
              <ShieldCheck className="h-4 w-4" />
              {t('nav.approvalRules')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('datastores:read') && (
          <DropdownMenuItem asChild>
            <Link to="/datastores" className="flex items-center gap-2 cursor-pointer">
              <Database className="h-4 w-4" />
              {t('nav.datastores')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('agents:run') && (
          <DropdownMenuItem asChild>
            <Link to="/tasks" className="flex items-center gap-2 cursor-pointer">
              <Activity className="h-4 w-4" />
              {t('nav.tasks')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('prompts:read') && (
          <DropdownMenuItem asChild>
            <Link to="/prompts" className="flex items-center gap-2 cursor-pointer">
              <Library className="h-4 w-4" />
              {t('nav.prompts')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasPermission('admin:access') && (
          <DropdownMenuItem asChild>
            <Link to="/admin" className="flex items-center gap-2 cursor-pointer">
              <Settings className="h-4 w-4" />
              {t('nav.admin')}
            </Link>
          </DropdownMenuItem>
        )}
        {hasMultipleDepts && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger className="flex items-center gap-2">
                <Layers className="h-4 w-4" />
                {t('dept.switchDept')}
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {currentOrgDepts.map((d) => (
                  <DropdownMenuItem
                    key={d.dept_id}
                    onClick={() => switchDept(d.dept_id)}
                    className={d.dept_id === deptId ? 'bg-accent' : ''}
                  >
                    <Layers className="h-4 w-4 me-2" />
                    {d.dept_name}
                    <span className="ml-auto text-xs text-muted-foreground">{d.role}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          </>
        )}
        {hasMultipleOrgs && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger className="flex items-center gap-2">
                <Building2 className="h-4 w-4" />
                {t('nav.switchOrg')}
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {user.memberships.map((m) => (
                  <DropdownMenuItem
                    key={m.org_id}
                    onClick={() => switchOrg(m.org_id)}
                    className={m.org_id === orgId ? 'bg-accent' : ''}
                  >
                    <Building2 className="h-4 w-4 me-2" />
                    {m.org_name}
                    <span className="ml-auto text-xs text-muted-foreground">{m.role}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={handleLogout}
          className="text-destructive focus:text-destructive focus:bg-destructive/10"
        >
          <LogOut className="h-4 w-4 mr-2" />
          {t('auth.logout')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
