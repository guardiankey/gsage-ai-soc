"""gSage AI — Agno post-hook: tenant session projection.

After each Agno agent run, this hook is called in-process to write data into
the gsage_* tables (gsage_tenant_sessions and gsage_agent_runs) so
that multi-tenant isolation queries work independently of the Agno DB schema.

Design principles (from docs/architecture/03-MODELO-MULTI-TENANT.md):
- Use Agno natively for history/memory.
- Project into gsage_* for multi-tenant isolation and billing.
- Never raise from a post-hook — log and swallow all exceptions.
- Idempotent: upsert by agno_session_id / agno_run_id.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select

import logging

from src.shared.database import _get_session_maker
from src.shared.models.agent_run import GSageAgentRun
from src.shared.models.tenant_session import GSageTenantSession
from src.backend_api.app.services.elasticsearch import get_tracer

if TYPE_CHECKING:
    from agno.run.agent import RunOutput as RunResponse
    from agno.session.agent import AgentSession

log = logging.getLogger(__name__)


async def persist_agno_run_projection(
    run_output: "RunResponse",
    session: "AgentSession | None" = None,
    metadata: Optional[dict] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> None:
    """Agno post-hook — project run into gsage_tenant_sessions / gsage_agent_runs.

    Agno calls post-hooks **after** the run completes successfully.  The
    function signature is type-checked by Agno — only declared params are
    passed (see ``execute_post_hooks``).

    Args:
        run_output: Completed run result from Agno.
        session:    Agno session object (may be ``None`` on first run).
        metadata:   Run metadata dict; expected to contain ``"organization_id"``.
        user_id:    User ID string injected by Agno from ``agent.user_id``.
    """
    try:
        org_id_str = (metadata or {}).get("organization_id")
        if not org_id_str:
            log.warning("persist_agno_run_projection: missing organization_id in metadata")
            return

        try:
            org_id = uuid.UUID(org_id_str)
        except ValueError:
            log.error("persist_agno_run_projection: invalid organization_id=%s", org_id_str)
            return

        # Resolve Agno session ID from run_output or agno session
        agno_session_id: Optional[str] = getattr(run_output, "session_id", None)
        if not agno_session_id and session is not None:
            agno_session_id = getattr(session, "session_id", None)
        if not agno_session_id:
            log.warning("persist_agno_run_projection: no session_id in run_output or session")
            return

        agno_run_id: Optional[str] = getattr(run_output, "run_id", None)

        # Resolve user UUID (nullable — API-key initiated sessions have no user)
        user_uuid: Optional[uuid.UUID] = None
        if user_id:
            try:
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                pass

        session_maker = _get_session_maker()
        async with session_maker() as db:
            # ------------------------------------------------------------------
            # 1. UPSERT gsage_tenant_sessions by agno_session_id
            # ------------------------------------------------------------------
            result = await db.execute(
                select(GSageTenantSession).where(
                    GSageTenantSession.agno_session_id == agno_session_id
                )
            )
            tenant_session = result.scalar_one_or_none()

            if tenant_session is None:
                # Infer source from the agno_session_id scope so that
                # channel_sender routes the response to the correct channel.
                #
                # Tenant-scoped format: "org_<uuid>:<scope>:<identifier>"
                # Legacy format (no prefix): "<scope>_<identifier>"
                _src = "web"
                _scope_part = agno_session_id
                if agno_session_id.startswith("org_") and ":" in agno_session_id:
                    # Extract the middle segment (scope) between the first and last ':'
                    parts = agno_session_id.split(":")
                    if len(parts) >= 3:
                        _scope_part = parts[1]
                if _scope_part.startswith("telegram"):
                    _src = "telegram"
                elif _scope_part.startswith("email"):
                    _src = "email"
                elif _scope_part.startswith("sched"):
                    _src = "scheduled"
                tenant_session = GSageTenantSession(
                    org_id=org_id,
                    user_id=user_uuid,
                    agno_session_id=agno_session_id,
                    source=_src,
                )
                db.add(tenant_session)
                await db.flush()  # populate tenant_session.id
                log.debug(
                    "Created gsage_tenant_session agno_session_id=%s org_id=%s",
                    agno_session_id,
                    org_id,
                )
            else:
                # Sanity check — ensure session belongs to same org
                if tenant_session.org_id != org_id:
                    log.error(
                        "org_id mismatch for agno_session_id=%s: expected=%s got=%s",
                        agno_session_id,
                        tenant_session.org_id,
                        org_id,
                    )
                    return

            # ------------------------------------------------------------------
            # 2. INSERT gsage_agent_runs — deduplicate by agno_run_id
            # ------------------------------------------------------------------
            if agno_run_id:
                run_result = await db.execute(
                    select(GSageAgentRun).where(
                        GSageAgentRun.agno_run_id == agno_run_id
                    )
                )
                if run_result.scalar_one_or_none() is None:
                    metrics = getattr(run_output, "metrics", None)

                    input_tokens: Optional[int] = None
                    output_tokens: Optional[int] = None
                    duration_ms: Optional[int] = None

                    if metrics is not None:
                        input_tokens = getattr(metrics, "input_tokens", None) or getattr(
                            metrics, "prompt_tokens", None
                        )
                        output_tokens = getattr(metrics, "output_tokens", None) or getattr(
                            metrics, "completion_tokens", None
                        )
                        duration_sec = getattr(metrics, "duration", None)
                        if duration_sec is not None:
                            duration_ms = int(duration_sec * 1000)

                    agent_run = GSageAgentRun(
                        org_id=org_id,
                        session_id=tenant_session.id,
                        agno_run_id=agno_run_id,
                        agent_type="maker",
                        status="completed",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        duration_ms=duration_ms,
                    )
                    db.add(agent_run)
                    log.debug(
                        "Inserted gsage_agent_run run_id=%s session=%s",
                        agno_run_id,
                        tenant_session.id,
                    )

            await db.commit()

        # ------------------------------------------------------------------
        # 3. Fire-and-forget ES trace
        # ------------------------------------------------------------------
        input_text: Optional[str] = None
        output_text: Optional[str] = None
        tools_invoked: list[str] = []
        try:
            # Extract input / output / tool names from run messages
            messages = getattr(run_output, "messages", None) or []
            for msg in messages:
                role = getattr(msg, "role", None)
                content = getattr(msg, "content", None)
                if role == "user" and input_text is None:
                    input_text = str(content) if content else None
                elif role in ("assistant", "model") and output_text is None:
                    output_text = str(content) if content else None
                elif role == "tool":
                    tool_name = getattr(msg, "tool_name", None) or getattr(msg, "name", None)
                    if tool_name and tool_name not in tools_invoked:
                        tools_invoked.append(str(tool_name))
        except Exception:
            pass

        agent_id_meta = (metadata or {}).get("agent_id", "unknown")
        model_meta = (metadata or {}).get("model")
        agent_type_meta = (metadata or {}).get("agent_type", "maker")
        metrics = getattr(run_output, "metrics", None)
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        token_count: Optional[int] = None
        duration_ms: Optional[int] = None
        if metrics is not None:
            in_t = getattr(metrics, "input_tokens", None)
            out_t = getattr(metrics, "output_tokens", None)
            if in_t is not None:
                input_tokens = int(in_t)
            if out_t is not None:
                output_tokens = int(out_t)
            if input_tokens is not None and output_tokens is not None:
                token_count = input_tokens + output_tokens
            dur = getattr(metrics, "duration", None)
            if dur is not None:
                duration_ms = int(dur * 1000)

        await get_tracer().trace_run(
            org_id=org_id_str,
            user_id=user_id,
            session_id=agno_session_id,
            agent_id=agent_id_meta,
            run_id=agno_run_id,
            input_text=input_text,
            output_text=output_text,
            model=model_meta,
            status="completed",
            duration_ms=duration_ms,
            token_count=token_count,
        )

        # ------------------------------------------------------------------
        # 4. Structured trace → agent-runs-* index (dashboards/monitoring)
        # ------------------------------------------------------------------
        try:
            import asyncio as _aio
            from src.shared.elasticsearch.sync_writer import index_trace as _sync_index

            source = (metadata or {}).get("source", "chat")
            interface = (metadata or {}).get("interface")
            elapsed_seconds: Optional[float] = (duration_ms / 1000) if duration_ms is not None else None

            agent_run_doc: dict = {
                "trace_id": agno_run_id or "",
                "org_id": org_id_str,
                "user_id": user_id,
                "conversation_id": agno_session_id,
                "agent_type": agent_type_meta,
                "source": source,
                "interface": interface,
                "status": "completed",
                "has_error": False,
                "total_duration_ms": duration_ms,
                "elapsed_seconds": elapsed_seconds,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": token_count,
                "tools_invoked": tools_invoked if tools_invoked else None,
                "tools_count": len(tools_invoked),
                "input_length": len(input_text) if input_text else None,
                "output_length": len(output_text) if output_text else None,
                "maker_model": model_meta,
            }
            await _aio.to_thread(_sync_index, "agent-runs", agent_run_doc)
        except Exception:
            log.debug("Failed to write agent-runs trace", exc_info=True)

    except Exception:
        # Post-hooks MUST NOT raise — a failed projection never fails the run
        log.error("persist_agno_run_projection failed", exc_info=True)
