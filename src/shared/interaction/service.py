"""gSage AI — InteractionService: tool-facing API for user interactions."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.interaction.broker import InteractionBroker
from src.shared.models.gsage_interaction import GSageInteraction
from src.shared.interaction.enums import InteractionStatus, ResumeMode
from src.shared.interaction.exceptions import InteractionReplanRequested
from src.shared.interaction.interactions import BaseInteraction, FormInteraction

logger = logging.getLogger(__name__)


class InteractionService:
    """Entry point for tools to request user interaction.

    Injected into :class:`BaseTool` via the ``interaction`` property.
    Does **not** depend on Redis directly — uses an :class:`InteractionBroker`.

    Lifecycle::

        # Inside Tool.execute()
        dados = await self.interaction.form(
            ClienteForm,
            title="Cadastro de Cliente",
            resume=ResumeMode.CONTINUE_TOOL,
            context={"ticket_id": "INC-1234"},
        )
        nome = dados["nome"]
    """

    def __init__(
        self,
        broker: InteractionBroker,
        session: AsyncSession,
        org_id: uuid.UUID,
        gsage_session_id: Optional[uuid.UUID],
        tool_name: str,
        execution_id: Optional[uuid.UUID] = None,
        tool_call_id: Optional[uuid.UUID] = None,
    ) -> None:
        self._broker = broker
        self._session = session
        self._org_id = org_id
        self._gsage_session_id = gsage_session_id
        self._tool_name = tool_name
        self._execution_id = execution_id
        self._tool_call_id = tool_call_id

    # ── Primary API ───────────────────────────────────────────────────────

    async def request(
        self,
        interaction: BaseInteraction,
        *,
        resume: ResumeMode = ResumeMode.CONTINUE_TOOL,
        context: Optional[dict] = None,
    ) -> dict:
        """Request a user interaction.

        This is the **primary** entry point.  ``form()`` is a convenience
        wrapper that builds a :class:`FormInteraction` automatically.

        Args:
            interaction: The interaction definition (e.g. ``FormInteraction``).
            resume:
                ``CONTINUE_TOOL`` — block until the user responds, then
                return the response ``dict`` so the tool can continue.
                ``REPLAN_AGENT`` — raise :exc:`InteractionReplanRequested`
                immediately.  The agent framework will later inject the
                user's responses as a ``[INTERACTION_RESPONSE]`` block so
                the agent can replan.
            context: Optional audit metadata — NOT shown to the user.
                Example: ``{"ticket_id": "INC-1234", "asset_id": "SRV-01"}``.
                Persisted to the ``gsage_interactions`` table.

        Returns:
            For ``CONTINUE_TOOL``: a ``dict`` of form responses keyed by
            field ID (e.g. ``{"nome": "João", "idade": 30}``).

        Raises:
            InteractionReplanRequested: When ``resume=REPLAN_AGENT``.
            InteractionTimeout: User did not respond within the timeout.
            InteractionCancelled: User explicitly cancelled.
        """
        interaction_id = uuid.uuid4()

        # 1. Persist interaction record
        schema = interaction.to_dict()
        db_row = GSageInteraction(
            id=interaction_id,
            org_id=self._org_id,
            gsage_session_id=self._gsage_session_id,
            tool_name=self._tool_name,
            interaction_type=interaction.interaction_type.value,
            title=interaction.title,
            description=interaction.description,
            schema_json=schema,
            status=InteractionStatus.WAITING_INPUT.value,
            resume_mode=resume.value,
            context_json=context,
            execution_id=self._execution_id,
            tool_call_id=self._tool_call_id,
        )
        self._session.add(db_row)
        await self._session.commit()

        # 2. Build SSE event payload
        event_payload: dict = {
            "interaction_id": str(interaction_id),
            "interaction_type": interaction.interaction_type.value,
            "title": interaction.title,
            "description": interaction.description,
            "schema": schema,
            "resume_mode": resume.value,
            "timeout_seconds": interaction.timeout_seconds,
            "submit_label": interaction.submit_label or None,
            "cancel_label": interaction.cancel_label or None,
            "size": interaction.size,
            "execution_id": str(self._execution_id) if self._execution_id else None,
            "tool_call_id": str(self._tool_call_id) if self._tool_call_id else None,
        }

        # 3. Publish to SSE channel (so the frontend opens the form)
        if self._gsage_session_id:
            await self._broker.publish_request(
                self._gsage_session_id, event_payload
            )

        # 4. REPLAN_AGENT → abort immediately
        if resume == ResumeMode.REPLAN_AGENT:
            logger.info(
                "Interaction %s (REPLAN_AGENT) — aborting tool execution",
                interaction_id,
            )
            raise InteractionReplanRequested(
                interaction_id=interaction_id,
                schema=schema,
                context=context,
            )

        # 5. CONTINUE_TOOL → block until user responds
        logger.info(
            "Interaction %s (CONTINUE_TOOL) — waiting for user response",
            interaction_id,
        )
        response = await self._broker.wait_for_response(
            interaction_id,
            timeout_seconds=interaction.timeout_seconds,
        )

        # 6. Update DB record
        db_row.status = InteractionStatus.SUBMITTED.value
        db_row.response_json = response
        await self._session.commit()

        return response

    # ── Convenience helpers ───────────────────────────────────────────────

    async def form(
        self,
        form_cls: type,
        *,
        title: str,
        description: str = "",
        resume: ResumeMode = ResumeMode.CONTINUE_TOOL,
        timeout_seconds: int = 600,
        context: Optional[dict] = None,
    ) -> dict:
        """Shortcut for ``request(FormInteraction(...))``.

        Example::

            dados = await tool.interaction.form(
                ClienteForm,
                title="Cadastro de Cliente",
                description="Informe os dados abaixo.",
                resume=ResumeMode.CONTINUE_TOOL,
                context={"ticket_id": "INC-1234"},
            )
        """
        fi = FormInteraction(
            title=title,
            description=description,
            timeout_seconds=timeout_seconds,
            form=form_cls,
        )
        return await self.request(fi, resume=resume, context=context)
