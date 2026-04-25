"""Admin Console screens package."""

from .api_keys import ApiKeysPanel
from .approval_rules import ApprovalRulesPanel
from .background_tasks import BackgroundTasksPanel
from .dashboard import DashboardPanel
from .datastores_browse import DatastoresBrowsePanel
from .docker_status import DockerPanel
from .email_accounts import EmailAccountsPanel
from .es_inspect import EsInspectPanel
from .files_browse import FilesBrowsePanel
from .group_manage import GroupManagePanel
from .interface_profiles import InterfaceProfilesPanel
from .knowledge_browse import KnowledgeBrowsePanel
from .maintenance import MaintenancePanel
from .dept_manage import DeptManagePanel
from .org_manage import OrgManagePanel
from .org_selector import OrgSelectorModal
from .redis_inspect import RedisInspectPanel
from .scheduled_jobs import ScheduledJobsPanel
from .sessions_browse import SessionsBrowsePanel
from .settings_view import SettingsViewPanel
from .tool_config import ToolConfigPanel
from .user_manage import UserManagePanel

__all__ = [
    "ApiKeysPanel",
    "ApprovalRulesPanel",
    "BackgroundTasksPanel",
    "DashboardPanel",
    "DatastoresBrowsePanel",
    "DeptManagePanel",
    "DockerPanel",
    "EmailAccountsPanel",
    "EsInspectPanel",
    "FilesBrowsePanel",
    "GroupManagePanel",
    "InterfaceProfilesPanel",
    "KnowledgeBrowsePanel",
    "MaintenancePanel",
    "OrgManagePanel",
    "OrgSelectorModal",
    "RedisInspectPanel",
    "ScheduledJobsPanel",
    "SessionsBrowsePanel",
    "SettingsViewPanel",
    "ToolConfigPanel",
    "UserManagePanel",
]
