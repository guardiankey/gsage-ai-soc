"""gSage AI — Microsoft Teams formatting helpers.

Microsoft Teams renders Markdown natively when the activity sets
``textFormat="markdown"``, so unlike Telegram we do **not** transform
the agent output into HTML. The only inbound concern is stripping the
``@bot`` mention entity that Teams prepends to every group/channel
message before delivering it to the bot.

Outbound chunking is provided as a safety net: Teams advertises a
~28 KB body limit per activity, but very large agent responses must
still be split.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List

# Conservative default — see settings.teams_max_message_length.
DEFAULT_MAX_LEN = 25_000


def strip_bot_mention(activity: Any) -> str:
    """Return ``activity.text`` with the leading ``@bot`` mention removed.

    Teams delivers group/channel messages with the bot's display name
    prepended, e.g. ``"<at>Gsage</at> hello"`` (HTML) or ``"@Gsage hello"``
    (plain). Stripping is required so the agent doesn't see its own name
    in every prompt.
    """
    text = getattr(activity, "text", "") or ""
    if not text:
        return ""

    entities = getattr(activity, "entities", None) or []
    recipient = getattr(activity, "recipient", None)
    bot_id = getattr(recipient, "id", None) if recipient is not None else None

    for entity in entities:
        # Bot Framework Mention entity carries `mentioned.id` matching
        # `activity.recipient.id` when the bot itself was mentioned.
        ent_type = (
            getattr(entity, "type", None)
            or (entity.get("type") if isinstance(entity, dict) else None)
        )
        if str(ent_type or "").lower() != "mention":
            continue
        mentioned = (
            getattr(entity, "mentioned", None)
            or (entity.get("mentioned") if isinstance(entity, dict) else None)
        )
        mentioned_id = (
            getattr(mentioned, "id", None)
            if mentioned is not None and not isinstance(mentioned, dict)
            else (mentioned.get("id") if isinstance(mentioned, dict) else None)
        )
        mention_text = (
            getattr(entity, "text", None)
            or (entity.get("text") if isinstance(entity, dict) else None)
        )
        if bot_id and mentioned_id and bot_id == mentioned_id and mention_text:
            text = text.replace(mention_text, "")

    # Strip residual leading ``<at>...</at>`` HTML wrappers if any were
    # left behind (Teams sometimes injects them).
    text = re.sub(r"<at>.*?</at>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def split_text(text: str, max_len: int = DEFAULT_MAX_LEN) -> List[str]:
    """Split *text* into chunks of at most *max_len* characters.

    Splits on paragraph (``\\n\\n``) then line (``\\n``) boundaries
    when possible, falling back to a hard cut.
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def join_chunks(chunks: Iterable[str]) -> str:
    """Re-join chunks for persistence (we store the full response, not
    one row per chunk)."""
    return "\n\n".join(c for c in chunks if c)
