"""gSage AI — Celery application factory.

Usage::

    from src.backend_api.app.celery_app import celery_app

    # Register tasks in src/backend_api/app/tasks/*.py and run:
    #   celery -A src.backend_api.app.celery_app.celery_app worker -l info
"""

from __future__ import annotations

import socket

from celery import Celery

from src.shared.config.settings import get_settings


def _tcp_keepalive_options() -> dict[int, int]:
    """Build TCP keepalive options dict using actual ``socket`` constants.

    redis-py iterates this mapping and calls
    ``sock.setsockopt(socket.IPPROTO_TCP, k, v)`` directly, so the keys must
    be the integer constants (e.g. ``socket.TCP_KEEPIDLE``) and not their
    string names. Some constants are platform-specific (TCP_KEEPIDLE only
    exists on Linux), so we probe each one defensively.
    """
    opts: dict[int, int] = {}
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts[socket.TCP_KEEPIDLE] = 60
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts[socket.TCP_KEEPINTVL] = 10
    if hasattr(socket, "TCP_KEEPCNT"):
        opts[socket.TCP_KEEPCNT] = 5
    return opts


def _make_celery() -> Celery:
    settings = get_settings()
    app = Celery(
        "gsage_ai",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=[
            "src.backend_api.app.tasks.maintenance",
            "src.backend_api.app.tasks.scheduled_job",
            "src.backend_api.app.tasks.ingest",
            "src.backend_api.app.tasks.email",
            "src.backend_api.app.tasks.background",
            "src.backend_api.app.tasks.agent_continuation",
            "src.backend_api.app.tasks.elasticsearch_ingest",
        ],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # ── Broker connection resilience ──────────────────────────────────
        # Retry connecting to the broker on worker startup (required in Celery 5+).
        broker_connection_retry_on_startup=True,
        # Retry indefinitely instead of dying after a few failed attempts.
        broker_connection_max_retries=None,
        # Transport-level options for Redis broker via kombu.
        # socket_keepalive prevents idle connections from being dropped by
        # firewalls / NAT / cloud load-balancers (common cause of the
        # _disconnect_raise_connect / readline() traceback in long-idle workers).
        broker_transport_options={
            # TCP keepalive — send a probe after 60 s of inactivity, retry every
            # 10 s up to 5 times before declaring the connection dead.
            "socket_keepalive": True,
            "socket_keepalive_options": _tcp_keepalive_options(),
            # Visibility timeout must be longer than the longest task.
            # Knowledge ingest of large archives can run up to ~15 min.
            "visibility_timeout": 18000,
            # Retry policy for transient broker connection errors.
            "retry_policy": {
                "timeout": 5.0,
            },
        },
        # Beat schedule — periodic tasks
        beat_schedule={
            "cleanup-inactive-sessions": {
                "task": "src.backend_api.app.tasks.maintenance.cleanup_inactive_sessions",
                "schedule": 3600.0,  # every hour
            },
            "prune-es-trace-indices": {
                "task": "src.backend_api.app.tasks.maintenance.prune_es_trace_indices",
                "schedule": 86400.0,  # every 24 hours
            },
            "purge-expired-files": {
                "task": "src.backend_api.app.tasks.maintenance.purge_expired_files",
                "schedule": 3600.0,  # every hour
            },
            "flush-es-buffer": {
                "task": "src.backend_api.app.tasks.elasticsearch_ingest.flush_es_buffer",
                "schedule": 60.0,  # every 60 seconds
                "options": {"queue": "elasticsearch"},
            },
        },
    )
    return app


celery_app = _make_celery()
