#!/usr/bin/env bash
# Entrypoint for the RIXI server container. Enforces secure-by-default: the server binds
# 0.0.0.0 inside the container (exposure is controlled by `docker -p`), but only starts
# unauthenticated when the operator explicitly opts in with RIXI_ALLOW_INSECURE=1.
set -euo pipefail

PORT="${RIXI_PORT:-9000}"
args=(--host 0.0.0.0 --port "$PORT")

if [ -n "${RIXI_PUBLIC_KEY:-}" ]; then
  args+=(--public-key "$RIXI_PUBLIC_KEY")
fi
if [ -n "${RIXI_JWKS_URL:-}" ]; then
  args+=(--jwks-url "$RIXI_JWKS_URL")
fi
if [ -n "${RIXI_AES_KEY:-}" ]; then
  args+=(--aes-key "$RIXI_AES_KEY")
fi
if [ -n "${RIXI_TLS_CERT:-}" ] && [ -n "${RIXI_TLS_KEY:-}" ]; then
  args+=(--tls-cert "$RIXI_TLS_CERT" --tls-key "$RIXI_TLS_KEY")
fi

have_auth=0
[ -n "${RIXI_PUBLIC_KEY:-}${RIXI_JWKS_URL:-}${RIXI_AES_KEY:-}${RIXI_KEY_SECRET:-}" ] && have_auth=1

if [ "$have_auth" -eq 0 ]; then
  if [ "${RIXI_ALLOW_INSECURE:-}" = "1" ]; then
    echo "⚠️  RIXI server starting OPEN/INSECURE — anyone who reaches port $PORT can run code." >&2
    args+=(--insecure)
  else
    echo "refusing to start: no auth configured. Set one of RIXI_PUBLIC_KEY / RIXI_JWKS_URL /" >&2
    echo "RIXI_AES_KEY / RIXI_KEY_SECRET, or RIXI_ALLOW_INSECURE=1 for open testing." >&2
    exit 1
  fi
fi

# Any extra args passed to `docker run … rixi-server <args>` are appended verbatim.
exec pixi run python rixi_server.py "${args[@]}" "$@"
