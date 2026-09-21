#!/usr/bin/env bash
#
# rixi direct-mode box bootstrap. Run by cloud-init as root on a fresh box; reads /etc/rixi/box.env
# (written by the gateway's user-data):
#
#   RIXI_BOX_ID         this box's id; tokens must carry aud=box-<id>
#   RIXI_TENANT         owning tenant; tokens must carry tenant=<it>
#   RIXI_HOSTNAME       public name, e.g. b-<id>.run.example.com (DNS already points here)
#   RIXI_JWKS_URL       where the token signing keys are published
#   RIXI_HEARTBEAT_URL  gateway heartbeat endpoint
#   RIXI_BOX_SECRET     this box's own heartbeat credential (useless for any other box)
#   RIXI_REF / RIXI_REPO  pinned rixi release to install
#   RIXI_ACME_EMAIL     optional Let's Encrypt account email
#
# Result: Caddy terminates TLS for RIXI_HOSTNAME on this box and forwards to the rixi server on
# loopback, which only accepts tokens scoped to this box and tenant. Traffic never leaves the
# client↔box connection. A heartbeat agent reports liveness + running-task count to the gateway.
set -euo pipefail

set -a
# shellcheck disable=SC1091
. /etc/rixi/box.env
set +a
export HOME=/root
RIXI_DIR=/opt/rixi
log() { echo "[rixi-direct] $*"; }

# 1. pixi + caddy -------------------------------------------------------------
if [ ! -x "$HOME/.pixi/bin/pixi" ]; then
  log "installing pixi"
  curl -fsSL --retry 5 https://pixi.sh/install.sh | bash >/dev/null
fi
PIXI="$HOME/.pixi/bin/pixi"
export PATH="$HOME/.pixi/bin:$PATH"
"$PIXI" global install caddy >/dev/null
CADDY="$HOME/.pixi/bin/caddy"

# 2. rixi at the pinned ref ----------------------------------------------------
if [ ! -d "$RIXI_DIR/server" ]; then
  log "fetching rixi@$RIXI_REF"
  git clone --depth 1 -b "$RIXI_REF" "$RIXI_REPO" "$RIXI_DIR"
fi
"$PIXI" install --manifest-path "$RIXI_DIR/server/pixi.toml"
PY="$RIXI_DIR/server/.pixi/envs/default/bin/python"
touch /etc/rixi/revoked_jti
chmod 600 /etc/rixi/revoked_jti

# 3. TLS on the box ------------------------------------------------------------
mkdir -p /var/lib/caddy
{
  echo "{"
  echo "  admin off"
  [ -z "${RIXI_ACME_EMAIL:-}" ] || echo "  email $RIXI_ACME_EMAIL"
  echo "}"
  echo "$RIXI_HOSTNAME {"
  echo "  reverse_proxy 127.0.0.1:9000 {"
  echo "    flush_interval -1"
  echo "  }"
  echo "}"
} > /etc/rixi/Caddyfile

# 4. services ----------------------------------------------------------------
cat > /etc/systemd/system/rixi-server.service <<UNIT
[Unit]
Description=RIXI server (loopback; reached through Caddy)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$RIXI_DIR/server
Environment=PATH=$HOME/.pixi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=$HOME
ExecStart=$PY rixi_server.py --host 127.0.0.1 --port 9000 --jwks-url $RIXI_JWKS_URL --audience box-$RIXI_BOX_ID --required-claim tenant=$RIXI_TENANT --revoked-jti-file /etc/rixi/revoked_jti
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/rixi-caddy.service <<UNIT
[Unit]
Description=Caddy — TLS for $RIXI_HOSTNAME
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=HOME=/var/lib/caddy XDG_DATA_HOME=/var/lib/caddy XDG_CONFIG_HOME=/var/lib/caddy
ExecStart=$CADDY run --config /etc/rixi/Caddyfile --adapter caddyfile
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/rixi-box-agent.service <<UNIT
[Unit]
Description=RIXI box agent (heartbeat to the gateway)
After=rixi-server.service rixi-caddy.service
[Service]
Type=simple
EnvironmentFile=/etc/rixi/box.env
ExecStart=$PY $RIXI_DIR/box/rixi_box_agent.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now rixi-server.service rixi-caddy.service rixi-box-agent.service
log "ready: https://$RIXI_HOSTNAME (box-$RIXI_BOX_ID, tenant $RIXI_TENANT)"
