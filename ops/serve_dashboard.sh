#!/usr/bin/env bash
# Serves the dashboard over HTTP on :8080.
#
# Opening it as file:// gives the page a "null" origin, which the engine's CORS
# rule cannot match and some browsers refuse outright for fetch(). Serving it
# over http://localhost keeps the origin real and the preflight answerable.
set -euo pipefail
cd "$(dirname "$0")/../dashboard"
PORT="${DASHBOARD_PORT:-8080}"
echo "dashboard: http://localhost:$PORT/BTC_Perps_Dashboard.html"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
