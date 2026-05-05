"""gSage AI — GuardianKey adaptive authentication service.

Sends authentication context to the GuardianKey API v2 (/v2/checkaccess) after
credentials have been validated. The response can be ALLOW, NOTIFY, HARD_NOTIFY,
BLOCK, or ERROR.

Behaviour:
- ALLOW    → proceed normally (default).
- NOTIFY / HARD_NOTIFY → proceed, log a warning.
- BLOCK    → reject login with a generic 401 (no info leak).
- ERROR    → fail-open (unreachable/timeout), proceed with a warning log.

All config is read from ``get_settings()`` (``GK_*`` env vars). The feature is
entirely disabled when ``GK_ENABLED=false`` (default).

API v2 protocol (from GuardianKey PHP reference):
  POST {GK_API_URL}/v2/checkaccess
  Body: {"id": authgroupid, "message": <message_json>, "hash": sha256(message_json + key_b64 + iv_b64)}
  Response: {"response": "ALLOW|BLOCK|NOTIFY|HARD_NOTIFY|ERROR", "risk": <float>}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.shared.config.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


@dataclass
class GKResponse:
    """Parsed response from the GuardianKey API."""

    response: str = "ERROR"
    risk: float = 0.0

    @property
    def should_block(self) -> bool:
        return self.response == "BLOCK"

    @property
    def should_notify(self) -> bool:
        return self.response in ("NOTIFY", "HARD_NOTIFY")

    @property
    def is_error(self) -> bool:
        return self.response == "ERROR"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GuardianKeyService:
    """Async client for the GuardianKey v2 API."""

    def _build_message(
        self,
        username: str,
        user_email: str,
        client_ip: str,
        user_agent: str,
        client_reverse: str,
        login_failed: int,
        event_type: str,
    ) -> str:
        """Build the JSON message string matching the GuardianKey PHP reference."""
        settings = get_settings()
        payload = {
            "generatedTime": int(time.time()),
            "agentId": settings.gk_agent_id,
            "organizationId": settings.gk_org_id,
            "authGroupId": settings.gk_authgroup_id,
            "service": settings.gk_service_name,
            "clientIP": client_ip,
            "clientReverse": client_reverse,
            "userName": username,
            "authMethod": "",
            "loginFailed": str(login_failed),
            "userAgent": user_agent[:500],
            "psychometricTyped": "",
            "psychometricImage": "",
            "event_type": event_type,
            "userEmail": user_email,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compute_hash(self, message_json: str) -> str:
        """Compute sha256(message_json + key_b64 + iv_b64) as in the PHP reference."""
        settings = get_settings()
        raw = message_json + settings.gk_key + settings.gk_iv
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _resolve_reverse(self, client_ip: str) -> str:
        """Perform a synchronous reverse-DNS lookup, or return empty string."""
        if not get_settings().gk_reverse_dns or not client_ip:
            return ""
        try:
            import socket
            return socket.gethostbyaddr(client_ip)[0]
        except Exception:
            return ""

    async def _resolve_reverse_async(self, client_ip: str) -> str:
        """Run reverse DNS in a thread to avoid blocking the event loop."""
        if not get_settings().gk_reverse_dns:
            return ""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._resolve_reverse, client_ip)

    async def check_access(
        self,
        username: str,
        user_email: str,
        client_ip: str,
        user_agent: str,
        login_failed: int = 0,
        event_type: str = "Authentication",
    ) -> GKResponse:
        """Send an auth event to GuardianKey and return the risk decision.

        Always fail-open: any exception returns ``GKResponse(response="ERROR")``.
        """
        settings = get_settings()
        if not settings.gk_enabled:
            return GKResponse(response="ALLOW")

        try:
            client_reverse = await self._resolve_reverse_async(client_ip)
            message_json = self._build_message(
                username, user_email, client_ip, user_agent,
                client_reverse, login_failed, event_type,
            )
            hash_val = self._compute_hash(message_json)
            body = {
                "id": settings.gk_authgroup_id,
                "message": message_json,
                "hash": hash_val,
            }
            url = f"{settings.gk_api_url.rstrip('/')}/v2/checkaccess"

            logger.debug(
                "GuardianKey: POST %s authgroup=%s message_len=%d",
                url, settings.gk_authgroup_id, len(message_json),
            )
            async with httpx.AsyncClient(timeout=settings.gk_timeout_seconds) as client:
                resp = await client.post(url, json=body)
                if resp.status_code >= 400:
                    logger.warning(
                        "GuardianKey: HTTP %d from %s body=%s",
                        resp.status_code, url, resp.text[:500],
                    )
                resp.raise_for_status()
                raw_body = resp.text
                data = resp.json()

            response_str = str(data.get("response", "ERROR"))
            if response_str == "ERROR":
                # API responded 200 OK but returned ERROR — usually a hash
                # mismatch, unknown authgroup/org, or a malformed message.
                logger.warning(
                    "GuardianKey: API returned ERROR — raw=%s "
                    "(check GK_KEY/GK_IV/GK_AUTHGROUP_ID/GK_ORG_ID and message format)",
                    raw_body[:500],
                )
                logger.debug(
                    "GuardianKey: ERROR sent message=%s hash=%s authgroupId=%s",
                    message_json[:1000], hash_val, settings.gk_authgroup_id,
                )
            return GKResponse(
                response=response_str,
                risk=float(data.get("risk", 0.0)),
            )

        except Exception as exc:
            logger.warning(
                "GuardianKey check_access failed (fail-open): %r", exc,
            )
            return GKResponse(response="ERROR", risk=0.0)

    async def notify_event(
        self,
        username: str,
        user_email: str,
        client_ip: str,
        user_agent: str,
        login_failed: int = 1,
        event_type: str = "Authentication",
    ) -> None:
        """Fire-and-forget notification for failed login attempts.

        Sends the event to GuardianKey but ignores the response. Used to train
        the risk model without blocking the login flow.
        """
        settings = get_settings()
        if not settings.gk_enabled:
            return

        try:
            client_reverse = await self._resolve_reverse_async(client_ip)
            message_json = self._build_message(
                username, user_email, client_ip, user_agent,
                client_reverse, login_failed, event_type,
            )
            hash_val = self._compute_hash(message_json)
            body = {
                "id": settings.gk_authgroup_id,
                "message": message_json,
                "hash": hash_val,
            }
            url = f"{settings.gk_api_url.rstrip('/')}/v2/checkaccess"

            logger.debug(
                "GuardianKey: POST %s (notify) authgroup=%s",
                url, settings.gk_authgroup_id,
            )
            async with httpx.AsyncClient(timeout=settings.gk_timeout_seconds) as client:
                resp = await client.post(url, json=body)
                if resp.status_code >= 400:
                    logger.warning(
                        "GuardianKey notify: HTTP %d from %s body=%s",
                        resp.status_code, url, resp.text[:500],
                    )

        except Exception as exc:
            logger.warning("GuardianKey notify_event failed: %r", exc)
