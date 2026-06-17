"""Shared pytest configuration for ``custom_code`` live tool tests.

These suites exercise real custom tools against live external systems by
invoking the tool classes directly (``tool.execute(...)``), bypassing the
MCP/agent orchestration layer.  Because they hit real APIs they are opt-in:
every test is gated behind environment variables and skips cleanly when the
required configuration is absent.

Markers registered here (the project ``pytest.ini`` uses ``--strict-markers``,
so they must be declared somewhere pytest can see them):

``sei_live``
    Live read-only SEI-PEN tests. Require the ``SEI_*`` connection env vars.

``sei_write``
    Live SEI-PEN write tests that create **permanent** artifacts (the WSSEI
    API has no delete operation). Additionally gated behind
    ``SEI_ALLOW_WRITE=1``.
"""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "sei_live: live read-only SEI-PEN tests (require SEI_* env vars)",
    )
    config.addinivalue_line(
        "markers",
        "sei_write: live SEI-PEN write tests that create permanent artifacts "
        "(require SEI_ALLOW_WRITE=1)",
    )
