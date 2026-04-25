"""Telegram message formatting utilities.

Extracted from ``handler.py`` so these can be reused by
:mod:`channel_sender` for push-delivering agent responses to Telegram.
"""

from __future__ import annotations

import re

# Maximum characters per Telegram message (hard platform limit = 4096).
DEFAULT_MAX_LEN = 4096

# Regex patterns for Markdown → Telegram HTML conversion.
_MD_BOLD_3      = re.compile(r'\*{3}(.+?)\*{3}', re.DOTALL)   # ***bold***
_MD_BOLD_2      = re.compile(r'\*{2}(.+?)\*{2}', re.DOTALL)   # **bold**
_MD_BOLD_1      = re.compile(r'\*(.+?)\*', re.DOTALL)          # *bold* (single)
_MD_ITALIC_UND  = re.compile(r'_{1,2}(.+?)_{1,2}', re.DOTALL)
_MD_CODE_BLOCK  = re.compile(r'```[\w]*\n?(.*?)```', re.DOTALL)
_MD_INLINE_CODE = re.compile(r'`(.+?)`')
_MD_HEADER      = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_TABLE_SEP   = re.compile(r'^\|[-:| ]+\|$', re.MULTILINE)
# Capture both the label and URL so we can render Markdown links as
# Telegram HTML anchors (<a href="...">label</a>).
_MD_LINK        = re.compile(r'\[([^\]]+?)\]\(([^)]+?)\)')
_MD_HR          = re.compile(r'^[-*_]{3,}\s*$', re.MULTILINE)
_HTML_CHARS     = re.compile(r'[&<>]')
_HTML_ESCAPE    = {'&': '&amp;', '<': '&lt;', '>': '&gt;'}


def markdown_to_telegram_html(text: str) -> str:
    """Convert LLM Markdown output to Telegram HTML.

    - Preserves **bold** / ***bold*** as <b>text</b>.
    - Renders Markdown links ``[label](url)`` as ``label (url)`` plain
      text.  The Telegram client auto-linkifies bare URLs, which is
      more reliable than HTML anchors and keeps the URL visible to the
      user (important for download citations).
    - Strips other Markdown (headers, italic, code blocks, tables, HR).
    - Escapes HTML special characters (&, <, >) before inserting tags.
    """
    # 1. Strip fenced code blocks (keep content, lose fences)
    text = _MD_CODE_BLOCK.sub(r'\1', text)
    # 2. Strip inline code ticks
    text = _MD_INLINE_CODE.sub(r'\1', text)
    # 3. Strip Markdown headers
    text = _MD_HEADER.sub('', text)
    # 4. Strip table separator rows and pipe chars
    text = _MD_TABLE_SEP.sub('', text)
    text = text.replace('|', ' ')
    # 5. Strip horizontal rules
    text = _MD_HR.sub('', text)
    # 6. Render Markdown links as ``label (url)`` plain text BEFORE HTML
    # escaping.  When label and url are identical (e.g. raw URL written
    # by the LLM), keep just the URL to avoid duplication.
    def _link_repl(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        return url if label == url else f"{label} ({url})"
    text = _MD_LINK.sub(_link_repl, text)
    # 7. Escape HTML special chars BEFORE inserting tags
    text = _HTML_CHARS.sub(lambda m: _HTML_ESCAPE[m.group()], text)
    # 8. Convert bold (triple first to avoid double-match)
    text = _MD_BOLD_3.sub(r'<b>\1</b>', text)
    text = _MD_BOLD_2.sub(r'<b>\1</b>', text)
    # 9. Strip single-asterisk italic (don't render as bold — just remove marks)
    text = _MD_BOLD_1.sub(r'\1', text)
    # 10. Strip underscore italic
    text = _MD_ITALIC_UND.sub(r'\1', text)
    # 11. Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def split_text(text: str, max_len: int = DEFAULT_MAX_LEN) -> list[str]:
    """Split *text* into chunks of at most *max_len* characters.

    Splits on newlines when possible to avoid breaking mid-sentence.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
            # If a single line exceeds max_len, hard-split it.
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current.rstrip())

    return chunks or [text[:max_len]]
