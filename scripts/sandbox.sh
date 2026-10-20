#!/usr/bin/env bash
# The sandbox: the dashboard on made-up data, sealed off from the world. See docs/development.md.
#
#   scripts/sandbox.sh [state] [port]               run it; open http://127.0.0.1:8870 (Ctrl-C stops it)
#   scripts/sandbox.sh shots [state] [out] [port]   start it, screenshot the key pages with layout checks, stop it
#   scripts/sandbox.sh check                        prove the fixture holds nothing real (static rules)
#
# States: fresh, reviews-off, busy, fleet, friends, family, expired, all (combine with commas: fleet,expired).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in
  shots)
    state="${2:-all}" out="${3:-/tmp/machine-shots/${2:-all}}" port="${4:-8870}"
    log="$(mktemp)"
    python3 "$here/sandbox/run.py" --state "$state" --port "$port" >"$log" 2>&1 &
    pid=$!
    trap 'kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null; rm -f "$log"' EXIT
    for _ in $(seq 60); do
      grep -q '^sandbox (' "$log" && break
      kill -0 "$pid" 2>/dev/null || { cat "$log"; exit 1; }
      sleep 0.5
    done
    sleep 10  # the Network hands over its waiting questions, and teammates answer the owner's
    node "$here/sandbox/shots.mjs" --url "http://127.0.0.1:$port" --out "$out" "${@:5}"
    ;;
  check)
    python3 "$here/sandbox/check_fixture.py"
    ;;
  -h|--help|help)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    exec python3 "$here/sandbox/run.py" --state "${1:-busy}" --port "${2:-8870}"
    ;;
esac
