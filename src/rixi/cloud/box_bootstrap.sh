#!/bin/bash
# rixi box bootstrap — rendered into cloud user-data by `rixi up` and run once at first boot.
#
# Installs pixi + the rixi server (pinned to the client's release), then runs it as a systemd
# service on an open port. Nothing on this port answers without a valid JWT signed by the key
# that `rixi up` generated on the client; request bodies must be AES-GCM sealed with a key the
# client negotiates over RSA right after boot (one-use handshake secret). Progress is logged to
# /var/log/rixi-bootstrap.log.
set -euo pipefail
exec > >(tee -a /var/log/rixi-bootstrap.log) 2>&1
echo "[rixi] bootstrap start $(date -u +%FT%TZ)"

export HOME=/root
RIXI_REF='__RIXI_REF__'
RIXI_PORT='__RIXI_PORT__'
RIXI_AUDIENCE='__RIXI_AUDIENCE__'
RIXI_REPO='https://github.com/KellerKev/rixi'

# ── secrets + auth material (0600, root only) ─────────────────────────────
install -d -m 700 /etc/rixi
umask 077
cat > /etc/rixi/jwt_pub.pem <<'RIXI_PEM_EOF'
__RIXI_JWT_PUBLIC_KEY__
RIXI_PEM_EOF
printf 'RIXI_KEY_SECRET=%s\n' '__RIXI_KEY_SECRET__' > /etc/rixi/rixi.env
umask 022

# ── optional SSH access for debugging ────────────────────────────────────
SSH_PUBKEY='__RIXI_SSH_PUBKEY__'
if [ -n "$SSH_PUBKEY" ]; then
  install -d -m 700 /root/.ssh
  grep -qxF "$SSH_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null \
    || echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

# ── base packages ────────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  for i in 1 2 3 4 5; do
    apt-get update -qq && apt-get install -y -qq curl ca-certificates tar && break
    echo "[rixi] apt busy, retrying ($i)"; sleep 10
  done
fi

# ── pixi ─────────────────────────────────────────────────────────────────
export PIXI_HOME=/opt/pixi
curl -fsSL https://pixi.sh/install.sh | PIXI_NO_PATH_UPDATE=1 bash
PIXI=/opt/pixi/bin/pixi
"$PIXI" --version

# ── rixi server, pinned ──────────────────────────────────────────────────
install -d /opt/rixi
curl -fsSL "$RIXI_REPO/archive/$RIXI_REF.tar.gz" | tar -xz -C /opt/rixi --strip-components=1
cd /opt/rixi/server
"$PIXI" install

cat > /etc/systemd/system/rixi-server.service <<UNIT
[Unit]
Description=rixi server (rixi up)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/rixi/server
EnvironmentFile=/etc/rixi/rixi.env
Environment=HOME=/root
Environment=PIXI_HOME=/opt/pixi
Environment=PATH=/opt/pixi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PIXI run python rixi_server.py --host 0.0.0.0 --port $RIXI_PORT \
  --public-key /etc/rixi/jwt_pub.pem --audience $RIXI_AUDIENCE --require-exp \
  --key-secret-uses 1 --require-encryption --log-dir /var/log/rixi
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now rixi-server
echo "[rixi] bootstrap done $(date -u +%FT%TZ) — rixi-server listening on :$RIXI_PORT"
