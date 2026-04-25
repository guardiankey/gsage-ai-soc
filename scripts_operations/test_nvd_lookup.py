#!/usr/bin/env python3
"""Diagnostic script for NVD API / nvdlib 404 issue.

Run inside the mcp-server container:
    docker compose exec mcp-server python scripts_operations/test_nvd_lookup.py

Or locally (with the venv activated):
    python scripts_operations/test_nvd_lookup.py

Each test is independent so failures don't abort the rest.
"""

import asyncio
import os
import socket
import sys
import traceback
import urllib.parse

CVE_ID = "CVE-2026-40175"
NVD_CVE_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
API_KEY = os.environ.get("TOOL_NVD_LOOKUP__API_KEY", "").strip() or None

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 0 — DNS resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_dns() -> None:
    banner("Test 0 — DNS resolution for services.nvd.nist.gov")
    try:
        addrs = socket.getaddrinfo("services.nvd.nist.gov", 443)
        ip = addrs[0][4][0]
        ok(f"Resolved to {ip}")
    except Exception as exc:
        fail(f"DNS resolution failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — httpx with Accept: application/json (our new code path)
# ─────────────────────────────────────────────────────────────────────────────

async def test_httpx_accept_json() -> None:
    banner("Test 1 — httpx GET with Accept: application/json")
    try:
        import httpx
    except ImportError:
        fail("httpx not installed")
        return

    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["apiKey"] = API_KEY
        info(f"Using API key: {API_KEY[:8]}...")
    else:
        info("No API key configured (TOOL_NVD_LOOKUP__API_KEY)")

    url = f"{NVD_CVE_URL}?cveId={urllib.parse.quote(CVE_ID, safe='')}"
    info(f"URL: {url}")
    info(f"Headers: {headers}")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        info(f"Status: {resp.status_code}")
        info(f"Response headers: {dict(resp.headers)}")
        resp.raise_for_status()
        data = resp.json()
        total = data.get("totalResults", "?")
        ok(f"totalResults={total}, first CVE id={data['vulnerabilities'][0]['cve']['id'] if data.get('vulnerabilities') else 'none'}")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — httpx with Content-Type: application/json (mimics nvdlib headers)
# ─────────────────────────────────────────────────────────────────────────────

async def test_httpx_content_type_json() -> None:
    banner("Test 2 — httpx GET with Content-Type: application/json (nvdlib headers)")
    try:
        import httpx
    except ImportError:
        fail("httpx not installed")
        return

    headers = {"content-type": "application/json"}
    if API_KEY:
        headers["apiKey"] = API_KEY

    url = f"{NVD_CVE_URL}?cveId={urllib.parse.quote(CVE_ID, safe='')}"
    info(f"URL: {url}")
    info(f"Headers: {headers}")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        info(f"Status: {resp.status_code}")
        if resp.status_code == 404:
            fail("Got 404 with Content-Type header — this confirms nvdlib header is the cause!")
        else:
            ok(f"Status {resp.status_code} — Content-Type header is NOT the issue")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — httpx with no special headers (baseline)
# ─────────────────────────────────────────────────────────────────────────────

async def test_httpx_no_headers() -> None:
    banner("Test 3 — httpx GET with no custom headers (baseline)")
    try:
        import httpx
    except ImportError:
        fail("httpx not installed")
        return

    url = f"{NVD_CVE_URL}?cveId={urllib.parse.quote(CVE_ID, safe='')}"
    info(f"URL: {url}")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
        info(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            ok(f"totalResults={data.get('totalResults')}")
        else:
            fail(f"Got {resp.status_code}")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — nvdlib.searchCVE without key/delay (simplest possible call)
# ─────────────────────────────────────────────────────────────────────────────

async def test_nvdlib_simple() -> None:
    banner("Test 4 — nvdlib.searchCVE(cveId=...) — no key, no delay")
    try:
        import nvdlib
        info(f"nvdlib version: {getattr(nvdlib, '__version__', 'unknown')}")
    except ImportError:
        fail("nvdlib not installed")
        return

    try:
        results = await asyncio.to_thread(nvdlib.searchCVE, cveId=CVE_ID)
        ok(f"Returned {len(results)} result(s): {[getattr(r, 'id', '?') for r in results]}")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — nvdlib.searchCVE with key (if available)
# ─────────────────────────────────────────────────────────────────────────────

async def test_nvdlib_with_key() -> None:
    banner("Test 5 — nvdlib.searchCVE(cveId=..., key=..., delay=0.7)")
    try:
        import nvdlib
    except ImportError:
        fail("nvdlib not installed")
        return

    if not API_KEY:
        info("Skipped — TOOL_NVD_LOOKUP__API_KEY not set")
        return

    try:
        results = await asyncio.to_thread(nvdlib.searchCVE, cveId=CVE_ID, key=API_KEY, delay=0.7)
        ok(f"Returned {len(results)} result(s): {[getattr(r, 'id', '?') for r in results]}")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — nvdlib with monkey-patched request logger
# ─────────────────────────────────────────────────────────────────────────────

async def test_nvdlib_intercept() -> None:
    banner("Test 6 — nvdlib intercepted: show exact URL and headers sent")
    try:
        import nvdlib
        import requests
        from unittest.mock import patch, MagicMock
    except ImportError as exc:
        fail(f"Missing dependency: {exc}")
        return

    captured: dict = {}

    original_get = requests.get

    def mock_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        info(f"  nvdlib requests.get url={url}")
        info(f"  nvdlib requests.get params={kwargs.get('params')!r}")
        info(f"  nvdlib requests.get headers={kwargs.get('headers')!r}")
        # Now actually call the real request
        return original_get(url, **kwargs)

    try:
        with patch("nvdlib.get.requests.get", side_effect=mock_get):
            results = await asyncio.to_thread(nvdlib.searchCVE, cveId=CVE_ID)
        ok(f"Returned {len(results)} result(s)")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"\nNVD Lookup Diagnostic — CVE: {CVE_ID}")
    print(f"Python: {sys.version}")
    print(f"API key: {'set' if API_KEY else 'not set'}")

    test_dns()
    await test_httpx_accept_json()
    await test_httpx_content_type_json()
    await test_httpx_no_headers()
    await test_nvdlib_simple()
    await test_nvdlib_with_key()
    await test_nvdlib_intercept()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
