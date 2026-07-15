#!/bin/sh
# Self-healing NodeODM tunnel.
#
# Runs a cloudflared quick tunnel to the local NodeODM and keeps the hosted
# backend pointed at it: quick-tunnel URLs change on every restart, so after
# the tunnel comes up (and every 5 minutes as a heartbeat, which also heals
# backend restarts that lose the override file) the current URL is pushed to
# the backend's token-guarded admin endpoint.
#
# Config file (KEYLINE_TUNNEL_CONF, default ~/.config/keyline/tunnel.env):
#   BACKEND_URL=https://keyline-backend.onrender.com
#   ADMIN_TOKEN=<same value as the ADMIN_TOKEN env var on the backend>
#   LOCAL_URL=http://localhost:3000        # optional
#   CLOUDFLARED=/path/to/cloudflared       # optional
#
# Run under systemd with Restart=always (see README) so a dead tunnel is
# replaced and re-announced automatically.
set -u

CONF="${KEYLINE_TUNNEL_CONF:-$HOME/.config/keyline/tunnel.env}"
if [ ! -f "$CONF" ]; then
    echo "config not found: $CONF" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$CONF"
: "${BACKEND_URL:?BACKEND_URL missing in $CONF}"
: "${ADMIN_TOKEN:?ADMIN_TOKEN missing in $CONF}"
LOCAL_URL="${LOCAL_URL:-http://localhost:3000}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"

LOG="$(mktemp /tmp/keyline-tunnel.XXXXXX.log)"
cleanup() {
    [ -n "${CFPID:-}" ] && kill "$CFPID" 2>/dev/null
    rm -f "$LOG"
}
trap cleanup EXIT INT TERM

"$CLOUDFLARED" tunnel --url "$LOCAL_URL" --no-autoupdate >"$LOG" 2>&1 &
CFPID=$!

URL=""
i=0
while [ $i -lt 60 ]; do
    URL="$(grep -oh 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)"
    [ -n "$URL" ] && break
    kill -0 "$CFPID" 2>/dev/null || { echo "cloudflared exited early" >&2; exit 1; }
    i=$((i + 1))
    sleep 2
done
if [ -z "$URL" ]; then
    echo "tunnel URL never appeared" >&2
    exit 1
fi
echo "tunnel up: $URL"

announce() {
    curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
        -X POST "$BACKEND_URL/api/admin/provider-url" \
        -H "Content-Type: application/json" \
        -H "X-Admin-Token: $ADMIN_TOKEN" \
        -d "{\"url\": \"$URL\"}"
}

# announce with retries (the tunnel may need a moment to route)
i=0
while [ $i -lt 20 ]; do
    CODE="$(announce)"
    [ "$CODE" = "200" ] && { echo "backend now points at $URL"; break; }
    echo "announce attempt $((i + 1)) -> HTTP $CODE; retrying" >&2
    i=$((i + 1))
    sleep 15
done

# heartbeat: re-announce every 5 min while the tunnel WORKS (idempotent;
# also restores the override after a backend restart wipes ephemeral disk).
# A live cloudflared process is not enough — quick-tunnel hostnames can die
# while the process survives (e.g. after machine suspend), so probe the URL
# itself and exit for a supervisor restart when it stops routing.
FAILS=0
while kill -0 "$CFPID" 2>/dev/null; do
    sleep 300
    kill -0 "$CFPID" 2>/dev/null || break
    PROBE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$URL/info" || true)"
    if [ "$PROBE" != "200" ]; then
        FAILS=$((FAILS + 1))
        echo "tunnel probe failed ($PROBE), strike $FAILS/2" >&2
        if [ $FAILS -ge 2 ]; then
            echo "tunnel URL dead while process alive — restarting" >&2
            exit 1
        fi
        continue
    fi
    FAILS=0
    announce >/dev/null
done

echo "cloudflared died — exiting so the supervisor restarts us" >&2
exit 1
