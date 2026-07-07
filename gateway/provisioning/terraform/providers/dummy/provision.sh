#!/usr/bin/env bash
# Dummy "provision": start a local echo server + the open rixi-tunnel connect dialing the gateway
# with the one-time token as node_id. Backgrounded + detached; pids saved for destroy.
set -euo pipefail
: "${PYTHON:?}" "${RIXI_DIR:?}" "${GATEWAY_WS:?}" "${NODE_ID:?}" "${SECRET:?}"

here="$(cd "$(dirname "$0")" && pwd)"
portf="$here/.echo.port"
rm -f "$portf"

nohup "$PYTHON" "$here/echo_server.py" "$portf" >"$here/echo.log" 2>&1 < /dev/null &
echo $! > "$here/.echo.pid"

for _ in $(seq 1 100); do [ -s "$portf" ] && break; sleep 0.1; done
[ -s "$portf" ] || { echo "echo server failed to start; see echo.log" >&2; cat "$here/echo.log" >&2 || true; exit 1; }
port="$(cat "$portf")"

salt_args=()
[ -n "${SALT:-}" ] && salt_args=(--kdf-salt "$SALT")   # v2 per-deployment salt (must match gateway)
nohup "$PYTHON" "$RIXI_DIR/tunnel/rixi_tunnel.py" connect \
  --to "$GATEWAY_WS" --node-id "$NODE_ID" --secret "$SECRET" \
  "${salt_args[@]+"${salt_args[@]}"}" --target "127.0.0.1:$port" \
  >"$here/tunnel.log" 2>&1 < /dev/null &
echo $! > "$here/.tunnel.pid"

echo "dummy box up: echo on 127.0.0.1:$port, tunnel dialing $GATEWAY_WS as $NODE_ID"

# Spot interruption simulation: after SPOT_TTL seconds, kill the tunnel + echo so the box "vanishes"
# (the gateway sees the tunnel drop and runs its preemption handler). Deterministic + local.
if [ "${SPOT:-false}" = "true" ] && [ "${SPOT_TTL:-0}" -gt 0 ] 2>/dev/null; then
  nohup bash -c "sleep ${SPOT_TTL}; kill \$(cat '$here/.tunnel.pid') \$(cat '$here/.echo.pid') 2>/dev/null" \
    >/dev/null 2>&1 < /dev/null &
  echo $! > "$here/.spot.pid"
  echo "dummy spot box: will self-terminate in ${SPOT_TTL}s (interruption simulation)"
fi
