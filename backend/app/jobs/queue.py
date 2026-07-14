"""RQ queue access.

The API process enqueues; a separate `rq worker` process (same code image)
executes. Job payloads are plain survey IDs — never live provider/storage
objects — so jobs survive restarts and serialize trivially.
"""

from __future__ import annotations

import logging

from .. import config

log = logging.getLogger(__name__)

QUEUE_NAME = "keyline"

# Multi-hour photogrammetry: generous RQ timeout (seconds).
JOB_TIMEOUT = 60 * 60 * 12


class QueueUnavailable(RuntimeError):
    pass


def get_queue():
    import redis
    from rq import Queue

    try:
        conn = redis.Redis.from_url(config.redis_url())
        conn.ping()
    except redis.exceptions.RedisError as exc:
        raise QueueUnavailable(
            f"Redis is unavailable at {config.redis_url()}: {exc}") from exc
    return Queue(QUEUE_NAME, connection=conn)


def enqueue_survey(survey_id: str) -> str:
    """Enqueue the photogrammetry job; returns the RQ job id."""
    q = get_queue()
    job = q.enqueue(
        "app.jobs.photogrammetry_job.run_survey",
        survey_id,
        job_timeout=JOB_TIMEOUT,
        result_ttl=86400,
        failure_ttl=7 * 86400,
    )
    return job.id
