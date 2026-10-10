#!/usr/bin/env bash
# Profile the running service with py-spy (flame graph + top).
# Usage: scripts/profile.sh [seconds] [port]
set -euo pipefail

DURATION="${1:-30}"
PORT="${2:-8000}"

command -v py-spy >/dev/null || {
  echo "py-spy is required: pip install py-spy (or brew install py-spy)" >&2
  exit 1
}

PID=$(lsof -ti tcp:"$PORT" || true)
[ -z "$PID" ] && { echo "No process listening on port $PORT" >&2; exit 1; }

echo "Profiling PID $PID for ${DURATION}s (flame graph -> profile.svg, top -> profile.txt)"
py-spy record --pid "$PID" --duration "$DURATION" --rate 50 --output profile.svg --format flamegraph &
py-spy dump --pid "$PID" > profile.txt
wait
echo "Done. Open profile.svg in a browser."
