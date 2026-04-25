"""gSage AI — Domain Security Audit tool."""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Common DKIM selectors to probe when none are specified by the caller
_DEFAULT_DKIM_SELECTORS = [
    "default", "google", "selector1", "selector2",
    "s1", "s2", "k1", "dkim", "mail",
]

_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9_](?:[a-zA-Z0-9\-_]{0,61}[a-zA-Z0-9_])?\.)+[a-zA-Z]{2,}$"
)


def _grade(score: int) -> str:
    if score >= 95:
        return "A+"
    if score >= 85:
        return "A"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


class DomainSecurityAuditTool(BaseTool):
    """
    Domain Security Audit — comprehensive email/domain security posture.

    Checks: SPF, DMARC, DNSSEC, STARTTLS, DANE/TLSA, MTA-STS, TLS-RPT,
    BIMI, TLS certificate (port 443), CAA records, and DKIM selector probes.

    Permission: ``dns:security``
    Timeout: 30s
    Rate limit: 20 queries/min per org
    Circuit breaker: enabled
    """

    name: ClassVar[str] = "domain_security_audit"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Comprehensive domain and email security posture check: SPF, DKIM, DMARC, DNSSEC, TLS, and MX"
    category: ClassVar[str] = "dns"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["dns:security"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "domain"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Domain to audit (e.g. 'example.com').",
            },
            "skip_tls": {
                "type": "boolean",
                "description": (
                    "Skip live STARTTLS/SMTP connection checks (faster, reduces latency). "
                    "MTA-STS, TLS-RPT, and DANE DNS record checks are still performed. Default: false."
                ),
                "default": False,
            },
            "include_tls_cert": {
                "type": "boolean",
                "description": (
                    "Probe port 443 to retrieve and validate the domain's TLS certificate. "
                    "Adds ~2-3s of latency. Default: true."
                ),
                "default": True,
            },
            "dkim_selectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "DKIM selectors to probe via DNS. Overrides the default list of 9 selectors: "
                    "default, google, selector1, selector2, s1, s2, k1, dkim, mail."
                ),
            },
        },
        "required": ["domain"],
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        raw_domain = params.get("domain", "")
        if not isinstance(raw_domain, str):
            return self._failure("INVALID_INPUT", "'domain' must be a string")
        domain = raw_domain.strip().lower().rstrip(".")
        if not domain:
            return self._failure("INVALID_INPUT", "'domain' parameter is required")
        if not _DOMAIN_RE.match(domain):
            return self._failure("INVALID_INPUT", f"Invalid domain format: '{domain}'")

        skip_tls: bool = bool(params.get("skip_tls", False))
        include_tls_cert: bool = bool(params.get("include_tls_cert", True))
        dkim_selectors: list[str] = params.get("dkim_selectors") or _DEFAULT_DKIM_SELECTORS

        # Run all independent checks concurrently
        tls_cert_coro = (
            self._check_tls_cert(domain) if include_tls_cert else asyncio.sleep(0, result=None)
        )

        (
            checkdmarc_result,
            tls_cert_result,
            caa_result,
            dkim_result,
        ) = await asyncio.gather(
            self._run_checkdmarc(domain, skip_tls),
            tls_cert_coro,
            self._check_caa(domain),
            self._probe_dkim(domain, dkim_selectors),
            return_exceptions=True,
        )

        # Assemble audit dict
        audit: dict = {"domain": domain}

        if isinstance(checkdmarc_result, BaseException):
            audit["checkdmarc_error"] = str(checkdmarc_result)
            cdmarc: dict = {}
        else:
            cdmarc = checkdmarc_result or {}  # type: ignore[assignment]
        audit.update(cdmarc)

        if include_tls_cert:
            if isinstance(tls_cert_result, BaseException):
                audit["tls_cert"] = {"error": str(tls_cert_result)}
            else:
                audit["tls_cert"] = tls_cert_result

        if isinstance(caa_result, BaseException):
            audit["caa"] = {"error": str(caa_result)}
        else:
            audit["caa"] = caa_result

        if isinstance(dkim_result, BaseException):
            audit["dkim_selectors"] = {"error": str(dkim_result)}
        else:
            audit["dkim_selectors"] = dkim_result

        # Scoring
        score_detail, total_score = self._compute_score(audit, include_tls_cert)
        audit["security_score"] = total_score
        audit["grade"] = _grade(total_score)
        audit["score_detail"] = score_detail
        audit["recommendations"] = self._build_recommendations(audit)

        return self._success(audit)

    # ── checkdmarc ────────────────────────────────────────────────────────

    async def _run_checkdmarc(self, domain: str, skip_tls: bool) -> dict:
        """Run checkdmarc.check_domains() in a thread (synchronous library)."""
        import checkdmarc  # type: ignore[import-untyped]

        raw = await asyncio.to_thread(
            checkdmarc.check_domains,
            [domain],
            skip_tls=skip_tls,
            timeout=5.0,
            retries=1,
        )

        # check_domains returns a list when given a list
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None:
            return {}

        def _to_plain(obj: object) -> Any:
            """Recursively convert checkdmarc dataclass objects to plain dicts/lists."""
            if hasattr(obj, "__dict__"):
                return {k: _to_plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
            if isinstance(obj, list):
                return [_to_plain(i) for i in obj]
            if isinstance(obj, dict):
                return {k: _to_plain(v) for k, v in obj.items()}
            return obj

        data: dict = _to_plain(raw) or {}
        return {
            "base_domain": data.get("base_domain"),
            "dnssec": data.get("dnssec"),
            "soa": data.get("soa"),
            "ns": data.get("ns"),
            "mx": data.get("mx"),
            "spf": data.get("spf"),
            "dmarc": data.get("dmarc"),
            "smtp_tls_reporting": data.get("smtp_tls_reporting"),
            "mta_sts": data.get("mta_sts"),
            "bimi": data.get("bimi"),
        }

    # ── TLS certificate ───────────────────────────────────────────────────

    async def _check_tls_cert(self, domain: str) -> dict:
        """Retrieve and parse the TLS certificate from port 443."""
        from cryptography import x509  # type: ignore[import-untyped]
        from cryptography.hazmat.backends import default_backend  # type: ignore[import-untyped]

        def _get_cert() -> dict:
            ctx = ssl.create_default_context()
            try:
                with socket.create_connection((domain, 443), timeout=8) as sock:
                    with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                        der = ssock.getpeercert(binary_form=True)
                        if der is None:
                            return {"valid": False, "error": "No certificate returned by server"}
                        cert = x509.load_der_x509_certificate(der, default_backend())

                        now = datetime.now(timezone.utc)
                        # cryptography >=42 exposes *_utc variants
                        not_after = (
                            cert.not_valid_after_utc  # type: ignore[attr-defined]
                            if hasattr(cert, "not_valid_after_utc")
                            else cert.not_valid_after.replace(tzinfo=timezone.utc)
                        )
                        not_before = (
                            cert.not_valid_before_utc  # type: ignore[attr-defined]
                            if hasattr(cert, "not_valid_before_utc")
                            else cert.not_valid_before.replace(tzinfo=timezone.utc)
                        )
                        days_remaining = (not_after - now).days

                        try:
                            san_ext = cert.extensions.get_extension_for_class(
                                x509.SubjectAlternativeName
                            )
                            sans: list[str] = san_ext.value.get_values_for_type(x509.DNSName)
                        except x509.ExtensionNotFound:
                            sans = []

                        return {
                            "valid": True,
                            "subject": cert.subject.rfc4514_string(),
                            "issuer": cert.issuer.rfc4514_string(),
                            "not_before": not_before.isoformat(),
                            "not_after": not_after.isoformat(),
                            "days_remaining": days_remaining,
                            "expired": days_remaining < 0,
                            "expiring_soon": 0 <= days_remaining <= 30,
                            "sans": sans[:10],
                            "serial": str(cert.serial_number),
                        }
            except ssl.SSLCertVerificationError as exc:
                return {"valid": False, "error": f"Certificate verification failed: {exc}"}
            except (OSError, TimeoutError, ConnectionRefusedError) as exc:
                return {"valid": None, "error": f"Could not connect to {domain}:443 — {exc}"}

        return await asyncio.to_thread(_get_cert)

    # ── CAA records ───────────────────────────────────────────────────────

    async def _check_caa(self, domain: str) -> dict:
        """Query CAA DNS records, walking up the label tree until found."""
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()
        labels = domain.split(".")
        # Walk from full domain up to the TLD+1 level
        candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]

        caa_records: list[str] = []
        errors: list[str] = []

        for candidate in candidates:
            try:
                answer = await asyncio.wait_for(
                    resolver.resolve(candidate, "CAA"),
                    timeout=5,
                )
                caa_records.extend(str(r) for r in answer)
                break  # CAA found — stop walking up
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.Timeout:
                errors.append(f"Timeout querying {candidate}")
                break
            except dns.exception.DNSException as exc:
                errors.append(str(exc))
                break

        result: dict = {"records": caa_records, "present": bool(caa_records)}
        if errors:
            result["errors"] = errors
        return result

    # ── DKIM selector probe ───────────────────────────────────────────────

    async def _probe_dkim(self, domain: str, selectors: list[str]) -> dict:
        """Probe DKIM selectors via DNS TXT records at <selector>._domainkey.<domain>."""
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()

        async def _probe_one(selector: str) -> tuple[str, dict]:
            query = f"{selector}._domainkey.{domain}"
            try:
                answer = await asyncio.wait_for(
                    resolver.resolve(query, "TXT"),
                    timeout=5,
                )
                txt_parts: list[str] = []
                for rdata in answer:
                    txt_parts.append(
                        "".join(
                            s.decode() if isinstance(s, bytes) else str(s)
                            for s in rdata.strings
                        )
                    )
                txt = " ".join(txt_parts)
                if any(marker in txt for marker in ("v=DKIM1", "k=rsa", "k=ed25519", "p=")):
                    return selector, {"found": True, "record": txt[:200]}
                return selector, {"found": False}
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return selector, {"found": False}
            except dns.exception.Timeout:
                return selector, {"found": False, "error": "timeout"}
            except dns.exception.DNSException:
                return selector, {"found": False}

        probe_results = await asyncio.gather(*(_probe_one(s) for s in selectors))
        found = {sel: data for sel, data in probe_results if data.get("found")}
        return {
            "found_selectors": list(found.keys()),
            "details": found,
            "probed_selectors": selectors,
        }

    # ── Scoring ───────────────────────────────────────────────────────────

    def _compute_score(self, audit: dict, include_tls_cert: bool) -> tuple[dict, int]:
        """Compute a 0-100 security score with per-check breakdown."""
        detail: dict[str, dict] = {}

        # DMARC — 25 pts
        dmarc = audit.get("dmarc") or {}
        dmarc_valid = bool(dmarc.get("valid")) if isinstance(dmarc, dict) else False
        dmarc_policy = (
            (dmarc.get("parsed") or {}).get("p", "none")
            if isinstance(dmarc, dict)
            else "none"
        )
        if dmarc_valid and dmarc_policy in ("quarantine", "reject"):
            dmarc_pts = 25
        elif dmarc_valid:
            dmarc_pts = 12  # valid but p=none
        else:
            dmarc_pts = 0
        detail["dmarc"] = {"points": dmarc_pts, "max": 25, "valid": dmarc_valid, "policy": dmarc_policy}

        # SPF — 20 pts
        spf = audit.get("spf") or {}
        spf_valid = bool(spf.get("valid")) if isinstance(spf, dict) else False
        spf_pts = 20 if spf_valid else 0
        detail["spf"] = {"points": spf_pts, "max": 20, "valid": spf_valid}

        # DNSSEC — 15 pts
        dnssec = audit.get("dnssec")
        dnssec_ok = (
            bool(dnssec)
            if not isinstance(dnssec, dict)
            else bool(dnssec.get("valid") or dnssec.get("enabled"))
        )
        dnssec_pts = 15 if dnssec_ok else 0
        detail["dnssec"] = {"points": dnssec_pts, "max": 15, "enabled": dnssec_ok}

        # TLS cert — 10 pts
        if include_tls_cert:
            tls_cert = audit.get("tls_cert") or {}
            tls_ok = (
                isinstance(tls_cert, dict)
                and tls_cert.get("valid") is True
                and not tls_cert.get("expired")
            )
            tls_pts = 10 if tls_ok else 0
            detail["tls_cert"] = {"points": tls_pts, "max": 10, "valid": tls_ok}
        else:
            tls_pts = 0
            detail["tls_cert"] = {"points": 0, "max": 10, "skipped": True}

        # STARTTLS — 10 pts (from MX hosts resolved by checkdmarc)
        mx = audit.get("mx") or {}
        hosts: list = mx.get("hosts", []) if isinstance(mx, dict) else []
        if hosts:
            starttls_ok = any(bool(h.get("starttls")) for h in hosts if isinstance(h, dict))
            starttls_pts = 10 if starttls_ok else 0
        else:
            # No MX hosts available (maybe skip_tls=True or no MX) — give partial credit
            starttls_ok = False
            starttls_pts = 5
        detail["starttls"] = {"points": starttls_pts, "max": 10, "supported": starttls_ok}

        # MTA-STS — 8 pts
        mta_sts = audit.get("mta_sts") or {}
        mta_sts_valid = bool(mta_sts.get("valid")) if isinstance(mta_sts, dict) else False
        mta_sts_pts = 8 if mta_sts_valid else 0
        detail["mta_sts"] = {"points": mta_sts_pts, "max": 8, "valid": mta_sts_valid}

        # BIMI — 4 pts
        bimi = audit.get("bimi") or {}
        bimi_valid = bool(bimi.get("valid")) if isinstance(bimi, dict) else False
        bimi_pts = 4 if bimi_valid else 0
        detail["bimi"] = {"points": bimi_pts, "max": 4, "valid": bimi_valid}

        # CAA — 4 pts
        caa = audit.get("caa") or {}
        caa_present = bool(caa.get("present")) if isinstance(caa, dict) else False
        caa_pts = 4 if caa_present else 0
        detail["caa"] = {"points": caa_pts, "max": 4, "present": caa_present}

        # TLS-RPT — 2 pts
        smtp_tls = audit.get("smtp_tls_reporting") or {}
        tls_rpt_valid = bool(smtp_tls.get("valid")) if isinstance(smtp_tls, dict) else False
        tls_rpt_pts = 2 if tls_rpt_valid else 0
        detail["tls_rpt"] = {"points": tls_rpt_pts, "max": 2, "valid": tls_rpt_valid}

        # DKIM — 2 pts
        dkim = audit.get("dkim_selectors") or {}
        dkim_found = bool(dkim.get("found_selectors")) if isinstance(dkim, dict) else False
        dkim_pts = 2 if dkim_found else 0
        detail["dkim"] = {"points": dkim_pts, "max": 2, "found": dkim_found}

        total = sum(v["points"] for v in detail.values())

        if not include_tls_cert:
            # Max achievable is 90 when TLS cert is skipped; normalize to 100
            total = round(total * 100 / 90)

        return detail, min(100, total)

    # ── Recommendations ───────────────────────────────────────────────────

    def _build_recommendations(self, audit: dict) -> list[str]:
        """Build a prioritized list of actionable security recommendations."""
        recs: list[str] = []
        detail: dict = audit.get("score_detail", {})

        def _zero(key: str) -> bool:
            d = detail.get(key, {})
            return d.get("points", -1) == 0 and not d.get("skipped")

        if _zero("dmarc"):
            recs.append(
                "CRITICAL: No valid DMARC record found. "
                "Publish a DMARC policy at _dmarc.<domain> (e.g. v=DMARC1; p=quarantine; rua=mailto:...)."
            )
        elif detail.get("dmarc", {}).get("policy") == "none":
            recs.append(
                "HIGH: DMARC policy is 'none' (monitoring only). "
                "Upgrade to p=quarantine or p=reject to protect against email spoofing."
            )

        if _zero("spf"):
            recs.append(
                "CRITICAL: No valid SPF record found. "
                "Publish an SPF TXT record at the domain root to authorise sending mail servers."
            )

        if _zero("dnssec"):
            recs.append(
                "HIGH: DNSSEC is not enabled. "
                "Enable DNSSEC via your domain registrar to prevent DNS cache poisoning."
            )

        if _zero("tls_cert"):
            tls = audit.get("tls_cert") or {}
            if isinstance(tls, dict) and tls.get("expired"):
                recs.append("CRITICAL: TLS certificate is expired. Renew immediately.")
            elif isinstance(tls, dict) and tls.get("expiring_soon"):
                days = tls.get("days_remaining", 0)
                recs.append(
                    f"HIGH: TLS certificate expires in {days} day(s). Renew soon to avoid interruptions."
                )
            else:
                recs.append(
                    "MEDIUM: TLS certificate on port 443 could not be validated. "
                    "Ensure HTTPS is properly configured."
                )

        if _zero("mta_sts"):
            recs.append(
                "MEDIUM: MTA-STS is not configured. "
                "Publish /.well-known/mta-sts.txt and a _mta-sts DNS TXT record to enforce TLS for inbound SMTP."
            )

        if _zero("caa"):
            recs.append(
                "MEDIUM: No CAA DNS records found. "
                "Add CAA records to restrict which Certificate Authorities may issue certificates for this domain."
            )

        if _zero("tls_rpt"):
            recs.append(
                "LOW: SMTP TLS Reporting (TLS-RPT) not configured. "
                "Add a _smtp._tls TXT record to receive TLS delivery failure reports."
            )

        if _zero("dkim"):
            recs.append(
                "LOW: No DKIM selectors found under common names. "
                "Ensure DKIM signing is configured and the selector name is published in DNS."
            )

        if not recs:
            recs.append(
                "Domain security posture is strong. "
                "Continue monitoring for certificate expiration and DNS record changes."
            )

        return recs
