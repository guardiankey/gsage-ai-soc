"""gSage AI — Base64 Decoder tool (MVP)."""

from __future__ import annotations

import base64
import binascii
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

MAX_INPUT_BYTES = 10 * 1024  # 10 KB (per PROMPT.md Phase 4)


class Base64DecoderTool(BaseTool):
    """
    Base64 Decoder — decode base64-encoded strings.

    Common in SOC work: encoded commands in logs, tokens, emails, malware samples.

    Permission: ``decode:base64``
    Timeout: 5s (local operation — generous buffer)
    Rate limit: 120 calls/min per org
    Circuit breaker: DISABLED (local, no external dependency)
    """

    name: ClassVar[str] = "base64_decoder"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Decode base64-encoded strings to their plaintext or binary representation"
    category: ClassVar[str] = "utility"
    permissions: ClassVar[list[str]] = ["decode:base64"]
    rate_limit_per_minute: ClassVar[int] = 120
    timeout_seconds: ClassVar[int] = 5
    use_circuit_breaker: ClassVar[bool] = False  # local tool

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "string",
                "description": (
                    "Base64-encoded input string to decode. "
                    "This field is required."
                ),
            },
            "encoding": {
                "type": "string",
                "enum": ["utf-8", "latin-1"],
                "default": "utf-8",
                "description": (
                    "Expected character encoding of the decoded output. "
                    "Defaults to 'utf-8' with automatic fallback to 'latin-1'."
                ),
            },
            "variant": {
                "type": "string",
                "enum": ["standard", "urlsafe"],
                "default": "standard",
                "description": (
                    "Base64 variant to use for decoding. "
                    "'standard' (default) uses the RFC 4648 alphabet; "
                    "'urlsafe' uses '-' and '_' instead of '+' and '/'."
                ),
            },
        },
        "additionalProperties": False,
    }
    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Decode a base64-encoded string.

        Params:
            data (str, required): Base64-encoded input.
            encoding (str, optional): Expected output encoding ("utf-8", "latin-1").
                Defaults to "utf-8" with fallback to latin-1.
            variant (str, optional): Base64 variant. "standard" (default) or "urlsafe".

        Returns:
            decoded_text (str): Decoded string if valid UTF-8 / latin-1.
            decoded_hex (str): Hex dump of raw bytes (always present).
            input_bytes (int): Input length in bytes.
            output_bytes (int): Output length in bytes.
            is_binary (bool): True if output contains non-printable bytes.
        """
        # ── Input validation ──────────────────────────────────────────────
        raw = params.get("data", "")
        if not raw:
            return self._failure("INVALID_INPUT", "'data' parameter is required")

        if not isinstance(raw, str):
            return self._failure("INVALID_INPUT", "'data' must be a string")

        # Size limit
        if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
            return self._failure("INPUT_TOO_LARGE", f"Input exceeds maximum size of {MAX_INPUT_BYTES // 1024}KB")

        # Strip whitespace (base64 in logs often has padding/newlines)
        cleaned = raw.strip().replace("\n", "").replace("\r", "").replace(" ", "")

        variant = params.get("variant", "standard")
        encoding = params.get("encoding", "utf-8")

        # ── Decoding ──────────────────────────────────────────────────────
        try:
            if variant == "urlsafe":
                decoded_bytes = base64.urlsafe_b64decode(cleaned + "==")
            else:
                # Add padding if missing
                padded = cleaned + "=" * (-len(cleaned) % 4)
                decoded_bytes = base64.b64decode(padded)
        except (binascii.Error, ValueError) as exc:
            return self._failure("DECODE_ERROR", f"Invalid base64 encoding: {exc}")

        # ── Output encoding detection ─────────────────────────────────────
        # Check for binary content (non-printable bytes)
        printable_threshold = 0.9
        non_printable = sum(1 for b in decoded_bytes if b < 0x20 and b not in (0x09, 0x0A, 0x0D))
        is_binary = (non_printable / max(len(decoded_bytes), 1)) > (1 - printable_threshold)

        decoded_text: Optional[str] = None
        actual_encoding: Optional[str] = None

        if not is_binary:
            # Try requested encoding first
            for enc in [encoding, "utf-8", "latin-1"]:
                try:
                    decoded_text = decoded_bytes.decode(enc)
                    actual_encoding = enc
                    break
                except (UnicodeDecodeError, LookupError):
                    continue

        return self._success({
            "decoded_text": decoded_text,
            "decoded_hex": decoded_bytes.hex(),
            "input_bytes": len(cleaned),
            "output_bytes": len(decoded_bytes),
            "is_binary": is_binary,
            "encoding_used": actual_encoding,
        })
