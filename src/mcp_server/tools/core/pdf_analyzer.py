"""gSage AI — PDF Security Analyzer tool.

Performs a security-focused static analysis of a PDF file uploaded as a
chat attachment.  A single call returns a comprehensive report covering:

  - Cryptographic hashes (MD5, SHA1, SHA256)
  - Document metadata (author, creator, dates, encryption, signatures)
  - Object inventory (all PDF object types with counts)
  - JavaScript detection (object references + code snippets)
  - Embedded URL extraction with suspicion classification
  - Auto-execute action detection (/OpenAction, /Launch, /SubmitForm, …)
  - Embedded file listing (with executable-extension warnings)
  - Text preview (first N chars via markitdown)
  - Aggregated risk summary (high / medium / low / clean)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel._url_utils import (
    URL_RE as _URL_RE,
    URL_SHORTENERS as _URL_SHORTENERS,
    SUSPICIOUS_TLDS as _SUSPICIOUS_TLDS,
    classify_url as _classify_url,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max bytes to load from MinIO (50 MB)
_PDF_MAX_BYTES = 50 * 1024 * 1024

# Max result size before offloading to MinIO (50 KB)
_MAX_INLINE_BYTES = 50 * 1024

# Maximum number of JS snippets to inline (each truncated to _JS_SNIPPET_MAX chars)
_JS_MAX_SNIPPETS = 15
_JS_SNIPPET_MAX = 800

# URL extraction: max URLs to return
_MAX_URLS = 100

# Accepted PDF MIME types
_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "application/vnd.pdf",
    "text/pdf",
    "text/x-pdf",
}

# Default max chars of extracted text to include in the report
_DEFAULT_MAX_TEXT_CHARS = 5000

# Executable extensions in embedded files → automatic HIGH risk flag
_EXECUTABLE_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".scr", ".com", ".hta", ".msi",
    ".pif", ".reg", ".jar", ".swf",
}

# PDF action types that can auto-execute code or exfiltrate data
_RISKY_ACTION_TYPES = {
    "/Launch",         # executes an external program
    "/SubmitForm",     # sends form data to a remote server
    "/ImportData",     # imports data from an external source
    "/GoToR",          # opens a remote PDF
    "/URI",            # navigates to a URL (less risky but noteworthy)
    "/JavaScript",     # embedded JavaScript execution
    "/JS",             # alias for JavaScript
    "/Sound",          # plays sound (unusual in malware, but possible)
    "/Movie",          # embeds/plays a movie object
    "/RichMedia",      # Flash/rich media (often exploited)
    "/ResetForm",      # resets form fields
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_hashes(data: bytes) -> dict[str, str]:
    """Return MD5, SHA1, and SHA256 hex digests for *data*."""
    return {
        "md5": hashlib.md5(data).hexdigest(),  # noqa: S324 — used for identification only
        "sha1": hashlib.sha1(data).hexdigest(),  # noqa: S324
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _pikepdf_obj_to_str(obj: Any) -> str:
    """Safely convert a pikepdf object to a string representation."""
    try:
        import pikepdf  # noqa: PLC0415
        if isinstance(obj, pikepdf.String):
            return str(obj)
        if isinstance(obj, pikepdf.Name):
            return str(obj)
        return repr(obj)[:200]
    except Exception:
        return repr(obj)[:200]


def _safe_pdf_string(obj: Any) -> Optional[str]:
    """Return a Python str from a pikepdf String/Name object, or None."""
    try:
        import pikepdf  # noqa: PLC0415
        if isinstance(obj, (pikepdf.String, pikepdf.Name)):
            return str(obj)
        return str(obj) if obj is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Analysis functions  (all take a pikepdf.Pdf instance)
# ---------------------------------------------------------------------------


def _extract_metadata(pdf: Any) -> dict:
    """Extract document information dictionary and structural metadata."""
    import pikepdf  # noqa: PLC0415

    meta: dict = {
        "pdf_version": pdf.pdf_version,
        "page_count": len(pdf.pages),
        "encrypted": pdf.is_encrypted,
    }

    # Check for digital signatures (AcroForm Sig fields)
    try:
        if "/AcroForm" in pdf.Root:
            meta["has_acroform"] = True
            acroform = pdf.Root["/AcroForm"]
            if "/SigFlags" in acroform:
                meta["signature_flags"] = int(acroform["/SigFlags"])
    except Exception:
        pass

    # Document information dictionary (/Info)
    try:
        info = pdf.docinfo
        for key in ("/Title", "/Author", "/Subject", "/Creator", "/Producer",
                    "/Keywords", "/CreationDate", "/ModDate"):
            try:
                val = info.get(key)
                if val is not None:
                    clean_key = key.lstrip("/").lower()
                    meta[clean_key] = _safe_pdf_string(val) or str(val)
            except Exception:
                pass
    except Exception:
        pass

    # XMP metadata (if present)
    try:
        with pdf.open_metadata() as xmp:
            if xmp:
                meta["has_xmp_metadata"] = True
    except Exception:
        pass

    # Check /Encrypt dictionary
    try:
        if "/Encrypt" in pdf.trailer:
            enc = pdf.trailer["/Encrypt"]
            enc_info: dict = {"present": True}
            try:
                if "/Filter" in enc:
                    enc_info["filter"] = str(enc["/Filter"])
                if "/V" in enc:
                    enc_info["version"] = int(enc["/V"])
                if "/R" in enc:
                    enc_info["revision"] = int(enc["/R"])
            except Exception:
                pass
            meta["encryption_details"] = enc_info
    except Exception:
        pass

    return meta


def _inventory_objects(pdf: Any) -> tuple[dict[str, int], list[str]]:
    """Walk all PDF objects and return (type_counts, suspicious_type_names).

    Returns a dict mapping object type name → count, and a list of type names
    that are considered suspicious (JS, actions, embedded files, etc.).
    """
    import pikepdf  # noqa: PLC0415

    counts: dict[str, int] = {}
    suspicious_found: list[str] = []

    _SUSPICIOUS_OBJECT_KEYS = {
        "/JavaScript", "/JS", "/Launch", "/SubmitForm", "/ImportData",
        "/EmbeddedFile", "/EmbeddedFiles", "/RichMedia", "/Movie",
        "/Sound", "/GoToR", "/AA",  # Additional Actions on page
    }

    def _visit(obj: Any) -> None:
        try:
            if isinstance(obj, pikepdf.Dictionary):
                obj_type = _safe_pdf_string(obj.get("/Type")) or "(dict)"
                counts[obj_type] = counts.get(obj_type, 0) + 1

                for key in obj.keys():
                    key_str = str(key)
                    if key_str in _SUSPICIOUS_OBJECT_KEYS:
                        counts[key_str] = counts.get(key_str, 0) + 1
                        if key_str not in suspicious_found:
                            suspicious_found.append(key_str)

            elif isinstance(obj, pikepdf.Stream):
                counts["/Stream"] = counts.get("/Stream", 0) + 1
                # Check stream dictionary for suspicious keys
                for key in obj.stream_dict.keys():
                    key_str = str(key)
                    if key_str in _SUSPICIOUS_OBJECT_KEYS:
                        counts[key_str] = counts.get(key_str, 0) + 1
                        if key_str not in suspicious_found:
                            suspicious_found.append(key_str)

            elif isinstance(obj, pikepdf.Array):
                counts["/Array"] = counts.get("/Array", 0) + 1

        except Exception:
            pass

    try:
        for obj in pdf.objects:
            _visit(obj)
    except Exception as exc:
        logger.debug("pdf_analyzer: object inventory error: %s", exc)

    # Clean up zero-noise keys
    counts = {k: v for k, v in counts.items() if v > 0}
    return counts, suspicious_found


def _detect_javascript(pdf: Any) -> list[dict]:
    """Find all JavaScript objects in the PDF and return code snippets."""
    import pikepdf  # noqa: PLC0415

    snippets: list[dict] = []

    def _extract_js(obj: Any, ref_str: str) -> None:
        try:
            js_src: Optional[str] = None
            if isinstance(obj, pikepdf.Dictionary):
                if "/JavaScript" in obj:
                    js_obj = obj["/JavaScript"]
                    if isinstance(js_obj, pikepdf.Stream):
                        js_src = js_obj.read_bytes().decode("utf-8", errors="replace")
                    elif isinstance(js_obj, pikepdf.Dictionary) and "/JS" in js_obj:
                        js_src = _safe_pdf_string(js_obj["/JS"])
                elif "/JS" in obj:
                    js_src = _safe_pdf_string(obj["/JS"])

            elif isinstance(obj, pikepdf.Stream):
                d = obj.stream_dict
                if "/JavaScript" in d or "/JS" in d:
                    js_src = obj.read_bytes().decode("utf-8", errors="replace")

            if js_src and js_src.strip():
                snippet = js_src.strip()[:_JS_SNIPPET_MAX]
                snippets.append({
                    "object_ref": ref_str,
                    "code_snippet": snippet,
                    "truncated": len(js_src.strip()) > _JS_SNIPPET_MAX,
                })
        except Exception:
            pass

    try:
        for i, obj in enumerate(pdf.objects):
            if len(snippets) >= _JS_MAX_SNIPPETS:
                break
            _extract_js(obj, f"obj {i}")
    except Exception as exc:
        logger.debug("pdf_analyzer: JS detection error: %s", exc)

    return snippets


def _extract_urls(pdf: Any, text_content: str) -> list[dict]:
    """Extract all URLs from PDF annotations, actions, and text content."""
    import pikepdf  # noqa: PLC0415

    seen: set[str] = set()
    urls: list[dict] = []

    def _add_url(url: str, source: str) -> None:
        url = url.strip().rstrip(">),;\"'")
        if not url or url in seen or len(urls) >= _MAX_URLS:
            return
        seen.add(url)
        level, reasons = _classify_url(url)
        urls.append({
            "url": url,
            "source": source,
            "suspicion": level,
            "reasons": reasons,
        })

    # Walk all objects looking for /URI actions
    try:
        for obj in pdf.objects:
            try:
                if isinstance(obj, pikepdf.Dictionary):
                    if "/URI" in obj:
                        uri = _safe_pdf_string(obj["/URI"])
                        if uri:
                            _add_url(uri, "annotation/action")
                    if "/A" in obj:
                        action = obj["/A"]
                        if isinstance(action, pikepdf.Dictionary):
                            if "/URI" in action:
                                uri = _safe_pdf_string(action["/URI"])
                                if uri:
                                    _add_url(uri, "annotation/action")
                            if "/S" in action:
                                s = str(action["/S"])
                                if s == "/URI" and "/URI" in action:
                                    uri = _safe_pdf_string(action["/URI"])
                                    if uri:
                                        _add_url(uri, "link_annotation")
                elif isinstance(obj, pikepdf.Stream):
                    d = obj.stream_dict
                    if "/URI" in d:
                        uri = _safe_pdf_string(d["/URI"])
                        if uri:
                            _add_url(uri, "stream")
            except Exception:
                continue
    except Exception as exc:
        logger.debug("pdf_analyzer: URL extraction object walk error: %s", exc)

    # Extract URLs from text content
    for m in _URL_RE.finditer(text_content):
        _add_url(m.group(0), "text_content")

    # Sort: high suspicion first
    _order = {"high": 0, "medium": 1, "low": 2}
    urls.sort(key=lambda u: _order.get(u["suspicion"], 3))
    return urls


def _detect_actions(pdf: Any) -> list[dict]:
    """Detect auto-execute and risky action triggers in the PDF."""
    import pikepdf  # noqa: PLC0415

    actions: list[dict] = []

    _ACTION_DESCRIPTIONS = {
        "/OpenAction": "Executes automatically when the document is opened",
        "/AA":         "Additional Actions trigger on page/document events",
        "/Launch":     "Launches an external application or script",
        "/SubmitForm": "Submits form data to a remote server (possible data exfiltration)",
        "/ImportData": "Imports data from an external source",
        "/GoToR":      "Opens a remote PDF file",
        "/JavaScript": "Executes JavaScript code",
        "/JS":         "Executes JavaScript code (alias)",
        "/URI":        "Navigates browser/viewer to a URL",
        "/RichMedia":  "Embeds rich media (Flash/video, historically exploited)",
        "/Movie":      "Embeds a movie object",
        "/Sound":      "Embeds a sound action",
        "/ResetForm":  "Resets all form fields",
    }

    _RISK_LEVELS = {
        "/OpenAction": "high",
        "/Launch":     "high",
        "/SubmitForm": "high",
        "/ImportData": "high",
        "/JavaScript": "high",
        "/JS":         "high",
        "/AA":         "medium",
        "/GoToR":      "medium",
        "/RichMedia":  "medium",
        "/URI":        "low",
        "/Movie":      "low",
        "/Sound":      "low",
        "/ResetForm":  "low",
    }

    seen_types: set[str] = set()

    def _check_dict(d: Any, context: str) -> None:
        try:
            for key in _ACTION_DESCRIPTIONS:
                if key in d and key not in seen_types:
                    obj = d[key]
                    action_type = "unknown"
                    target = None
                    try:
                        if isinstance(obj, pikepdf.Dictionary) and "/S" in obj:
                            action_type = str(obj["/S"])
                        elif isinstance(obj, pikepdf.Name):
                            action_type = str(obj)
                        if isinstance(obj, pikepdf.Dictionary) and "/URI" in obj:
                            target = _safe_pdf_string(obj["/URI"])
                    except Exception:
                        pass

                    seen_types.add(key)
                    entry: dict = {
                        "trigger": key,
                        "action_type": action_type,
                        "context": context,
                        "description": _ACTION_DESCRIPTIONS.get(key, "Risky PDF action"),
                        "risk": _RISK_LEVELS.get(key, "medium"),
                    }
                    if target:
                        entry["target"] = target
                    actions.append(entry)
        except Exception:
            pass

    # Check document-level catalog
    try:
        _check_dict(pdf.Root, "document_catalog")
    except Exception:
        pass

    # Walk all objects for action dictionaries
    try:
        for obj in pdf.objects:
            try:
                if isinstance(obj, pikepdf.Dictionary):
                    _check_dict(obj, "object")
                elif isinstance(obj, pikepdf.Stream):
                    _check_dict(obj.stream_dict, "stream")
            except Exception:
                continue
    except Exception as exc:
        logger.debug("pdf_analyzer: action detection walk error: %s", exc)

    # Sort: high risk first
    _r_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda a: _r_order.get(a["risk"], 3))
    return actions


def _list_embedded_files(pdf: Any) -> list[dict]:
    """List files embedded in the PDF's EmbeddedFiles name tree."""
    import pikepdf  # noqa: PLC0415

    embedded: list[dict] = []

    try:
        # /Names → /EmbeddedFiles name tree
        root = pdf.Root
        if "/Names" not in root:
            return embedded
        names_dict = root["/Names"]
        if "/EmbeddedFiles" not in names_dict:
            return embedded
        ef_tree = names_dict["/EmbeddedFiles"]

        # Flatten the name tree (may be a /Names array directly)
        pairs: list[tuple[str, Any]] = []

        def _walk_tree(node: Any) -> None:
            try:
                if isinstance(node, pikepdf.Dictionary):
                    if "/Names" in node:
                        arr = node["/Names"]
                        if isinstance(arr, pikepdf.Array):
                            raw: list[Any] = [arr[i] for i in range(len(arr))]  # type: ignore[arg-type]
                            for i in range(0, len(raw) - 1, 2):
                                pairs.append((_safe_pdf_string(raw[i]) or "", raw[i + 1]))
                    if "/Kids" in node:
                        kids = node.get("/Kids")
                        if isinstance(kids, pikepdf.Array):
                            for i in range(len(kids)):  # type: ignore[arg-type]
                                _walk_tree(kids[i])
            except Exception:
                pass

        _walk_tree(ef_tree)

        for name, filespec in pairs:
            try:
                entry: dict = {"name": name or "(unnamed)"}
                if isinstance(filespec, pikepdf.Dictionary):
                    if "/Desc" in filespec:
                        entry["description"] = _safe_pdf_string(filespec["/Desc"])
                    if "/EF" in filespec:
                        ef = filespec["/EF"]
                        if isinstance(ef, pikepdf.Dictionary) and "/F" in ef:
                            ef_stream = ef["/F"]
                            if isinstance(ef_stream, pikepdf.Stream):
                                params = ef_stream.stream_dict
                                if "/Size" in params:
                                    entry["size_bytes"] = int(params["/Size"])
                                if "/Subtype" in params:
                                    entry["mime_type"] = str(params["/Subtype"])

                ext = os.path.splitext(name.lower())[1] if name else ""
                entry["suspicious"] = ext in _EXECUTABLE_EXTENSIONS
                if entry["suspicious"]:
                    entry["reason"] = f"Executable file extension ({ext})"
                embedded.append(entry)
            except Exception:
                continue

    except Exception as exc:
        logger.debug("pdf_analyzer: embedded files error: %s", exc)

    return embedded


def _extract_text_preview(pdf_path: str, max_chars: int) -> tuple[str, Optional[str]]:
    """Extract text from the PDF via markitdown.

    Returns (text_preview, error_message_or_None).
    """
    try:
        from markitdown import MarkItDown  # noqa: PLC0415
        md = MarkItDown()
        result = md.convert(pdf_path)
        text = (result.text_content or "").strip()
        return text[:max_chars], None
    except Exception as exc:
        return "", f"Text extraction failed: {exc}"


def _build_risk_summary(
    metadata: dict,
    obj_inventory: dict[str, int],
    js_snippets: list[dict],
    urls: list[dict],
    actions: list[dict],
    embedded_files: list[dict],
) -> dict:
    """Aggregate all analysis results into a risk level and findings list."""
    findings: list[dict] = []

    # ── HIGH risk indicators ────────────────────────────────────────────────
    if js_snippets:
        findings.append({
            "severity": "high",
            "category": "javascript",
            "description": f"JavaScript code detected ({len(js_snippets)} object(s) found).",
        })

    high_actions = [a for a in actions if a.get("risk") == "high"]
    for action in high_actions:
        findings.append({
            "severity": "high",
            "category": "action",
            "description": f"{action['trigger']}: {action['description']}",
        })

    suspicious_exe = [f for f in embedded_files if f.get("suspicious")]
    for ef in suspicious_exe:
        findings.append({
            "severity": "high",
            "category": "embedded_file",
            "description": f"Embedded executable file: {ef['name']} — {ef.get('reason', '')}",
        })

    high_urls = [u for u in urls if u.get("suspicion") == "high"]
    for url in high_urls[:10]:  # cap at 10 to avoid flooding
        reasons_str = "; ".join(url.get("reasons", [])) or "unknown"
        findings.append({
            "severity": "high",
            "category": "url",
            "description": f"Suspicious URL ({reasons_str}): {url['url'][:120]}",
        })

    # ── MEDIUM risk indicators ──────────────────────────────────────────────
    medium_actions = [a for a in actions if a.get("risk") == "medium"]
    for action in medium_actions:
        findings.append({
            "severity": "medium",
            "category": "action",
            "description": f"{action['trigger']}: {action['description']}",
        })

    medium_urls = [u for u in urls if u.get("suspicion") == "medium"]
    if medium_urls:
        findings.append({
            "severity": "medium",
            "category": "url",
            "description": f"{len(medium_urls)} URL(s) with medium suspicion level.",
        })

    non_exe_embedded = [f for f in embedded_files if not f.get("suspicious")]
    if non_exe_embedded:
        findings.append({
            "severity": "medium",
            "category": "embedded_file",
            "description": f"{len(non_exe_embedded)} non-executable file(s) embedded in the PDF.",
        })

    # ── LOW risk indicators ─────────────────────────────────────────────────
    low_actions = [a for a in actions if a.get("risk") == "low"]
    for action in low_actions:
        findings.append({
            "severity": "low",
            "category": "action",
            "description": f"{action['trigger']}: {action['description']}",
        })

    low_urls = [u for u in urls if u.get("suspicion") == "low"]
    if low_urls:
        findings.append({
            "severity": "low",
            "category": "url",
            "description": f"{len(low_urls)} benign-looking URL(s) found.",
        })

    if metadata.get("encrypted"):
        findings.append({
            "severity": "low",
            "category": "metadata",
            "description": "PDF is encrypted. Full content inspection may be limited.",
        })

    # ── Determine overall risk level ────────────────────────────────────────
    severities = {f["severity"] for f in findings}
    if "high" in severities:
        risk_level = "high"
    elif "medium" in severities:
        risk_level = "medium"
    elif "low" in severities:
        risk_level = "low"
    else:
        risk_level = "clean"

    return {
        "risk_level": risk_level,
        "finding_count": len(findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class PdfAnalyzerTool(BaseTool):
    """
    PDF Security Analyzer — static security analysis of attached PDF files.

    Performs a comprehensive one-shot analysis of a PDF file uploaded as a
    chat attachment.  The report covers:

        hashes          MD5, SHA1, SHA256 of the raw file bytes.

        metadata        Author, Creator, Producer, dates, page count, PDF
                        version, encryption status, XMP metadata presence,
                        AcroForm/digital-signature details.

        object_inventory
                        Count of every PDF object type found in the file,
                        with special attention to suspicious types
                        (JavaScript, Launch actions, EmbeddedFile, etc.).

        javascript      Code snippets from all JavaScript objects in the PDF.
                        Any JavaScript in a PDF is inherently suspicious.

        urls            All URLs found in annotations, actions, and extracted
                        text content — classified as high / medium / low
                        suspicion based on IP-only hosts, URL shorteners,
                        suspicious TLDs, and excessive subdomains.

        actions         Auto-execute and risky action triggers:
                        /OpenAction (runs on open), /Launch (external program),
                        /SubmitForm (data exfiltration), /ImportData, etc.

        embedded_files  Files embedded inside the PDF, with a flag for
                        executable extensions (.exe, .ps1, .js, .vbs, …).

        text_preview    First ``max_text_chars`` characters of the extracted
                        text content (useful for phishing / social engineering
                        analysis).

        risk_summary    Aggregated risk level (high / medium / low / clean)
                        with a prioritised list of specific findings.

    Required parameter:
        file_id (str): UUID of the attached PDF file.

    Optional parameters:
        max_text_chars (int): Characters of extracted text to include.
                              Default: 5000. Range: 0–20000.

    Permission: ``agents:run``
    """

    name: ClassVar[str] = "pdf_analyzer"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Analyze PDF documents for security risks: JavaScript, embedded files, suspicious URLs, metadata"
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["agents:run"]
    rate_limit_per_minute: ClassVar[int] = 15
    timeout_seconds: ClassVar[int] = 90
    background_threshold_seconds: ClassVar[Optional[int]] = 60
    requires_approval: ClassVar[bool] = False
    use_circuit_breaker: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_entities": "file_id"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the attached PDF file to analyze. "
                    "Upload the PDF as a chat attachment first, then provide its UUID here."
                ),
            },
            "max_text_chars": {
                "type": "integer",
                "description": (
                    "Maximum number of characters of extracted text content to include "
                    "in the report (useful for phishing / social engineering analysis). "
                    "Default: 5000. Set to 0 to skip text extraction."
                ),
                "default": _DEFAULT_MAX_TEXT_CHARS,
                "minimum": 0,
                "maximum": 20000,
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
        start = time.monotonic()

        # ── Parameter validation ────────────────────────────────────────────
        file_id = params.get("file_id", "")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")

        try:
            uuid.UUID(file_id)
        except ValueError:
            return self._failure("INVALID_INPUT", f"'file_id' is not a valid UUID: {file_id!r}")

        max_text_chars = params.get("max_text_chars", _DEFAULT_MAX_TEXT_CHARS)
        if not isinstance(max_text_chars, int) or not (0 <= max_text_chars <= 20000):
            return self._failure(
                "INVALID_INPUT",
                "'max_text_chars' must be an integer between 0 and 20000.",
            )

        # ── Load file from MinIO ────────────────────────────────────────────
        file_meta = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_PDF_MAX_BYTES,
        )
        if file_meta is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' was not found or you do not have access to it.",
            )

        filename: str = file_meta.get("filename", "")
        content_type: str = file_meta.get("content_type", "application/octet-stream")
        pdf_bytes: bytes = file_meta.get("data", b"")
        file_size: int = file_meta.get("size_bytes", len(pdf_bytes))

        # Validate file type
        suffix = os.path.splitext(filename.lower())[1]
        is_pdf = suffix == ".pdf" or content_type.lower() in _PDF_CONTENT_TYPES
        if not is_pdf:
            return self._failure(
                "INVALID_FILE_TYPE",
                f"File '{filename}' does not appear to be a PDF "
                f"(content type: {content_type}, extension: {suffix or 'none'}). "
                "Only .pdf files are supported.",
            )

        if not pdf_bytes:
            return self._failure("EMPTY_FILE", f"File '{filename}' is empty.")

        # ── Compute hashes (from raw bytes, before temp file) ───────────────
        hashes = _compute_hashes(pdf_bytes)

        # ── Write to temp file ──────────────────────────────────────────────
        tmp_path: Optional[str] = None
        data: dict = {}

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".pdf", delete=False, prefix="gsage_ai_pdf_"
            ) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name

            # ── Run all analyses ────────────────────────────────────────────
            import pikepdf  # noqa: PLC0415

            try:
                pdf = pikepdf.open(tmp_path, suppress_warnings=True)
            except pikepdf.PdfError as exc:
                # Might still be parseable partially, or just bad
                return self._failure(
                    "PDF_PARSE_ERROR",
                    f"Failed to open PDF: {exc}. "
                    "The file may be corrupted, password-protected, or not a valid PDF.",
                    retryable=False,
                )

            with pdf:
                metadata = await _run_sync(_extract_metadata, pdf)
                obj_inventory, suspicious_types = await _run_sync(_inventory_objects, pdf)
                js_snippets = await _run_sync(_detect_javascript, pdf)
                actions = await _run_sync(_detect_actions, pdf)
                embedded_files = await _run_sync(_list_embedded_files, pdf)
                # URL extraction needs text too — we'll do text first
                text_preview, text_error = (
                    await _run_sync(_extract_text_preview, tmp_path, max_text_chars)
                    if max_text_chars > 0
                    else ("", None)
                )
                urls = await _run_sync(_extract_urls, pdf, text_preview)

            risk_summary = _build_risk_summary(
                metadata, obj_inventory, js_snippets, urls, actions, embedded_files
            )

            data = {
                "hashes": hashes,
                "metadata": metadata,
                "object_inventory": obj_inventory,
                "suspicious_object_types": suspicious_types,
                "javascript": js_snippets,
                "urls": urls,
                "actions": actions,
                "embedded_files": embedded_files,
                "risk_summary": risk_summary,
            }

            if max_text_chars > 0:
                data["text_preview"] = text_preview
                if text_error:
                    data["text_extraction_error"] = text_error

            data["_meta"] = {
                "file_id": file_id,
                "filename": filename,
                "file_size_bytes": file_size,
                "truncated": file_meta.get("truncated", False),
            }

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        elapsed = int((time.monotonic() - start) * 1000)

        # ── Offload large results to MinIO ──────────────────────────────────
        try:
            result_json = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            result_json = json.dumps({"error": "Result serialization failed"})

        if len(result_json.encode()) > _MAX_INLINE_BYTES:
            safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
            stored_filename = f"pdf_analysis_{safe_name}.json"
            file_info: Optional[dict] = None

            # Reuse the framework session (injected via _tool_session_ctx)
            # instead of creating a new one from the pool.  After a long
            # analysis (60-90s), pool connections may be stale; the
            # framework session was committed before execute() and is still
            # valid for a new transaction.
            from src.mcp_server.tools.base import _tool_session_ctx  # noqa: PLC0415

            store_session = _tool_session_ctx.get()
            if store_session is not None:
                try:
                    file_info = await self._store_file(
                        data=result_json.encode("utf-8"),
                        filename=stored_filename,
                        content_type="application/json",
                        agent_context=agent_context,
                        session=store_session,
                        description=f"PDF security analysis for {filename}",
                    )
                except Exception as exc:
                    logger.error("pdf_analyzer: failed to store large result: %s", exc)

            summary_data: dict = {
                "_meta": data["_meta"],
                "hashes": data["hashes"],
                "risk_summary": data["risk_summary"],
                "note": (
                    "The full analysis report exceeds the inline size limit. "
                    "A summary is shown here; use the download link for the complete details."
                ),
            }
            if file_info:
                summary_data["result_file"] = file_info
            # Include metadata and top findings inline
            summary_data["metadata"] = data.get("metadata", {})
            summary_data["javascript_count"] = len(data.get("javascript", []))
            summary_data["actions"] = data.get("actions", [])
            summary_data["urls_high_count"] = sum(
                1 for u in data.get("urls", []) if u.get("suspicion") == "high"
            )

            return self._partial(
                summary_data,
                code="RESULT_OFFLOADED",
                message=(
                    "Full analysis stored in the linked file. "
                    "Risk summary and key findings are shown inline."
                ),
                execution_time_ms=elapsed,
            )

        return self._success(data, execution_time_ms=elapsed)


# ---------------------------------------------------------------------------
# Async bridge for CPU-bound synchronous functions
# ---------------------------------------------------------------------------


import asyncio
import functools


async def _run_sync(fn: Any, *args: Any) -> Any:
    """Run a synchronous function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))
