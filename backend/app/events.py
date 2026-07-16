"""Server-Sent Events bridge for analysis-run progress.

No new infrastructure: the stream is a DB-poll loop over the authoritative
``analysis_runs`` row, so it shows exactly the same truth as the polling API
(which remains the fallback). Events are pushed when meaningful state
changes — stage transitions, progress, messages, warnings, health verdict —
and a comment keepalive proves liveness in between. Terminal states end the
stream; clients that lose the stream simply resume polling.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Callable

from . import db
from .progress import TERMINAL_STATES

# Fields whose values change on every read (derived from wall-clock age);
# they must not count as "the run changed" or the stream would never idle.
_VOLATILE_FIELDS = {
    "elapsed_seconds", "stage_elapsed_seconds", "seconds_since_heartbeat",
    "seconds_since_progress",
}

POLL_INTERVAL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0
MAX_STREAM_SECONDS = 4 * 3600  # photogrammetry-scale ceiling; client reconnects
_SWEEP_EVERY_TICKS = 30


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _change_key(payload: dict) -> str:
    stable = {k: v for k, v in payload.items() if k not in _VOLATILE_FIELDS}
    return json.dumps(stable, sort_keys=True)


async def run_event_stream(
        pid: str, rid: str,
        serialize: Callable[[dict], dict]) -> AsyncIterator[str]:
    """Yields SSE frames for one analysis run until it reaches a terminal
    state, the run disappears, or the stream ceiling is hit. All DB access
    happens off the event loop."""
    from . import config

    stale_after = config.analysis_worker_lost_seconds()
    started = time.monotonic()
    last_key: str | None = None
    last_emit = 0.0
    tick = 0
    while time.monotonic() - started < MAX_STREAM_SECONDS:
        if tick % _SWEEP_EVERY_TICKS == 0:
            await asyncio.to_thread(db.sweep_stale_running_runs, stale_after)
        run = await asyncio.to_thread(db.get_analysis_run, rid)
        if run is None or run.get("project_id") != pid:
            yield _sse("gone", {"run_id": rid})
            return
        # serialize may consult the queue backend (worker status) — keep it
        # off the event loop as well
        payload = await asyncio.to_thread(serialize, run)
        key = _change_key(payload)
        now = time.monotonic()
        if key != last_key:
            last_key = key
            last_emit = now
            yield _sse("run", payload)
            if run.get("state") in TERMINAL_STATES:
                yield _sse("end", {"run_id": rid, "state": run["state"]})
                return
        elif now - last_emit >= KEEPALIVE_SECONDS:
            last_emit = now
            yield ": keepalive\n\n"
        tick += 1
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    yield _sse("timeout", {"run_id": rid})
