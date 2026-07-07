#!/usr/bin/env bash
# Tear down the dummy box's local processes.
here="$(cd "$(dirname "$0")" && pwd)"
for f in .spot.pid .tunnel.pid .echo.pid; do
  [ -f "$here/$f" ] || continue
  pid="$(cat "$here/$f")"
  kill "$pid" 2>/dev/null || true
  rm -f "$here/$f"
done
rm -f "$here/.echo.port"
echo "dummy box destroyed"
