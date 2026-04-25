"""gSage AI — Knowledge Base toolkit for the agno Agent.

This toolkit replaces agno's built-in ``search_knowledge_base`` tool and adds
write operations.  Three tools are exposed to the LLM:

- ``search_knowledge_base`` — semantic search that returns matching chunks
  **plus a numbered References block** pointing to the download URL of the
  original uploaded document (when available in MinIO).
- ``add_to_knowledge_base`` — append a text entry.
- ``delete_from_knowledge_base`` — remove a named entry.

All tools operate on the **same** per-org Weaviate collection (``kb_{org_id}``)
that the agent uses through ``knowledge=``, ensuring read/write consistency.

When building the Agent, pass ``search_knowledge=False`` and
``add_search_knowledge_instructions=False`` so agno does not auto-register its
own default tool — ours already includes tailored instructions for citing
references.

Usage::

    from src.backend_api.app.services.knowledge_tools import KnowledgeToolkit

    toolkit = KnowledgeToolkit(knowledge=build_knowledge(ctx.org_id))
    agent = Agent(..., tools=[toolkit], search_knowledge=False,
                  add_search_knowledge_instructions=False)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, cast

from agno.knowledge.knowledge import Knowledge
from agno.tools import Toolkit

from src.shared.database import _get_session_maker
from src.shared.config.settings import get_settings
from src.shared.models.ingest_job import GSageIngestJob, IngestScope, IngestStatus
from src.shared.models.knowledge_base import GSageKnowledgeSource

if TYPE_CHECKING:
    from agno.vectordb.weaviate.weaviate import Weaviate

log = logging.getLogger(__name__)

# Relative path of the SPA download route.  We always emit an ABSOLUTE URL
# (``{public_base_url}{_DOWNLOAD_URL_PATH}``) so the link is usable in
# channels that render plain text without a known origin (Telegram,
# e-mail).  The web client's MarkdownLink intercepts clicks on the same
# path regardless of whether the host is included.  The backing API
# route is registered in ``src.backend_api.app.api.v1.knowledge.download_router``
# under ``/api`` and the SPA route in
# ``web_client/src/pages/KbDownloadPage.tsx`` handles auth gating.
_DOWNLOAD_URL_PATH = "/kb/download/{job_id}"


def _build_download_url(job_id: str) -> str:
    base = (get_settings().public_base_url or "").rstrip("/")
    path = _DOWNLOAD_URL_PATH.format(job_id=job_id)
    return f"{base}{path}" if base else path


class KnowledgeToolkit(Toolkit):
    """Expose knowledge-base read+write operations to the LLM agent."""

    def __init__(
        self,
        knowledge: Knowledge,
        *,
        org_id: Optional[uuid.UUID] = None,
        user_id: Optional[uuid.UUID] = None,
        dept_id: Optional[uuid.UUID] = None,
    ) -> None:
        super().__init__(name="knowledge_base")
        self._knowledge = knowledge
        self._org_id = org_id
        self._user_id = user_id
        self._dept_id = dept_id
        self.register(self.search_knowledge_base)
        self.register(self.add_to_knowledge_base)
        self.register(self.delete_from_knowledge_base)

    async def search_knowledge_base(self, query: str) -> str:
        """Search the organization's knowledge base for information relevant
        to the query and return matching chunks grouped by source document.

        ALWAYS call this tool when the user asks about internal documents,
        previously saved facts, procedures, policies, or any information that
        may have been ingested into the knowledge base.

        OUTPUT FORMAT
        -------------
        The result is a plain text string with two sections:

        1. Matching chunks, each prefixed with a single reference marker
           ``[ref: N]`` when the chunk comes from an uploaded file.  Multiple
           chunks from the SAME file share the SAME ``N``.  Chunks from the
           system/default knowledge (no downloadable original) have NO
           marker — do not cite them with a reference number.

        2. A ``References:`` block mapping each ``N`` to the original
           filename and an absolute download URL, e.g.:

               References:
               [1] estatuto.pdf — download: https://app.example.com/kb/download/9b0f1570-...

        CITATION RULES (IMPORTANT)
        --------------------------
        - Cite ONLY numbers that appear in the ``References`` block.
          If only ``[1]`` is listed, never write ``[2]`` or ``[1, 3]``.
        - Use a single bracketed number per citation: ``[1]`` — never
          ``[1, 2]`` or ``[1] [3]`` in sequence for the same statement.
        - At the end of the answer, include a ``References`` section with
          Markdown links using the EXACT URLs returned by this tool:

              References:
              [1] [estatuto.pdf](https://app.example.com/kb/download/9b0f1570-...)

        - Never modify the URL: do not change the host, do not truncate
          the UUID, do not add a query string.  Never fabricate a link
          for chunks that have no reference marker.

        Args:
            query: Natural-language search query.

        Returns:
            Formatted string with chunks and a references section.
        """
        try:
            docs = await self._knowledge.asearch(query=query)
        except Exception as exc:
            log.error("search_knowledge_base failed: %s", exc, exc_info=True)
            return f"Error searching knowledge base: {exc}"

        if not docs:
            return "No documents found."

        # Dedup key is the source filename (inner archive member when
        # present).  This keeps references stable when the same document
        # was ingested multiple times (each upload creates a new job_id
        # with identical filename) and ensures ONE citation number per
        # source document regardless of how many chunks matched.
        refs: dict[str, dict[str, object]] = {}
        chunks_out: list[str] = []

        for doc in docs:
            meta = doc.meta_data or {}
            # ``meta_data`` is stored as a JSON string in Weaviate; agno's
            # Weaviate adapter parses it back to a dict, but be defensive.
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (ValueError, TypeError):
                    meta = {}

            job_id: Optional[str] = None
            filename: Optional[str] = None
            if isinstance(meta, dict):
                raw_job = meta.get("job_id")
                job_id = str(raw_job) if raw_job else None
                filename = meta.get("inner_filename") or meta.get("original_filename")

            ref_num: Optional[int] = None
            if job_id and filename:
                key = str(filename)
                if key not in refs:
                    refs[key] = {
                        "num": len(refs) + 1,
                        "filename": key,
                        # First job_id encountered for this filename wins.
                        # Later duplicate ingestions resolve to this one;
                        # authorisation is equivalent since they belong to
                        # the same org.
                        "url": _build_download_url(job_id),
                    }
                ref_num = cast(int, refs[key]["num"])

            content = doc.content or ""
            prefix = f"[ref: {ref_num}] " if ref_num else ""
            chunks_out.append(f"{prefix}{content}")

        body = "\n\n---\n\n".join(chunks_out)

        if refs:
            ref_lines = [
                f"[{r['num']}] {r['filename']} — download: {r['url']}"
                for r in sorted(refs.values(), key=lambda x: cast(int, x["num"]))
            ]
            body += "\n\nReferences:\n" + "\n".join(ref_lines)

        return body

    async def add_to_knowledge_base(
        self,
        content: str,
        name: Optional[str] = None,
    ) -> str:
        """Save information to the organization's knowledge base so it can be
        recalled in future conversations.

        Use this tool when the user explicitly asks to remember, save, or store
        something.  The content is indexed for semantic search and will appear
        in ``search_knowledge_base`` results.

        Args:
            content: The text to save (facts, procedures, notes, etc.).
            name: Optional short label for the entry (e.g. "VPN procedure").

        Returns:
            Confirmation message.
        """
        label_suffix = f" ({name})" if name else ""

        # ------------------------------------------------------------------
        # 1. Create a GSageIngestJob row so the entry appears in the
        #    "Ingest history" list in the UI alongside uploaded documents.
        #    Only possible when the toolkit was built with tenant context
        #    (org_id + user_id); agent sessions without it still save to
        #    Weaviate but are skipped from the history.
        # ------------------------------------------------------------------
        job_id: Optional[str] = None
        display_name = name or "Agent-saved note"
        if self._org_id is not None and self._user_id is not None:
            try:
                session_maker = _get_session_maker()
                async with session_maker() as db:
                    job = GSageIngestJob(
                        org_id=self._org_id,
                        user_id=self._user_id,
                        dept_id=None,
                        scope=IngestScope.ORG,
                        original_filename=display_name[:500],
                        file_size=len(content.encode("utf-8")),
                        status=IngestStatus.COMPLETED,
                        chunks_stored=1,
                    )
                    db.add(job)
                    await db.flush()
                    job_id = str(job.id)
                    await db.commit()
            except Exception as exc:
                # Non-fatal: continue without history row
                log.warning(
                    "add_to_knowledge_base: failed to create GSageIngestJob: %s", exc,
                )

        # ------------------------------------------------------------------
        # 2. Insert into Weaviate with metadata aligned to the upload flow
        #    (source / scope / job_id / user_id / original_filename / saved_at).
        # ------------------------------------------------------------------
        metadata: dict[str, str] = {
            "source": GSageKnowledgeSource.USER_REQUEST.value,
            "scope": IngestScope.ORG,
            "original_filename": display_name,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        if job_id:
            metadata["job_id"] = job_id
        if self._user_id is not None:
            metadata["user_id"] = str(self._user_id)

        try:
            await self._knowledge.ainsert(
                text_content=content,
                name=name,
                metadata=metadata,
            )
            log.info("Knowledge entry added%s (%d chars)", label_suffix, len(content))
            return f"Informação salva na base de conhecimento{label_suffix}."
        except Exception as exc:
            log.error("Failed to add knowledge entry: %s", exc, exc_info=True)
            # Best-effort: mark the history row as failed so it isn't misleading
            if job_id is not None:
                try:
                    session_maker = _get_session_maker()
                    async with session_maker() as db:
                        row = await db.get(GSageIngestJob, uuid.UUID(job_id))
                        if row is not None:
                            row.status = IngestStatus.FAILED
                            row.error_message = str(exc)[:1000]
                            row.chunks_stored = 0
                            await db.commit()
                except Exception:
                    pass
            return f"Erro ao salvar na base de conhecimento: {exc}"

    def delete_from_knowledge_base(self, name: str) -> str:
        """Remove an entry from the knowledge base by its name/label.

        Use this only when the user explicitly asks to forget or remove
        previously saved information.

        Args:
            name: The name/label of the entry to remove.

        Returns:
            Confirmation message.
        """
        try:
            vdb = cast("Weaviate", self._knowledge.vector_db)
            vdb.delete_by_name(name)
            log.info("Knowledge entry deleted: %s", name)
            return f"Entrada '{name}' removida da base de conhecimento."
        except Exception as exc:
            log.error("Failed to delete knowledge entry '%s': %s", name, exc, exc_info=True)
            return f"Erro ao remover entrada '{name}': {exc}"
