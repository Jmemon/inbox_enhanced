"""Celery application factory.

Single Celery app shared by workers and beat. Broker + backend both point at
REDIS_URL — using one redis instance is intentional (homepage spec, "redis:
three roles, one instance").

`task_always_eager` is flipped on in tests so jobs run synchronously inside
the test transaction; production runs against a real worker.
"""

import logging
import os
from celery import Celery

# Configure logging for the worker and beat processes. Each is a separate
# Python process that doesn't inherit the FastAPI main.py basicConfig call.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
from app.config import get_settings


def _build_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "inbox_enhanced",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=["app.workers.tasks", "app.workers.task_engine_tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Redelivery on worker death; we want the periodic poll to be best-effort.
        task_acks_late=False,
        broker_connection_retry_on_startup=True,
    )
    if os.getenv("CELERY_TASK_ALWAYS_EAGER") == "1":
        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True

    # Wire beat schedule from a separate module so beat-only restart paths can
    # import it without pulling task code.
    from app.workers.beat_schedule import beat_schedule
    app.conf.beat_schedule = beat_schedule
    return app


celery_app = _build_app()
