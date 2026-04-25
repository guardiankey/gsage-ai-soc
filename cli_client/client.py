"""HTTP client for gSage AI API."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from cli_client.config import Config

logger = logging.getLogger(__name__)


class APIError(Exception):
    """API request failed."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


class NotAuthenticatedError(Exception):
    """No valid authentication configured."""


class GSageAPIClient:
    """HTTP client for gSage AI REST API (v1).

    Authentication:
    - API key:  ``Authorization: Bearer gk_live_...`` or ``gk_test_...``
    - JWT:      ``Authorization: Bearer <jwt>`` (call :meth:`login` first)
    """

    def __init__(self, config: Config):
        self.config = config
        self.org_id: str | None = config.org_id
        self.dept_id: str | None = config.dept_id
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._device_token: str | None = None
        self.permissions: list[str] = []

        self.client = httpx.Client(
            base_url=config.api_host,
            timeout=120.0,
            event_hooks={"request": [self._inject_dept_header]},
        )

        # Immediately configure auth if API key was provided
        if config.api_key:
            self._apply_bearer(config.api_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_bearer(self, token: str) -> None:
        """Set/replace the Authorization: Bearer header on the shared client."""
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    def _inject_dept_header(self, request: httpx.Request) -> None:
        """Event hook: inject X-Department-Id on every outgoing request when dept_id is set."""
        if self.dept_id:
            request.headers["X-Department-Id"] = self.dept_id
        elif "X-Department-Id" in request.headers:
            del request.headers["X-Department-Id"]

    def _org_prefix(self) -> str:
        """Return the URL prefix for org-scoped routes.

        Raises NotAuthenticatedError if org_id is not yet known.
        """
        if not self.org_id:
            raise NotAuthenticatedError(
                "Not authenticated. Please run 'login' first or set GSAGE_ORG_ID."
            )
        return f"/api/v1/orgs/{self.org_id}"

    def _dept_prefix(self) -> str:
        """Return the URL prefix for department-scoped routes.

        Raises NotAuthenticatedError if org_id or dept_id is not yet known.
        """
        org = self._org_prefix()
        if not self.dept_id:
            raise NotAuthenticatedError(
                "No department selected. Use 'dept set <slug>' or set GSAGE_DEPT_ID."
            )
        return f"{org}/depts/{self.dept_id}"

    def _parse_org_from_token(self, access_token: str) -> str | None:
        """Decode org_id from JWT claims without verifying the signature."""
        return self._parse_jwt_claims(access_token).get("org_id")

    def _parse_jwt_claims(self, access_token: str) -> dict:
        """Decode JWT payload without verifying the signature."""
        try:
            parts = access_token.split(".")
            if len(parts) < 2:
                return {}
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return {}

    def _store_tokens(self, data: dict[str, Any]) -> None:
        """Store JWT tokens and update the Authorization header + org_id."""
        access_token: str = data["access_token"]
        self._access_token = access_token
        self._refresh_token = data["refresh_token"]
        self._apply_bearer(access_token)
        claims = self._parse_jwt_claims(access_token)
        if claims.get("org_id"):
            self.org_id = claims["org_id"]
        self.permissions = claims.get("permissions", [])

    def _raise_error(self, response: httpx.Response) -> None:
        """Parse error response and raise APIError."""
        try:
            error_data = response.json()
            detail = error_data.get("detail", "Unknown error")
        except Exception:
            detail = response.text or "Unknown error"
        raise APIError(response.status_code, detail)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self) -> GSageAPIClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Auth routes — /api/v1/auth/...
    # ------------------------------------------------------------------

    def login(
        self,
        email: str,
        password: str,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate with email + password and store JWT tokens.

        When OTP is required for the account, the returned dict has
        ``otp_required=True`` and an ``otp_token`` field.  In that case tokens
        are **not** stored — the caller must call :meth:`verify_otp` next.

        Args:
            email: User email
            password: User password
            org_id: Optional org UUID string; selects org when user belongs to many

        Returns:
            TokenResponse dict. ``otp_required`` is True when a second step is needed.

        Raises:
            APIError: If credentials are invalid
        """
        payload: dict[str, Any] = {"email": email, "password": password}
        if org_id:
            payload["org_id"] = org_id

        headers: dict[str, str] = {}
        if self._device_token:
            headers["X-Device-Token"] = self._device_token

        response = self.client.post("/api/v1/auth/login", json=payload, headers=headers)
        if response.status_code != 200:
            self._raise_error(response)

        data = response.json()
        if not data.get("otp_required"):
            self._store_tokens(data)
        return data

    def register(
        self,
        email: str,
        password: str,
        full_name: str,
        org_name: str,
        org_slug: str | None = None,
    ) -> dict[str, Any]:
        """Register a new user and create their first organization.

        Args:
            email: New user email
            password: Password (min 8 chars)
            full_name: Display name
            org_name: Organization name
            org_slug: Optional URL slug (auto-derived from org_name if omitted)

        Returns:
            TokenResponse dict — user is already authenticated after this call

        Raises:
            APIError: If email/slug already taken or validation fails
        """
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "full_name": full_name,
            "org_name": org_name,
        }
        if org_slug:
            payload["org_slug"] = org_slug

        response = self.client.post("/api/v1/auth/register", json=payload)
        if response.status_code != 201:
            self._raise_error(response)

        data = response.json()
        self._store_tokens(data)
        return data

    def refresh_auth(self) -> None:
        """Exchange the stored refresh token for a new access+refresh token pair.

        Raises:
            APIError: If the refresh token is expired or invalid
            NotAuthenticatedError: If no refresh token is stored
        """
        if not self._refresh_token:
            raise NotAuthenticatedError("No refresh token available. Please login again.")

        response = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        if response.status_code != 200:
            self._raise_error(response)

        self._store_tokens(response.json())

    def get_me(self) -> dict[str, Any]:
        """Return current user info and org memberships.

        Also refreshes ``self.permissions`` from the membership matching
        ``self.org_id`` so the help view always reflects server-side state
        (useful when using API key auth or after role changes).

        Returns:
            MeResponse dict with id, email, full_name, is_active, memberships

        Raises:
            APIError: If not authenticated
        """
        response = self.client.get("/api/v1/auth/me")
        if response.status_code != 200:
            self._raise_error(response)
        data = response.json()
        # Refresh local permissions from the membership that matches the
        # active org.  Fall back to keeping whatever was already stored.
        for membership in data.get("memberships", []):
            if str(membership.get("org_id")) == str(self.org_id):
                self.permissions = membership.get("permissions", self.permissions)
                break
        return data

    def update_profile(self, full_name: str) -> dict[str, Any]:
        """Update the authenticated user's profile (currently: full_name).

        Args:
            full_name: New display name

        Returns:
            Updated MeResponse dict

        Raises:
            APIError: If the request fails
        """
        response = self.client.patch("/api/v1/auth/me", json={"full_name": full_name})
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def change_password(self, current_password: str, new_password: str) -> None:
        """Change the authenticated user's password.

        Args:
            current_password: Current password for verification
            new_password: New password (min 8 chars)

        Raises:
            APIError: If current password is wrong or account uses external auth
        """
        payload = {"current_password": current_password, "new_password": new_password}
        response = self.client.post("/api/v1/auth/me/change-password", json=payload)
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # ------------------------------------------------------------------
    # OTP (TOTP 2FA) routes — /api/v1/auth/otp/...
    # ------------------------------------------------------------------

    def verify_otp(
        self,
        otp_token: str,
        code: str | None = None,
        backup_code: str | None = None,
        remember_device: bool = False,
    ) -> dict[str, Any]:
        """Complete the OTP verification step after a partial login.

        Exactly one of *code* or *backup_code* must be provided.
        On success the full JWT tokens are stored automatically.

        Args:
            otp_token: Short-lived token returned by login when otp_required=True.
            code: 6-digit TOTP code from authenticator app.
            backup_code: One-time backup recovery code.
            remember_device: When True the response device_token is stored
                             and sent on future logins to skip OTP.

        Returns:
            TokenResponse dict with full access + refresh tokens.

        Raises:
            APIError: If the code is invalid or the otp_token has expired.
        """
        payload: dict[str, Any] = {"otp_token": otp_token}
        if code:
            payload["code"] = code
        if backup_code:
            payload["backup_code"] = backup_code
        if remember_device:
            payload["remember_device"] = True

        response = self.client.post("/api/v1/auth/otp/verify", json=payload)
        if response.status_code != 200:
            self._raise_error(response)

        data = response.json()
        self._store_tokens(data)
        if remember_device and data.get("device_token"):
            self._device_token = data["device_token"]
        return data

    def otp_status(self) -> dict[str, Any]:
        """Return the OTP enrollment status for the authenticated user.

        Returns:
            OTPStatusResponse dict: enabled, confirmed_at, backup_codes_remaining.

        Raises:
            APIError: If not authenticated.
        """
        response = self.client.get("/api/v1/auth/otp/status")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def otp_setup(self) -> dict[str, Any]:
        """Start TOTP enrollment — generates and returns a new secret + QR code.

        Returns:
            OTPSetupResponse dict: provisioning_uri, qr_base64, secret.

        Raises:
            APIError: If already enabled or not authenticated.
        """
        response = self.client.post("/api/v1/auth/otp/setup")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def otp_confirm(self, code: str) -> dict[str, Any]:
        """Confirm TOTP enrollment with the first generated code.

        Args:
            code: 6-digit code from the authenticator app.

        Returns:
            OTPConfirmResponse dict: backup_codes (list[str]).

        Raises:
            APIError: If the code is wrong or setup was not started.
        """
        response = self.client.post("/api/v1/auth/otp/confirm", json={"code": code})
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def otp_disable(
        self,
        password: str | None = None,
        otp_code: str | None = None,
    ) -> None:
        """Disable TOTP for the authenticated user.

        Requires either the account password or a valid OTP code.

        Args:
            password: Account password for verification.
            otp_code: Current 6-digit TOTP code (alternative to password).

        Raises:
            APIError: If verification fails or OTP is not enabled.
        """
        payload: dict[str, Any] = {}
        if password:
            payload["password"] = password
        if otp_code:
            payload["otp_code"] = otp_code

        response = self.client.request("DELETE", "/api/v1/auth/otp", json=payload)
        if response.status_code not in (200, 204):
            self._raise_error(response)

    def regenerate_backup_codes(
        self,
        password: str | None = None,
        otp_code: str | None = None,
    ) -> dict[str, Any]:
        """Generate a new set of backup codes, invalidating old ones.

        Args:
            password: Account password for verification.
            otp_code: Current 6-digit TOTP code (alternative to password).

        Returns:
            Dict with backup_codes: list[str] (10 new plaintext codes).

        Raises:
            APIError: If verification fails or OTP is not enabled.
        """
        payload: dict[str, Any] = {}
        if password:
            payload["password"] = password
        if otp_code:
            payload["otp_code"] = otp_code

        response = self.client.post("/api/v1/auth/otp/backup-codes/regenerate", json=payload)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Conversation routes — /api/v1/orgs/{org_id}/chat/conversations
    # ------------------------------------------------------------------

    def create_conversation(
        self,
        title: str | None = None,
        agent_id: str = "assistant",
    ) -> dict[str, Any]:
        """Create a new conversation.

        Args:
            title: Optional conversation title
            agent_id: Agent to attach (default: "assistant")

        Returns:
            ConversationOut dict with id, agno_session_id, title, is_active, ...

        Raises:
            APIError: If the request fails
            NotAuthenticatedError: If org_id is not set
        """
        payload: dict[str, Any] = {"agent_id": agent_id}
        if title:
            payload["title"] = title

        response = self.client.post(
            f"{self._org_prefix()}/chat/conversations",
            json=payload,
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Get conversation detail.

        Args:
            conversation_id: Conversation UUID string

        Returns:
            ConversationOut dict

        Raises:
            APIError: If not found or request fails
        """
        response = self.client.get(
            f"{self._org_prefix()}/chat/conversations/{conversation_id}"
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def list_conversations(
        self,
        page: int = 1,
        limit: int = 20,
        active_only: bool = True,
    ) -> dict[str, Any]:
        """List conversations for the authenticated user in the current org.

        Args:
            page: Page number (1-based)
            limit: Maximum number of conversations per page (1–100)
            active_only: When True (default) only return active conversations

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more

        Raises:
            APIError: If the request fails
        """
        params: dict[str, Any] = {
            "page": page,
            "limit": limit,
            "active": active_only,
        }
        response = self.client.get(
            f"{self._org_prefix()}/chat/conversations",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def archive_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Archive (soft-deactivate) a conversation.

        Args:
            conversation_id: Conversation UUID string

        Returns:
            Updated ConversationOut dict

        Raises:
            APIError: If not found or request fails
        """
        response = self.client.patch(
            f"{self._org_prefix()}/chat/conversations/{conversation_id}",
            json={"is_active": False},
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def send_message(
        self,
        conversation_id: str,
        message: str,
        output_format: str = "markdown",
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send a message to a conversation and get the synchronous agent reply.

        Args:
            conversation_id: Conversation UUID string
            message: User message text
            output_format: Unused by the backend — kept for CLI compat
            attachment_ids: Optional list of file attachment UUIDs

        Returns:
            SendMessageResponse dict with content, metadata, ...

        Raises:
            APIError: If the request fails
        """
        payload: dict[str, Any] = {"message": message, "attachment_ids": attachment_ids or []}

        response = self.client.post(
            f"{self._org_prefix()}/chat/conversations/{conversation_id}/messages",
            json=payload,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def stream_message(
        self,
        conversation_id: str,
        message: str,
        attachment_ids: list[str] | None = None,
    ):
        """Stream a message via SSE and yield (event_name, data) tuples.

        Yields:
            ('content_delta', {'delta': str})
            ('run_paused',    {'pending_approvals': list[str], 'run_id': str | None})
            ('message_end',   {'id': str, 'metadata': dict})
            ('error',         {'detail': str})

        Raises:
            APIError: If the server returns a non-200 status before streaming.
        """
        import json as _json  # noqa: PLC0415

        with self.client.stream(
            "POST",
            f"{self._org_prefix()}/chat/conversations/{conversation_id}/messages/stream",
            json={"message": message, "attachment_ids": attachment_ids or []},
            timeout=180.0,
        ) as resp:
            if resp.status_code != 200:
                resp.read()  # consume body so we can parse the error
                self._raise_error(resp)

            event_name: str = ""
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line:
                    event_name = ""
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    raw_data = line[5:].strip()
                    try:
                        data = _json.loads(raw_data)
                    except _json.JSONDecodeError:
                        data = {"delta": raw_data}
                    yield event_name, data

    def upload_attachment(
        self,
        conversation_id: str,
        filename: str,
        data: bytes,
        content_type: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file attachment for a chat conversation.

        Returns:
            dict with id, filename, content_type, size_bytes

        Raises:
            APIError: If the upload fails
        """
        import io  # noqa: PLC0415

        files = {"file": (filename, io.BytesIO(data), content_type)}
        params: dict[str, Any] = {}
        if description:
            params["description"] = description
        response = self.client.post(
            f"{self._org_prefix()}/chat/conversations/{conversation_id}/attachments",
            files=files,
            params=params,
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def list_messages(
        self,
        conversation_id: str,
        last_n: int | None = None,
    ) -> list[dict[str, Any]]:
        """List messages in a conversation (from Agno session history).

        Args:
            conversation_id: Conversation UUID string
            last_n: Return only the last N messages (None = all)

        Returns:
            List of MessageOut dicts with role, content, ...

        Raises:
            APIError: If the request fails
        """
        params: dict[str, Any] = {}
        if last_n is not None:
            params["last_n"] = last_n

        response = self.client.get(
            f"{self._org_prefix()}/chat/conversations/{conversation_id}/messages",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()  # list[MessageOut]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Liveness probe — no auth required."""
        response = self.client.get("/api/v1/health")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Approvals (HITL) — /api/v1/orgs/{org_id}/approvals
    # ------------------------------------------------------------------

    def list_approvals(
        self,
        approval_status: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List approvals for the current org/user.

        Args:
            approval_status: Filter by status ("pending", "approved", "rejected")
            page: Page number (1-based)
            limit: Max results per page (1–100)

        Returns:
            ApprovalListResponse dict with items, total, page, limit

        Raises:
            APIError: If the request fails
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if approval_status:
            params["status"] = approval_status

        response = self.client.get(
            f"{self._org_prefix()}/approvals",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        """Get a single approval by ID.

        Args:
            approval_id: Approval UUID string

        Returns:
            ApprovalOut dict

        Raises:
            APIError: If not found or request fails
        """
        response = self.client.get(
            f"{self._org_prefix()}/approvals/{approval_id}"
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def resolve_approval(
        self,
        approval_id: str,
        action: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject a pending approval.

        Args:
            approval_id: Approval UUID string
            action: "approve" or "reject"
            comment: Optional comment / reason

        Returns:
            Updated ApprovalOut dict

        Raises:
            APIError: If not found, not pending, or access denied
        """
        payload: dict[str, Any] = {"action": action}
        if comment:
            payload["comment"] = comment

        response = self.client.post(
            f"{self._org_prefix()}/approvals/{approval_id}/resolve",
            json=payload,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def continue_run_from_approval(self, approval_id: str) -> dict[str, Any]:
        """Continue the agent run associated with an approved approval.

        Calls POST /orgs/{org_id}/approvals/{approval_id}/continue-run which
        resumes the paused Agno run and returns a SendMessageResponse.

        Args:
            approval_id: Approval UUID string (must already be approved)

        Returns:
            SendMessageResponse dict with ``content``, ``status``, etc.

        Raises:
            APIError: If not approved, session not found, or agent error
        """
        response = self.client.post(
            f"{self._org_prefix()}/approvals/{approval_id}/continue-run",
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Knowledge base — /api/v1/orgs/{org_id}/knowledge
    # ------------------------------------------------------------------

    def search_knowledge(
        self,
        query: str,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Semantic search over the tenant knowledge base.

        Args:
            query: Natural language search query
            max_results: Maximum number of results to return (default: 5)

        Returns:
            KnowledgeSearchResponse dict with results list and total count
        """
        response = self.client.post(
            f"{self._org_prefix()}/knowledge/search",
            json={"query": query, "max_results": max_results},
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def list_knowledge(
        self,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List stored documents in the tenant knowledge base.

        Args:
            page: Page number (1-based)
            limit: Max results per page (1–100)

        Returns:
            KnowledgeContentListResponse dict with items, total, page, limit
        """
        response = self.client.get(
            f"{self._org_prefix()}/knowledge/content",
            params={"page": page, "limit": limit},
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def add_knowledge(
        self,
        name: str,
        content: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Add a text document to the tenant knowledge base.

        Args:
            name: Document name
            content: Document text content (required unless url is provided)
            description: Optional description
            metadata: Optional extra metadata dict
            url: Optional URL to fetch content from (used when content is not provided)

        Returns:
            KnowledgeContentOut dict with id, name, type, status, ...
        """
        payload: dict[str, Any] = {"name": name}
        if content is not None:
            payload["content"] = content
        if description is not None:
            payload["description"] = description
        if metadata is not None:
            payload["metadata"] = metadata
        if url is not None:
            payload["url"] = url

        response = self.client.post(
            f"{self._org_prefix()}/knowledge/content",
            json=payload,
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def delete_knowledge(self, content_id: str) -> None:
        """Delete a document from the tenant knowledge base.

        Args:
            content_id: Document UUID string

        Raises:
            APIError: If not found or access denied
        """
        response = self.client.delete(
            f"{self._org_prefix()}/knowledge/content/{content_id}",
        )
        if response.status_code != 204:
            self._raise_error(response)

    def ingest_document(
        self,
        filepath: str,
        scope: str = "org",
    ) -> dict[str, Any]:
        """Upload a file for async ingestion into the knowledge base.

        The file is sent as multipart/form-data.  The backend stores it,
        dispatches a Celery task, and returns a job_id for polling.

        Args:
            filepath: Absolute or relative path to the file to upload
            scope: "org" (default), "user", or "dept" (requires active department context)

        Returns:
            IngestJobSubmitResponse dict with job_id, status, filename, scope
        """
        path = Path(filepath)
        with path.open("rb") as fh:
            response = self.client.post(
                f"{self._org_prefix()}/knowledge/ingest",
                files={"file": (path.name, fh, "application/octet-stream")},
                data={"scope": scope},
            )
        if response.status_code != 202:
            self._raise_error(response)
        return response.json()

    def get_ingest_status(self, job_id: str) -> dict[str, Any]:
        """Get the status of a document ingest job.

        Args:
            job_id: Ingest job UUID returned by ingest_document()

        Returns:
            IngestJobStatusResponse dict with job_id, status, filename,
            scope, file_size, chunks_stored, error_message, ...
        """
        response = self.client.get(
            f"{self._org_prefix()}/knowledge/ingest/{job_id}",
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Files — /api/v1/orgs/{org_id}/files
    # ------------------------------------------------------------------

    def list_files(
        self,
        page: int = 1,
        limit: int = 20,
        tool_name: str | None = None,
        include_purged: bool = False,
        category: str | None = None,
    ) -> dict[str, Any]:
        """List tool-generated files for the current org/user.

        Args:
            page: Page number (1-based)
            limit: Max results per page (1–100)
            tool_name: Optional filter by tool name
            include_purged: Include files whose bytes have been purged
            category: Optional filter: "generated" | "template"

        Returns:
            FileListResponse dict with items, total, page, limit
        """
        params: dict[str, Any] = {
            "page": page,
            "limit": limit,
            "include_purged": include_purged,
        }
        if tool_name:
            params["tool_name"] = tool_name
        if category:
            params["category"] = category

        response = self.client.get(
            f"{self._org_prefix()}/files",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def download_file(self, file_id: str, dest_path: str | None = None) -> dict[str, Any]:
        """Download a tool-generated file to *dest_path* on the local filesystem.

        Streams file bytes directly from the authenticated API proxy endpoint —
        MinIO does not need to be externally reachable.

        Args:
            file_id: File UUID string
            dest_path: Local file path or directory to write (created/overwritten).
                       Defaults to ./gsage_ai_downloads/<filename>.

        Returns:
            dict with filename, size_bytes, dest_path

        Raises:
            APIError: If the file is not found, purged, or access is denied
        """
        url = f"{self._org_prefix()}/files/{file_id}/download"
        with self.client.stream("GET", url, timeout=120.0) as resp:
            if resp.status_code != 200:
                self._raise_error(resp)
            # Extract filename from Content-Disposition: attachment; filename="..."
            cd = resp.headers.get("content-disposition", "")
            filename = file_id  # fallback: use file_id as filename
            if 'filename="' in cd:
                filename = cd.split('filename="', 1)[1].rstrip('"')
            if dest_path is None:
                download_dir = Path("gsage_ai_downloads")
                download_dir.mkdir(parents=True, exist_ok=True)
                local_path = str(download_dir / filename)
            elif Path(dest_path).is_dir():
                local_path = str(Path(dest_path) / filename)
            else:
                local_path = dest_path
            size = 0
            with open(local_path, "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)
                    size += len(chunk)

        return {"filename": filename, "size_bytes": size, "dest_path": local_path}

    def upload_file(
        self,
        file_path: str,
        description: str | None = None,
        scope: str = "user",
    ) -> dict[str, Any]:
        """Upload a document template to the templates bucket.

        Args:
            file_path: Local path of the file to upload.
            description: Optional human-readable description.
            scope: "user" (private), "org" / "organization" (visible to all org
                   members), or "dept" / "department" (visible to dept members).

        Returns:
            FileOut dict for the newly created template record.

        Raises:
            APIError: 403 (permission), 415 (bad extension), 413 (too large).
        """
        import pathlib
        import mimetypes

        path = pathlib.Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        # Normalise short aliases to canonical backend values
        _scope_map = {"org": "organization", "dept": "department"}
        api_scope = _scope_map.get(scope, scope)

        params: dict[str, Any] = {"scope": api_scope}
        if description:
            params["description"] = description

        with open(path, "rb") as fh:
            response = self.client.post(
                f"{self._org_prefix()}/files/upload",
                params=params,
                files={"file": (path.name, fh, mime_type)},
            )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def delete_file(self, file_id: str) -> None:
        """Delete a template file by ID.

        Args:
            file_id: File UUID string.

        Raises:
            APIError: 404 (not found), 403 (forbidden), 409 (not a template).
        """
        response = self.client.delete(
            f"{self._org_prefix()}/files/{file_id}",
        )
        if response.status_code != 204:
            self._raise_error(response)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def list_background_tasks(
        self,
        page: int = 1,
        limit: int = 20,
        tool_name: str | None = None,
        task_status: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """List background tool execution tasks.

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if tool_name:
            params["tool_name"] = tool_name
        if task_status:
            params["status"] = task_status
        if session_id:
            params["session_id"] = session_id
        response = self.client.get(
            f"{self._org_prefix()}/background-tasks",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def list_api_keys(
        self,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List API keys for the current org (admin only).

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        response = self.client.get(
            f"{self._org_prefix()}/api-keys",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Scheduled Jobs — /api/v1/orgs/{org_id}/scheduled-jobs
    # ------------------------------------------------------------------

    def list_scheduled_jobs(
        self,
        page: int = 1,
        limit: int = 20,
        job_type: str | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """List scheduled jobs for the current org.

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if job_type:
            params["job_type"] = job_type
        if is_active is not None:
            params["is_active"] = is_active
        response = self.client.get(
            f"{self._org_prefix()}/scheduled-jobs",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_scheduled_job(self, job_id: str) -> dict[str, Any]:
        """Get a scheduled job by ID."""
        response = self.client.get(f"{self._org_prefix()}/scheduled-jobs/{job_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def create_scheduled_job(
        self,
        name: str,
        job_type: str,
        cron_expression: str,
        timezone: str = "UTC",
        prompt_content: str | None = None,
        description: str | None = None,
        max_runs: int | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        """Create a new scheduled job.

        Returns:
            ScheduledJobOut dict
        """
        payload: dict[str, Any] = {
            "name": name,
            "job_type": job_type,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "is_active": is_active,
        }
        if description:
            payload["description"] = description
        if prompt_content:
            payload["prompt_content"] = prompt_content
        if max_runs is not None:
            payload["max_runs"] = max_runs
        response = self.client.post(
            f"{self._org_prefix()}/scheduled-jobs",
            json=payload,
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def update_scheduled_job(
        self,
        job_id: str,
        name: str | None = None,
        description: str | None = None,
        cron_expression: str | None = None,
        timezone: str | None = None,
        prompt_content: str | None = None,
        max_runs: int | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """Update fields of a scheduled job (PATCH — only sends provided fields).

        Args:
            job_id: Scheduled job UUID string
            name: New name
            description: New description
            cron_expression: New 5-field cron expression
            timezone: New IANA timezone string
            prompt_content: New prompt text (PROMPT_RUN jobs only)
            max_runs: New max_runs limit (None = unlimited)
            is_active: Toggle active state

        Returns:
            Updated ScheduledJobOut dict

        Raises:
            APIError: If not found or validation fails
        """
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if cron_expression is not None:
            payload["cron_expression"] = cron_expression
        if timezone is not None:
            payload["timezone"] = timezone
        if prompt_content is not None:
            payload["prompt_content"] = prompt_content
        if max_runs is not None:
            payload["max_runs"] = max_runs
        if is_active is not None:
            payload["is_active"] = is_active

        response = self.client.patch(
            f"{self._org_prefix()}/scheduled-jobs/{job_id}",
            json=payload,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def activate_scheduled_job(self, job_id: str) -> dict[str, Any]:
        """Activate a scheduled job."""
        response = self.client.post(f"{self._org_prefix()}/scheduled-jobs/{job_id}/activate")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def deactivate_scheduled_job(self, job_id: str) -> dict[str, Any]:
        """Deactivate a scheduled job."""
        response = self.client.post(f"{self._org_prefix()}/scheduled-jobs/{job_id}/deactivate")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def delete_scheduled_job(self, job_id: str) -> None:
        """Delete a scheduled job permanently."""
        response = self.client.delete(f"{self._org_prefix()}/scheduled-jobs/{job_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # ------------------------------------------------------------------
    # Org members — /api/v1/orgs/{org_id}/members
    # ------------------------------------------------------------------

    def list_org_members(self) -> list[dict[str, Any]]:
        """List active members of the current org (used for approver selection).

        Returns:
            List of OrgMemberOut dicts: user_id, full_name, email, role
        """
        response = self.client.get(f"{self._org_prefix()}/members")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Approval Rules — /api/v1/orgs/{org_id}/approval-rules
    # ------------------------------------------------------------------

    def list_approval_rules(
        self,
        page: int = 1,
        limit: int = 20,
        is_active: bool | None = None,
        tool_pattern: str | None = None,
    ) -> dict[str, Any]:
        """List approval rules for the current org.

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if is_active is not None:
            params["is_active"] = is_active
        if tool_pattern:
            params["tool_pattern"] = tool_pattern
        response = self.client.get(
            f"{self._org_prefix()}/approval-rules",
            params=params,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_approval_rule(self, rule_id: str) -> dict[str, Any]:
        """Get an approval rule by ID."""
        response = self.client.get(f"{self._org_prefix()}/approval-rules/{rule_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def create_approval_rule(
        self,
        tool_pattern: str,
        approver_user_id: str,
        user_id_pattern: str = "*",
        dept_id_pattern: str = "*",
        is_active: bool = True,
        priority: int = 0,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a new approval rule.

        Returns:
            ApprovalRuleOut dict
        """
        payload: dict[str, Any] = {
            "tool_pattern": tool_pattern,
            "approver_user_id": approver_user_id,
            "user_id_pattern": user_id_pattern,
            "dept_id_pattern": dept_id_pattern,
            "is_active": is_active,
            "priority": priority,
        }
        if description:
            payload["description"] = description
        response = self.client.post(
            f"{self._org_prefix()}/approval-rules",
            json=payload,
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def update_approval_rule(
        self,
        rule_id: str,
        tool_pattern: str | None = None,
        user_id_pattern: str | None = None,
        dept_id_pattern: str | None = None,
        approver_user_id: str | None = None,
        is_active: bool | None = None,
        priority: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update an approval rule (only send provided fields)."""
        payload: dict[str, Any] = {}
        if tool_pattern is not None:
            payload["tool_pattern"] = tool_pattern
        if user_id_pattern is not None:
            payload["user_id_pattern"] = user_id_pattern
        if dept_id_pattern is not None:
            payload["dept_id_pattern"] = dept_id_pattern
        if approver_user_id is not None:
            payload["approver_user_id"] = approver_user_id
        if is_active is not None:
            payload["is_active"] = is_active
        if priority is not None:
            payload["priority"] = priority
        if description is not None:
            payload["description"] = description
        response = self.client.patch(
            f"{self._org_prefix()}/approval-rules/{rule_id}",
            json=payload,
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def activate_approval_rule(self, rule_id: str) -> dict[str, Any]:
        """Activate an approval rule."""
        response = self.client.post(f"{self._org_prefix()}/approval-rules/{rule_id}/activate")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def deactivate_approval_rule(self, rule_id: str) -> dict[str, Any]:
        """Deactivate an approval rule."""
        response = self.client.post(f"{self._org_prefix()}/approval-rules/{rule_id}/deactivate")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def delete_approval_rule(self, rule_id: str) -> None:
        """Delete an approval rule permanently."""
        response = self.client.delete(f"{self._org_prefix()}/approval-rules/{rule_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Departments — /api/v1/orgs/{org_id}/depts
    # ------------------------------------------------------------------

    def list_departments(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        """List departments in the current org."""
        params: dict[str, Any] = {}
        if include_inactive:
            params["include_inactive"] = "true"
        response = self.client.get(f"{self._org_prefix()}/depts/", params=params)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_department(self, dept_id: str) -> dict[str, Any]:
        """Get a department by ID."""
        response = self.client.get(f"{self._org_prefix()}/depts/{dept_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def my_departments(self) -> list[dict[str, Any]]:
        """Return the current user's departments in this org."""
        response = self.client.get(f"{self._org_prefix()}/depts/my")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # DataStores — /api/v1/orgs/{org_id}/depts/{dept_id}/datastores
    # ------------------------------------------------------------------

    def list_datastores(self, page: int = 1, limit: int = 20) -> dict[str, Any]:
        """List data stores for the current department.

        Returns:
            PaginatedResponse dict with items, total, page, limit, has_more
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        response = self.client.get(f"{self._dept_prefix()}/datastores", params=params)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_datastore(self, store_id: str) -> dict[str, Any]:
        """Get a data store by ID."""
        response = self.client.get(f"{self._dept_prefix()}/datastores/{store_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def create_datastore(
        self,
        name: str,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
        visibility: str = "shared",
        max_records: int | None = None,
    ) -> dict[str, Any]:
        """Create a new data store in the current department.

        Returns:
            DataStoreOut dict
        """
        payload: dict[str, Any] = {
            "name": name,
            "visibility": visibility,
        }
        if max_records is not None:
            payload["max_records"] = max_records
        if description:
            payload["description"] = description
        if schema is not None:
            payload["schema"] = schema
        response = self.client.post(f"{self._dept_prefix()}/datastores", json=payload)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def update_datastore(
        self,
        store_id: str,
        name: str | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
        visibility: str | None = None,
        max_records: int | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """Update a data store (only send provided fields)."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if schema is not None:
            payload["schema"] = schema
        if visibility is not None:
            payload["visibility"] = visibility
        if max_records is not None:
            payload["max_records"] = max_records
        if is_active is not None:
            payload["is_active"] = is_active
        response = self.client.patch(
            f"{self._dept_prefix()}/datastores/{store_id}", json=payload
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def delete_datastore(self, store_id: str) -> None:
        """Delete a data store and all its records permanently."""
        response = self.client.delete(f"{self._dept_prefix()}/datastores/{store_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # DataStore Records

    def list_datastore_records(
        self, store_id: str, page: int = 1, limit: int = 20
    ) -> dict[str, Any]:
        """List records in a data store."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        response = self.client.get(
            f"{self._dept_prefix()}/datastores/{store_id}/records", params=params
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def get_datastore_record(self, store_id: str, record_id: str) -> dict[str, Any]:
        """Get a single record from a data store."""
        response = self.client.get(
            f"{self._dept_prefix()}/datastores/{store_id}/records/{record_id}"
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def insert_datastore_record(
        self, store_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert a new record into a data store."""
        response = self.client.post(
            f"{self._dept_prefix()}/datastores/{store_id}/records", json={"data": data}
        )
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def update_datastore_record(
        self, store_id: str, record_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing record in a data store."""
        response = self.client.patch(
            f"{self._dept_prefix()}/datastores/{store_id}/records/{record_id}",
            json={"data": data},
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def delete_datastore_record(self, store_id: str, record_id: str) -> None:
        """Delete a record from a data store."""
        response = self.client.delete(
            f"{self._dept_prefix()}/datastores/{store_id}/records/{record_id}"
        )
        if response.status_code not in (200, 204):
            self._raise_error(response)

    def query_datastore_records(
        self,
        store_id: str,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Query records in a data store with optional filters."""
        payload: dict[str, Any] = {"page": page, "page_size": page_size}
        if filters:
            payload["filters"] = filters
        response = self.client.post(
            f"{self._dept_prefix()}/datastores/{store_id}/records/query", json=payload
        )
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # ------------------------------------------------------------------
    # Admin — /api/v1/orgs/{org_id}/admin/...
    # Requires the admin:access permission.
    # ------------------------------------------------------------------

    def _admin_prefix(self) -> str:
        return f"{self._org_prefix()}/admin"

    # --- Organization ---

    def admin_get_org(self) -> dict[str, Any]:
        """Get organization settings (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/organization")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_update_org(self, **fields: Any) -> dict[str, Any]:
        """Update organization settings (admin, only send provided fields)."""
        response = self.client.patch(f"{self._admin_prefix()}/organization", json=fields)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    # --- Users ---

    def admin_list_users(
        self, page: int = 1, limit: int = 20, search: str | None = None
    ) -> dict[str, Any]:
        """List organization members (admin)."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if search:
            params["search"] = search
        response = self.client.get(f"{self._admin_prefix()}/users", params=params)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_get_user(self, user_id: str) -> dict[str, Any]:
        """Get a user's org membership details (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/users/{user_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_create_user(self, email: str, full_name: str, role: str = "user", **extra: Any) -> dict[str, Any]:
        """Create a user and add them to the org (admin)."""
        payload: dict[str, Any] = {"email": email, "full_name": full_name, "role": role, **extra}
        response = self.client.post(f"{self._admin_prefix()}/users", json=payload)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def admin_update_user(self, user_id: str, **fields: Any) -> dict[str, Any]:
        """Update a user's org membership (admin)."""
        response = self.client.patch(f"{self._admin_prefix()}/users/{user_id}", json=fields)
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_reset_user_password(self, user_id: str) -> dict[str, Any]:
        """Reset a user's password and return a temporary password (admin)."""
        response = self.client.post(f"{self._admin_prefix()}/users/{user_id}/reset-password")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_reset_user_otp(self, user_id: str) -> None:
        """Disable OTP for a user (admin)."""
        response = self.client.post(f"{self._admin_prefix()}/users/{user_id}/reset-otp")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    def admin_remove_user(self, user_id: str) -> None:
        """Remove a user from the org (admin)."""
        response = self.client.delete(f"{self._admin_prefix()}/users/{user_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # --- Groups ---

    def admin_list_permissions(self) -> list[dict[str, Any]]:
        """List all available permissions (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/groups/permissions")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_list_groups(self) -> list[dict[str, Any]]:
        """List permission groups (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/groups")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_get_group(self, group_id: str) -> dict[str, Any]:
        """Get group details including members and permissions (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/groups/{group_id}")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_create_group(self, name: str, description: str | None = None) -> dict[str, Any]:
        """Create a permission group (admin)."""
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        response = self.client.post(f"{self._admin_prefix()}/groups", json=payload)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def admin_delete_group(self, group_id: str) -> None:
        """Delete a permission group (admin)."""
        response = self.client.delete(f"{self._admin_prefix()}/groups/{group_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # --- Tool Configs ---

    def admin_list_tool_configs(self) -> list[dict[str, Any]]:
        """List tool configurations (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/tool-configs")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_create_tool_config(
        self, tool_name: str, profile_id: str, config: dict[str, Any], description: str | None = None
    ) -> dict[str, Any]:
        """Create a tool configuration override (admin)."""
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "profile_id": profile_id,
            "config": config,
        }
        if description:
            payload["description"] = description
        response = self.client.post(f"{self._admin_prefix()}/tool-configs", json=payload)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def admin_delete_tool_config(self, config_id: str) -> None:
        """Delete a tool configuration (admin)."""
        response = self.client.delete(f"{self._admin_prefix()}/tool-configs/{config_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # --- Interface Profiles ---

    def admin_list_interfaces(self) -> list[dict[str, Any]]:
        """List interface profiles (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/interface-profiles")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_create_interface(
        self,
        interface: str,
        mode: str = "denylist",
        tool_permissions: list[str] | None = None,
        description: str | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        """Create an interface profile (admin)."""
        payload: dict[str, Any] = {
            "interface": interface,
            "mode": mode,
            "tool_permissions": tool_permissions or [],
            "is_active": is_active,
        }
        if description:
            payload["description"] = description
        response = self.client.post(f"{self._admin_prefix()}/interface-profiles", json=payload)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def admin_delete_interface(self, profile_id: str) -> None:
        """Delete an interface profile (admin)."""
        response = self.client.delete(f"{self._admin_prefix()}/interface-profiles/{profile_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    # --- Email Accounts ---

    def admin_list_email_accounts(self) -> list[dict[str, Any]]:
        """List email accounts (admin)."""
        response = self.client.get(f"{self._admin_prefix()}/email-accounts")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()

    def admin_create_email_account(self, **fields: Any) -> dict[str, Any]:
        """Create an email account (admin)."""
        response = self.client.post(f"{self._admin_prefix()}/email-accounts", json=fields)
        if response.status_code != 201:
            self._raise_error(response)
        return response.json()

    def admin_delete_email_account(self, account_id: str) -> None:
        """Delete an email account (admin)."""
        response = self.client.delete(f"{self._admin_prefix()}/email-accounts/{account_id}")
        if response.status_code not in (200, 204):
            self._raise_error(response)

    def admin_test_email_account(self, account_id: str) -> dict[str, Any]:
        """Test IMAP/SMTP connectivity for an email account (admin)."""
        response = self.client.post(f"{self._admin_prefix()}/email-accounts/{account_id}/test")
        if response.status_code != 200:
            self._raise_error(response)
        return response.json()
