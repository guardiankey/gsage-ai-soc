"""gSage AI — Microsoft Teams REST streaming helper.

Implements the **outbound** "Stream bot messages" protocol documented at
https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bot-messages-ai-generated-content?tabs=before%2Cbotmessage

Protocol summary
----------------
Outbound activities sent to the conversation carry an additional
``streaminfo`` entity:

* **informative**  — first frame, type=``typing``. The Bot Framework Service
  responds with a ``ResourceResponse`` whose ``id`` is the *streamId* shared
  by every subsequent frame.
* **streaming**    — incremental frame, type=``typing``. Text MUST be
  cumulative (each frame contains everything sent so far + new tokens).
  Throttle to ≤1 req/s; recommended buffer 1.5–2 s between sends.
* **final**        — final frame, type=``message``. ``streamSequence`` is
  omitted. Carries the full markdown text plus optional attachments.

Constraints
~~~~~~~~~~~
* Total stream lifetime ≤2 minutes.
* 1-on-1 chats only (no group / channel).
* Cannot be combined with function-calling: as soon as the agent invokes a
  tool, fall back to a non-streaming reply.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Recommended buffer between successive ``streaming`` frames (seconds).
MIN_BUFFER_SECONDS: float = 1.5

# Hard cap enforced by the Bot Framework Service.
MAX_STREAM_LIFETIME_SECONDS: float = 110.0  # leave headroom under the 120 s limit


class TeamsStreamSender:
    """Stateful helper that emits the three streaminfo phases in order.

    The class holds the cumulative text and the *streamId* returned by the
    Bot Framework Service after the first ``informative`` frame.  Every
    subsequent ``streaming``/``final`` frame is sent through the same
    ``turn_context.send_activity`` to reuse the adapter's authenticated
    ``ConnectorClient``.
    """

    def __init__(self, turn_context: Any) -> None:
        self.turn_context = turn_context
        self.stream_id: Optional[str] = None
        self.sequence: int = 0
        self.accumulated_text: str = ""
        self._started_at: float = time.monotonic()
        self._last_send_at: float = 0.0
        self._finalized: bool = False

    # ------------------------------------------------------------------ utils
    @property
    def has_started(self) -> bool:
        """``True`` once the informative frame has been acknowledged."""
        return self.stream_id is not None

    @property
    def expired(self) -> bool:
        """``True`` once we are close to the 2-minute Bot Framework limit."""
        return (time.monotonic() - self._started_at) >= MAX_STREAM_LIFETIME_SECONDS

    def _build_streaminfo_entity(
        self, *, stream_type: str, sequence: Optional[int]
    ) -> Any:
        from botbuilder.schema import Entity

        data: dict = {"type": "streaminfo", "streamType": stream_type}
        if self.stream_id is not None:
            data["streamId"] = self.stream_id
        if sequence is not None:
            data["streamSequence"] = sequence
        ent = Entity()
        ent.type = "streaminfo"
        # Bot Framework expects the additional fields to be flattened on the
        # entity, not nested. Use the deserialise helper to attach them.
        try:
            return Entity().deserialize(data)
        except Exception:
            # Older botbuilder builds: fall back to setting attributes.
            for k, v in data.items():
                try:
                    setattr(ent, k, v)
                except Exception:
                    pass
            return ent

    # ----------------------------------------------------------- informative
    async def informative(self, text: str) -> None:
        """Send the first ``informative`` frame and capture the streamId."""
        from botbuilder.schema import Activity

        self.sequence += 1
        activity = Activity(
            type="typing",
            text=text,
            entities=[
                self._build_streaminfo_entity(
                    stream_type="informative", sequence=self.sequence
                )
            ],
        )
        try:
            response = await self.turn_context.send_activity(activity)
        except Exception:
            logger.exception("TeamsStreamSender.informative: send_activity failed")
            raise

        # ``send_activity`` may return either a ``ResourceResponse`` or a list
        # of them depending on botbuilder version.
        resource_id: Optional[str] = None
        if response is not None:
            if isinstance(response, list) and response:
                resource_id = getattr(response[0], "id", None)
            else:
                resource_id = getattr(response, "id", None)
        if resource_id:
            self.stream_id = resource_id
        self._last_send_at = time.monotonic()

    # -------------------------------------------------------------- streaming
    async def streaming(self, accumulated_text: str, *, force: bool = False) -> bool:
        """Send a ``streaming`` chunk if the throttle window has elapsed.

        Returns ``True`` when a frame was sent, ``False`` when the call was
        skipped because we are still inside the 1.5 s buffer.  Callers should
        retry later; the ``accumulated_text`` is stored regardless so a
        subsequent ``finalize`` always carries the full content.
        """
        from botbuilder.schema import Activity

        self.accumulated_text = accumulated_text
        if not self.stream_id:
            # No streamId yet → cannot emit streaming frames.  The caller
            # should have invoked ``informative`` first.
            return False

        if self.expired:
            return False

        now = time.monotonic()
        if not force and (now - self._last_send_at) < MIN_BUFFER_SECONDS:
            return False

        if not accumulated_text.strip():
            return False

        self.sequence += 1
        activity = Activity(
            type="typing",
            text=accumulated_text,
            entities=[
                self._build_streaminfo_entity(
                    stream_type="streaming", sequence=self.sequence
                )
            ],
        )
        try:
            await self.turn_context.send_activity(activity)
        except Exception as exc:
            # 403 ContentStreamNotAllowed = user pressed "Stop" — abort.
            logger.warning("TeamsStreamSender.streaming: %s", exc)
            return False
        self._last_send_at = now
        return True

    # ------------------------------------------------------------------ final
    async def finalize(
        self,
        final_text: str,
        *,
        attachments: Optional[list] = None,
    ) -> None:
        """Send the closing ``final`` frame (type=message)."""
        from botbuilder.schema import Activity

        if self._finalized:
            return
        self._finalized = True

        activity = Activity(
            type="message",
            text=final_text,
            text_format="markdown",
            entities=[
                self._build_streaminfo_entity(stream_type="final", sequence=None)
            ],
        )
        if attachments:
            activity.attachments = attachments
        try:
            await self.turn_context.send_activity(activity)
        except Exception:
            logger.exception("TeamsStreamSender.finalize: send_activity failed")
            raise
