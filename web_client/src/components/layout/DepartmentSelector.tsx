import { useTranslation } from 'react-i18next'
import { Layers, ChevronDown } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/contexts/AuthContext'

export function DepartmentSelector() {
  const { user, orgId, deptId, switchDept } = useAuth()
  const { t } = useTranslation()

  if (!user || !orgId) return null

  const currentOrg = user.memberships.find((m) => m.org_id === orgId)
  const departments = currentOrg?.departments?.filter((d) => d.is_active) ?? []

  if (departments.length === 0) return null

  const currentDept = departments.find((d) => d.dept_id === deptId)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="flex items-center gap-2 text-white/80 hover:text-white hover:bg-white/10 h-8 px-2"
        >
          <Layers className="h-4 w-4" />
          <span className="hidden sm:block text-sm max-w-[120px] truncate">
            {currentDept ? currentDept.dept_name : t('dept.select')}
          </span>
          <ChevronDown className="h-3 w-3 opacity-70" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel>{t('dept.label')}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {departments.map((d) => (
          <DropdownMenuItem
            key={d.dept_id}
            onClick={() => switchDept(d.dept_id)}
            className={d.dept_id === deptId ? 'bg-accent' : ''}
          >
            <Layers className="h-4 w-4 mr-2" />
            {d.dept_name}
            <span className="ml-auto text-xs text-muted-foreground">{d.role}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
