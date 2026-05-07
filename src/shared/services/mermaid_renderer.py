"""gSage AI — Shared Mermaid renderer.

Thin async wrapper around the ``@mermaid-js/mermaid-cli`` (``mmdc``) binary.

Originally lived inside ``src/mcp_server/tools/core/mermaid_validate.py``.
Moved here so the Teams handler (in ``backend_api``) can render Mermaid
blocks to PNGs inline, without depending on the ``mcp_server`` runtime.

The function is *pure* (no DB, no MinIO) — callers decide what to do with
the resulting PNG bytes (store in MinIO, return as Bot Framework
attachment, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Puppeteer / Chromium launch flags. Written to a fresh config file per
# invocation so we can also inject a per-call ``--user-data-dir`` (chromium
# refuses to share a profile across concurrent processes).
#
# --no-sandbox / --disable-setuid-sandbox: required because the host process
#   typically runs as a non-root user inside a container without user
#   namespace support.
# --disable-dev-shm-usage: containers ship a tiny /dev/shm (default 64MB) that
#   Chromium can exhaust, causing renderer crashes.
# --disable-gpu / --disable-crash-reporter / --disable-breakpad: silence the
#   crashpad/breakpad subsystem which would otherwise abort with
#   "chrome_crashpad_handler: --database is required" when HOME is unset.
#
# Why no ``--no-zygote``: it forces chromium to bootstrap crashpad eagerly in
# the parent process, which is exactly the path that fails inside the
# container; the default zygote model + writable user-data-dir works.
# ---------------------------------------------------------------------------
PUPPETEER_CHROME_ARGS_BASE: list[str] = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-crash-reporter",
    "--disable-breakpad",
]


async def run_mmdc(
    *,
    diagram_text: str,
    mmdc_bin: str,
    want_png: bool,
    timeout: int,
    scale: int = 3,
) -> tuple[str, str, int, Optional[bytes]]:
    """Run ``mmdc`` on ``diagram_text`` and return ``(stdout, stderr, rc, png)``.

    Writes the input to a temporary ``.mmd`` file and (optionally) reads the
    produced PNG. A throw-away Chromium profile + Puppeteer config is built
    per invocation so concurrent calls don't collide on the user-data-dir
    lock and chromium's crashpad has a writable database path.
    Temporary files / dirs are always cleaned up.

    Parameters
    ----------
    diagram_text:
        Mermaid source WITHOUT surrounding ``` fences.
    mmdc_bin:
        Absolute path to (or PATH-resolvable name of) the ``mmdc`` binary.
    want_png:
        When ``True`` and the diagram is valid, the rendered PNG bytes are
        returned in the fourth tuple slot.
    timeout:
        Hard subprocess timeout in seconds.
    scale:
        ``mmdc --scale`` factor. Higher → bigger PNG. Default 3 matches the
        legacy ``mermaid_validate`` tool; Teams attachments use 2 to fit
        the 4 MB-per-activity envelope.
    """
    tmp_in = tempfile.NamedTemporaryFile(
        suffix=".mmd", mode="w", delete=False, encoding="utf-8"
    )
    tmp_out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    chrome_home = tempfile.mkdtemp(prefix="gsage-chrome-")
    user_data_dir = os.path.join(chrome_home, "profile")
    os.makedirs(user_data_dir, exist_ok=True)
    puppeteer_config_path = os.path.join(chrome_home, "puppeteer.json")
    Path(puppeteer_config_path).write_text(
        json.dumps(
            {
                "args": [
                    *PUPPETEER_CHROME_ARGS_BASE,
                    f"--user-data-dir={user_data_dir}",
                ]
            }
        ),
        encoding="utf-8",
    )
    # Force HOME / XDG_* into a writable location: the runtime user often has
    # no home directory, which is what triggers the crashpad failure.
    sub_env = {
        **os.environ,
        "HOME": chrome_home,
        "XDG_CONFIG_HOME": chrome_home,
        "XDG_CACHE_HOME": chrome_home,
        "TMPDIR": chrome_home,
    }
    try:
        tmp_in.write(diagram_text)
        tmp_in.close()
        tmp_out.close()

        proc = await asyncio.create_subprocess_exec(
            mmdc_bin,
            "-i", tmp_in.name,
            "-o", tmp_out.name,
            "--scale", str(scale),
            "--puppeteerConfigFile", puppeteer_config_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode or 0

        png_bytes: Optional[bytes] = None
        if want_png and rc == 0:
            try:
                with open(tmp_out.name, "rb") as f:
                    png_bytes = f.read()
            except OSError:
                png_bytes = None

        return stdout, stderr, rc, png_bytes
    finally:
        for path in (tmp_in.name, tmp_out.name):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        # Best-effort cleanup of the per-invocation chromium profile.
        try:
            import shutil

            shutil.rmtree(chrome_home, ignore_errors=True)
        except Exception:
            pass
