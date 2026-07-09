"""gSage AI — aggregated v1 API router."""

from fastapi import APIRouter, Depends

from src.backend_api.app.api.middleware.rate_limit import check_rate_limit
from src.backend_api.app.api.v1 import (
    admin_emails,
    admin_groups,
    admin_interfaces,
    admin_organization,
    admin_tool_configs,
    admin_users,
    agents,
    api_keys,
    approval_rules,
    approvals,
    auth,
    auth_lookup,
    auth_sso,
    background_tasks,
    channels_teams,
    chat,
    credentials,
    datastores,
    departments,
    files,
    health,
    interactions,
    knowledge,
    org_settings,
    prompts,
    scheduled_jobs,
    sessions,
)
from src.backend_api.app.api.deps import get_tenant_context, require_org_admin

api_router = APIRouter()

# Routes that do NOT require tenant auth (no rate limiting)
api_router.include_router(health.router, prefix="/v1", tags=["Health"])
api_router.include_router(auth.router, prefix="/v1/auth", tags=["Auth"])
api_router.include_router(auth_lookup.router, prefix="/v1/auth", tags=["Auth"])
api_router.include_router(auth_sso.router, prefix="/v1/auth", tags=["Auth SSO"])

# Microsoft Teams webhook — auth is enforced by Bot Framework JWT validation
# inside ``BotFrameworkAdapter.process_activity`` (not gsage tenant auth).
# Mounted without rate-limiting and without org admin gating; multi-tenant
# isolation is achieved via the ``profile_id`` path parameter.
api_router.include_router(channels_teams.router, tags=["Teams"])

# Org-scoped routes — rate-limited via check_rate_limit dependency
# FastAPI resolves get_tenant_context (sub-dep of check_rate_limit) first;
# subsequent route deps that declare get_tenant_context use the cached result.
_org_router = APIRouter(dependencies=[Depends(check_rate_limit)])
_org_router.include_router(chat.router, prefix="/v1", tags=["Chat"])
_org_router.include_router(sessions.router, prefix="/v1", tags=["Sessions"])
_org_router.include_router(agents.router, prefix="/v1", tags=["Agents"])
_org_router.include_router(
    api_keys.router,
    prefix="/v1/orgs/{org_id}/api-keys",
    tags=["API Keys"],
)
_org_router.include_router(
    api_keys.personal_router,
    prefix="/v1/orgs/{org_id}/me/api-keys",
    tags=["API Keys"],
)
_org_router.include_router(
    credentials.router,
    prefix="/v1/orgs/{org_id}/me/credentials",
    tags=["Credentials"],
)
_org_router.include_router(knowledge.router, prefix="/v1", tags=["Knowledge"])
_org_router.include_router(approvals.router, prefix="/v1", tags=["Approvals"])
_org_router.include_router(files.router, prefix="/v1", tags=["Files"])
_org_router.include_router(interactions.router, prefix="/v1", tags=["Interactions"])
_org_router.include_router(background_tasks.router, prefix="/v1", tags=["Background Tasks"])
_org_router.include_router(scheduled_jobs.router, prefix="/v1", tags=["Scheduled Jobs"])
_org_router.include_router(approval_rules.router, prefix="/v1", tags=["Approval Rules"])
_org_router.include_router(org_settings.router, prefix="/v1", tags=["Org Settings"])
_org_router.include_router(
    prompts.router,
    prefix="/v1",
    tags=["Prompts"],
)
_org_router.include_router(
    departments.router,
    prefix="/v1/orgs/{org_id}/depts",
    tags=["Departments"],
)
_org_router.include_router(
    datastores.router,
    prefix="/v1/orgs/{org_id}/depts/{dept_id}/datastores",
    tags=["Data Stores"],
)

api_router.include_router(_org_router)

# Admin routes — restricted to org admins and owners, rate-limited
_admin_router = APIRouter(dependencies=[Depends(require_org_admin), Depends(check_rate_limit)])
_admin_router.include_router(admin_organization.router, tags=["Admin"])
_admin_router.include_router(admin_users.router, tags=["Admin"])
_admin_router.include_router(admin_groups.router, tags=["Admin"])
_admin_router.include_router(admin_tool_configs.router, tags=["Admin"])
_admin_router.include_router(admin_interfaces.router, tags=["Admin"])
_admin_router.include_router(admin_emails.router, tags=["Admin"])
api_router.include_router(_admin_router, prefix="/v1/orgs/{org_id}/admin")

# SSE stream route — auth required (get_tenant_context) but excluded from rate limiting.
_stream_router = APIRouter(dependencies=[Depends(get_tenant_context)])
_stream_router.include_router(chat.stream_router, prefix="/v1", tags=["Chat"])
api_router.include_router(_stream_router)

# Short knowledge-base download alias — /api/kb/download/{job_id}.
# Auth is enforced by the endpoint itself via ``get_current_user`` + per-job
# membership check, so it's mounted without the org-scoped dependencies.
api_router.include_router(knowledge.download_router, tags=["Knowledge"])
