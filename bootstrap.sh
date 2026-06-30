#!/usr/bin/env bash
#
# rixi on-box bootstrap — installs the rixi server AND a tunnel agent on THIS host and dials a
# gateway, so a freshly-provisioned (firewalled) box becomes reachable with no inbound holes.
# Designed to be run from cloud-init user-data; everything comes from the environment:
#
#   RIXI_GATEWAY_URL    ws:// URL the tunnel agent dials (the gateway)        [required]
#   RIXI_NODE_ID        node id the agent presents at auth (e.g. a one-time   [required]
#                       registration token, so the gateway wires the right client)
#   RIXI_TUNNEL_SECRET  shared tunnel secret (channel encryption + auth)      [required]
#   RIXI_SERVER_PORT    loopback port the rixi server binds                   [default 9000]
#   RIXI_REF            git ref of the public rixi to install                 [default main]
#   RIXI_DIR            install location                                      [default ~/rixi]
#   RIXI_REPO           rixi git URL                                          [default github]
#   RIXI_KEY_SECRET     optional rixi handshake secret (passed to the server)
#
set -euo pipefail

# cloud-init's runcmd runs without a login environment, so HOME may be unset.
HOME="${HOME:-$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)}"
HOME="${HOME:-/root}"
export HOME

GATEWAY_URL="${RIXI_GATEWAY_URL:?set RIXI_GATEWAY_URL}"
NODE_ID="${RIXI_NODE_ID:?set RIXI_NODE_ID}"
TUNNEL_SECRET="${RIXI_TUNNEL_SECRET:?set RIXI_TUNNEL_SECRET}"
SERVER_PORT="${RIXI_SERVER_PORT:-9000}"
RIXI_REF="${RIXI_REF:-main}"
RIXI_DIR="${RIXI_DIR:-$HOME/rixi}"
RIXI_REPO="${RIXI_REPO:-https://github.com/KellerKev/rixi}"

log() { echo "[rixi-bootstrap] $*"; }

# 1. pixi -------------------------------------------------------------------
if command -v pixi >/dev/null 2>&1; then
  PIXI="$(command -v pixi)"
elif [ -x "$HOME/.pixi/bin/pixi" ]; then
  PIXI="$HOME/.pixi/bin/pixi"
else
  log "installing pixi…"
  curl -fsSL https://pixi.sh/install.sh | bash >/dev/null
  PIXI="$HOME/.pixi/bin/pixi"
fi

# 2. fetch the public rixi --------------------------------------------------
if [ ! -d "$RIXI_DIR/server" ]; then
  log "fetching rixi@$RIXI_REF → $RIXI_DIR"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 -b "$RIXI_REF" "$RIXI_REPO" "$RIXI_DIR"
  else
    mkdir -p "$RIXI_DIR"
    curl -fsSL "$RIXI_REPO/archive/$RIXI_REF.tar.gz" | tar -xz -C "$RIXI_DIR" --strip-components=1
  fi
fi

# 3. resolve environments ---------------------------------------------------
log "resolving server + tunnel environments (pixi install)…"
"$PIXI" install --manifest-path "$RIXI_DIR/server/pixi.toml"
"$PIXI" install --manifest-path "$RIXI_DIR/tunnel/pixi.toml"

# 4. secrets → 0600 env file ------------------------------------------------
ENV_FILE="$RIXI_DIR/rixi.env"
(
  umask 077
  : > "$ENV_FILE"
  printf 'RIXI_TUNNEL_SECRET=%s\n' "$TUNNEL_SECRET" >> "$ENV_FILE"
  [ -z "${RIXI_KEY_SECRET:-}" ] || printf 'RIXI_KEY_SECRET=%s\n' "$RIXI_KEY_SECRET" >> "$ENV_FILE"
)
chmod 600 "$ENV_FILE"

# 5. services: rixi server (loopback) + tunnel agent dialing the gateway -----
SERVER_EXEC="$PIXI run --manifest-path $RIXI_DIR/server/pixi.toml python rixi_server.py --port $SERVER_PORT"
TUNNEL_EXEC="$PIXI run --manifest-path $RIXI_DIR/tunnel/pixi.toml python rixi_tunnel.py connect --to $GATEWAY_URL --node-id $NODE_ID --target 127.0.0.1:$SERVER_PORT"

if command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" = "0" ]; then
    UNIT_DIR="/etc/systemd/system"; SC=(systemctl); WANTED="multi-user.target"
  else
    UNIT_DIR="$HOME/.config/systemd/user"; SC=(systemctl --user); WANTED="default.target"
    loginctl enable-linger "$(id -un)" 2>/dev/null || true
  fi
  mkdir -p "$UNIT_DIR"

  cat > "$UNIT_DIR/rixi-server.service" <<UNIT
[Unit]
Description=RIXI server (loopback)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$RIXI_DIR/server
EnvironmentFile=$ENV_FILE
ExecStart=$SERVER_EXEC
Restart=on-failure
RestartSec=3
[Install]
WantedBy=$WANTED
UNIT

  cat > "$UNIT_DIR/rixi-tunnel.service" <<UNIT
[Unit]
Description=RIXI tunnel agent (dials the gateway)
After=rixi-server.service
Wants=rixi-server.service
[Service]
Type=simple
WorkingDirectory=$RIXI_DIR/tunnel
EnvironmentFile=$ENV_FILE
ExecStart=$TUNNEL_EXEC
Restart=always
RestartSec=3
[Install]
WantedBy=$WANTED
UNIT

  "${SC[@]}" daemon-reload
  "${SC[@]}" enable --now rixi-server.service
  "${SC[@]}" enable --now rixi-tunnel.service
  log "started rixi-server + rixi-tunnel (systemd)"
else
  log "no systemd — starting both in the background"
  # shellcheck disable=SC2086,SC1090
  ( set -a; . "$ENV_FILE"; set +a
    cd "$RIXI_DIR/server" && nohup $SERVER_EXEC >"$RIXI_DIR/server.log" 2>&1 < /dev/null &
    sleep 2
    cd "$RIXI_DIR/tunnel" && nohup $TUNNEL_EXEC >"$RIXI_DIR/tunnel.log" 2>&1 < /dev/null & )
  log "started rixi-server + rixi-tunnel (background; logs in $RIXI_DIR)"
fi

log "done — node '$NODE_ID' dialing $GATEWAY_URL"
