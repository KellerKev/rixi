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

# Who may call the server is decided by tokens. With more than one server behind one issuer,
# scope them, or a token for another server (or another tenant) works here too.
if [ -n "${RIXI_AUDIENCE:-}" ]; then
  args+=(--audience "$RIXI_AUDIENCE")
fi
if [ -n "${RIXI_REQUIRED_CLAIMS:-}" ]; then          # NAME=VALUE[,NAME=VALUE…]
  IFS=',' read -ra claims <<< "$RIXI_REQUIRED_CLAIMS"
  for claim in "${claims[@]}"; do
    args+=(--required-claim "$claim")
  done
fi
if [ "${RIXI_REQUIRE_EXP:-}" = "1" ]; then
  args+=(--require-exp)
fi
if [ -n "${RIXI_REVOKED_JTI_FILE:-}" ]; then
  args+=(--revoked-jti-file "$RIXI_REVOKED_JTI_FILE")
fi

# Only tokens are authentication. An AES key (RIXI_AES_KEY, or RIXI_KEY_SECRET for the key
# handshake) hides what is sent; it does not say who may send it.
have_auth=0
[ -n "${RIXI_PUBLIC_KEY:-}${RIXI_JWKS_URL:-}" ] && have_auth=1
have_key=0
[ -n "${RIXI_AES_KEY:-}${RIXI_KEY_SECRET:-}" ] && have_key=1

if [ "$have_key" -eq 1 ] && { [ "$have_auth" -eq 0 ] || [ "${RIXI_REQUIRE_ENCRYPTION:-}" = "1" ]; }; then
  # without tokens the key is all there is: at least refuse anything not sealed with it
  args+=(--require-encryption)
fi

if [ "$have_auth" -eq 0 ]; then
  if [ "${RIXI_ALLOW_INSECURE:-}" = "1" ]; then
    echo "⚠️  RIXI server starting WITHOUT TOKENS — anyone who reaches port $PORT can use it" >&2
    if [ "$have_key" -eq 1 ]; then
      echo "⚠️  (request bodies must be sealed with the AES key; that is not authentication)." >&2
    else
      echo "⚠️  and run code." >&2
    fi
    args+=(--insecure)
  else
    echo "refusing to start: no authentication configured. Set RIXI_PUBLIC_KEY or RIXI_JWKS_URL" >&2
    echo "(an AES key alone is not authentication), or RIXI_ALLOW_INSECURE=1 for open testing." >&2
    exit 1
  fi
fi

# Any extra args passed to `docker run … rixi-server <args>` are appended verbatim.
exec pixi run python rixi_server.py "${args[@]}" "$@"
