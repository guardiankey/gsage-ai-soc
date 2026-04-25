"""gSage AI — Knowledge Base source enum.

The knowledge base is stored in Weaviate (not PostgreSQL).
This module retains only the GSageKnowledgeSource enum used by the CRUD tool.
"""

from __future__ import annotations

import enum


class GSageKnowledgeSource(str, enum.Enum):
    """Source of knowledge base entry."""

    USER_REQUEST = "user_request"      # User explicitly requested: "remember this"
    AGENT_AUTO = "agent_auto"          # Agent automatically stored relevant finding
    ADMIN = "admin"                    # Admin manually added via REST API
    DOCUMENT_UPLOAD = "document_upload"  # Uploaded via /knowledge/ingest endpoint
    SYSTEM = "system"                  # Default knowledge base loaded on org creation

