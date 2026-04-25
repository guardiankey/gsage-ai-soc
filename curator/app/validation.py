"""Curator — input validation per collection type.

Each validator receives the raw string value and returns a canonical
(normalised) form, or raises ValueError with a descriptive message.

For ip/cidr collection types the canonical value is stored in the
``cidr`` column (PostgreSQL CIDR native type).  For all other types
it is stored in the ``value`` column.
"""

from __future__ import annotations

import ipaddress
import re


# ── Validators ────────────────────────────────────────────────────────────────


def validate_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ValueError(f"Invalid IP address: {value!r}")


def validate_cidr(value: str) -> str:
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
        return str(net)
    except ValueError:
        raise ValueError(f"Invalid CIDR notation: {value!r}")


_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)


def validate_domain(value: str) -> str:
    v = value.strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(v):
        raise ValueError(f"Invalid domain name: {value!r}")
    return v


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def validate_url(value: str) -> str:
    v = value.strip()
    if not _URL_RE.match(v):
        raise ValueError(f"Invalid URL: {value!r}")
    return v


def validate_domain_regex(value: str) -> str:
    try:
        re.compile(value.strip())
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}")
    return value.strip()


def validate_hash_md5(value: str) -> str:
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        raise ValueError(f"Invalid MD5 hash (expected 32 hex chars): {value!r}")
    return v


def validate_hash_sha1(value: str) -> str:
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", v):
        raise ValueError(f"Invalid SHA1 hash (expected 40 hex chars): {value!r}")
    return v


def validate_hash_sha256(value: str) -> str:
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise ValueError(f"Invalid SHA256 hash (expected 64 hex chars): {value!r}")
    return v


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(value: str) -> str:
    v = value.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError(f"Invalid email address: {value!r}")
    return v


_ASN_RE = re.compile(r"^(?:AS|as)?(\d{1,10})$")


def validate_asn(value: str) -> str:
    m = _ASN_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid ASN (expected AS12345 or plain number): {value!r}")
    num = int(m.group(1))
    if num < 1 or num > 4294967295:
        raise ValueError(f"ASN out of range: {num}")
    return f"AS{num}"


def validate_ja3(value: str) -> str:
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        raise ValueError(f"Invalid JA3 hash (expected 32 hex chars): {value!r}")
    return v


def validate_ja4(value: str) -> str:
    # JA4 fingerprints have a specific format: t13d1516h2_..._... but also
    # accept 32 hex chars (JA4 hash variant).
    v = value.strip()
    if re.fullmatch(r"[0-9a-f]{32}", v.lower()):
        return v.lower()
    # Accept the full JA4 string format (at least 10 chars, printable)
    if len(v) >= 10 and v.isprintable():
        return v
    raise ValueError(f"Invalid JA4 fingerprint: {value!r}")


# ── Dispatch table ─────────────────────────────────────────────────────────

# Types that use the CIDR column (ip/cidr)
CIDR_TYPES = {"ip", "cidr"}

_VALIDATORS = {
    "ip": validate_ip,
    "cidr": validate_cidr,
    "domain": validate_domain,
    "url": validate_url,
    "domain_regex": validate_domain_regex,
    "file_hash_md5": validate_hash_md5,
    "file_hash_sha1": validate_hash_sha1,
    "file_hash_sha256": validate_hash_sha256,
    "email": validate_email,
    "asn": validate_asn,
    "ja3": validate_ja3,
    "ja4": validate_ja4,
}


def validate_value(collection_type: str, raw_value: str) -> str:
    """Validate and normalise *raw_value* for the given *collection_type*.

    Returns the canonical string form.  Raises ``ValueError`` on failure.
    """
    validator = _VALIDATORS.get(collection_type)
    if validator is None:
        raise ValueError(f"Unknown collection type: {collection_type!r}")
    return validator(raw_value)
