#!/bin/bash
# rixi box bootstrap — rendered into cloud user-data by `rixi up` and run once at first boot.
#
# Installs pixi + the rixi server (pinned to the client's release), then runs it as a systemd
# service on an open port, over TLS with the certificate `rixi up` made for this box (the client
# trusts that certificate and no other, so nothing on the way can read or replay a request).
# Nothing on this port answers without a valid JWT signed by the key that `rixi up` generated on
# the client; request bodies are AES-GCM sealed as well, with a key the client negotiates right
# after boot (one-use handshake secret). Progress is logged to /var/log/rixi-bootstrap.log.
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
cat > /etc/rixi/tls_cert.pem <<'RIXI_PEM_EOF'
__RIXI_TLS_CERT__
RIXI_PEM_EOF
cat > /etc/rixi/tls_key.pem <<'RIXI_PEM_EOF'
__RIXI_TLS_KEY__
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
# pixi, pinned: the release archive itself, checked against the hash recorded here — not an
# installer script fetched and run as root on the day the box boots.
PIXI_VERSION="${PIXI_VERSION:-v0.70.1}"
install_pixi() {  # $1 = the directory the pixi binary goes into
  local arch sum tmp
  case "$(uname -m)" in
    x86_64|amd64)  arch=x86_64-unknown-linux-musl
                   sum=09692fb29be000cde10ad3fbf61edb1d147cfb6ad539d365a573408615b774d1 ;;
    aarch64|arm64) arch=aarch64-unknown-linux-musl
                   sum=304c81982b14780230ca9fed6e9796ddc268c41a8883a27219595d57871ccd2c ;;
    *) echo "pixi: unsupported architecture $(uname -m)" >&2; return 1 ;;
  esac
  # another release has another hash: say which
  [ "$PIXI_VERSION" = "v0.70.1" ] || sum="${PIXI_SHA256:?set PIXI_SHA256 to the hash of pixi $PIXI_VERSION for this architecture}"
  tmp="$(mktemp -d)"
  curl -fsSL --retry 5 -o "$tmp/pixi.tar.gz" \
    "https://github.com/prefix-dev/pixi/releases/download/$PIXI_VERSION/pixi-$arch.tar.gz"
  echo "$sum  $tmp/pixi.tar.gz" | sha256sum -c - >/dev/null \
    || { echo "pixi: the download does not match its recorded hash — not installing it" >&2; rm -rf "$tmp"; return 1; }
  mkdir -p "$1"
  tar -xzf "$tmp/pixi.tar.gz" -C "$1" pixi
  chmod 755 "$1/pixi"
  rm -rf "$tmp"
}
install_pixi /opt/pixi/bin
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
  --tls-cert /etc/rixi/tls_cert.pem --tls-key /etc/rixi/tls_key.pem \
  --key-secret-uses 1 --require-encryption --log-dir /var/log/rixi
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now rixi-server
echo "[rixi] bootstrap done $(date -u +%FT%TZ) — rixi-server listening on :$RIXI_PORT"
