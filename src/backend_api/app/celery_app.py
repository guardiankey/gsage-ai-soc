"""gSage AI — Celery application factory.

Usage::

    from src.backend_api.app.celery_app import celery_app

    # Register tasks in src/backend_api/app/tasks/*.py and run:
    #   celery -A src.backend_api.app.celery_app.celery_app worker -l info
"""

from __future__ import annotations

from celery import Celery

from src.shared.config.settings import get_settings


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
