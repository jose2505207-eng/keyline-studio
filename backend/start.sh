#!/bin/sh
# Container entrypoint.
#
# WORKER_EMBEDDED=1 additionally starts the RQ worker as a *separate process*
# in this container. That is meant for constrained single-container hosts
# (e.g. a free-tier web instance that cannot run a second service); real
# deployments and docker-compose run the worker as its own service.
set -e

if [ "$WORKER_EMBEDDED" = "1" ] && [ -n "$REDIS_URL" ]; then
    echo "starting embedded rq worker (WORKER_EMBEDDED=1)"
    rq worker keyline --url "$REDIS_URL" &
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
