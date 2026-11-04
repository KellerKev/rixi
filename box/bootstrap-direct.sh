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
#   RIXI_ACME_EMAIL     optional ACME account email
#   RIXI_ACME_CA        optional "zerossl" (Let's Encrypt as fallback) or an ACME directory URL;
#                       Let's Encrypt alone when unset
#   RIXI_SERVE_KIND     optional "ollama": serve a model behind /v1 instead of only running uploads
#   RIXI_SERVE_MODEL    the model to pull and serve (e.g. qwen3:8b)
#   RIXI_ENDPOINT_KEY   bearer key for /v1; only this box has it
#   RIXI_OLLAMA_VERSION pinned ollama release (e.g. v0.35.1)
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

# 0. persistent volumes -----------------------------------------------------------
# RIXI_VOLUMES = <disk id>:<name>+…  Each disk is found by its id, formatted only if it has no
# filesystem yet (a volume moving between boxes keeps its data), and mounted at
# /workspace/data/<name>, also after a stop/start (fstab, nofail).
if [ -n "${RIXI_VOLUMES:-}" ]; then
  mkdir -p /workspace/data
  IFS='+' read -ra rixi_vols <<< "$RIXI_VOLUMES"
  for entry in "${rixi_vols[@]}"; do
    vid="${entry%%:*}"; name="${entry##*:}"
    dev=""
    for _ in $(seq 1 60); do
      dev="$(ls /dev/disk/by-id/*"$vid"* 2>/dev/null | head -1)"
      [ -n "$dev" ] && break
      sleep 2
    done
    if [ -z "$dev" ]; then log "volume $vid ($name) did not appear"; continue; fi
    blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -q -L "rixi-${name:0:10}" "$dev"
    mkdir -p "/workspace/data/$name"
    uuid="$(blkid -s UUID -o value "$dev")"
    grep -q "UUID=$uuid " /etc/fstab || \
      echo "UUID=$uuid /workspace/data/$name ext4 defaults,nofail 0 2" >> /etc/fstab
    mountpoint -q "/workspace/data/$name" || mount "/workspace/data/$name"
    log "volume $name mounted at /workspace/data/$name"
  done
fi

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

# 3. optional: the model server ------------------------------------------------
if [ "${RIXI_SERVE_KIND:-}" = "ollama" ]; then
  log "installing ollama ${RIXI_OLLAMA_VERSION:-latest}"
  curl -fsSL --retry 5 https://ollama.com/install.sh | \
    OLLAMA_VERSION="${RIXI_OLLAMA_VERSION#v}" sh >/dev/null
  systemctl enable --now ollama
  # Pull in the background: a large model takes minutes and the box should already be
  # reachable (and reporting) while it downloads. The agent reports model_ready.
  nohup bash -c "until ollama pull '$RIXI_SERVE_MODEL'; do sleep 10; done" \
    >/var/log/rixi-model-pull.log 2>&1 &
fi

# 4. TLS on the box ------------------------------------------------------------
# Previews: https://<port>.<this name>/ for ports the server opens on purpose. Caddy and the
# server share a gate secret, so only Caddy can ask "may this name have a certificate?" and
# "may this request through?".
if [ ! -s /etc/rixi/preview.env ]; then
  {
    echo "RIXI_PREVIEW_DOMAIN=$RIXI_HOSTNAME"
    echo "RIXI_PREVIEW_GATE=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  } > /etc/rixi/preview.env
  chmod 600 /etc/rixi/preview.env
fi
. /etc/rixi/preview.env
mkdir -p /var/lib/caddy
{
  # The port is the first label of a preview name: as many labels from the right as the
  # box's own name has.
  port_label=$(echo "$RIXI_HOSTNAME" | tr '.' '\n' | wc -l)
  echo "{"
  echo "  admin off"
  [ -z "${RIXI_ACME_EMAIL:-}" ] || echo "  email $RIXI_ACME_EMAIL"
  # Every box and every opened preview port takes a certificate, more than Let's Encrypt's
  # weekly allowance for one domain once there are many boxes. Issuers are tried in order;
  # for ZeroSSL, Caddy fetches the account binding with the email, so no CA secret lives here.
  case "${RIXI_ACME_CA:-}" in
    ""|letsencrypt) ;;
    zerossl)
      echo "  cert_issuer acme {"
      echo "    dir https://acme.zerossl.com/v2/DV90"
      echo "  }"
      echo "  cert_issuer acme"
      ;;
    https://*)
      echo "  cert_issuer acme {"
      echo "    dir $RIXI_ACME_CA"
      echo "  }"
      echo "  cert_issuer acme"
      ;;
  esac
  echo "  on_demand_tls {"
  echo "    ask http://127.0.0.1:9000/preview/ask?gate=$RIXI_PREVIEW_GATE"
  echo "  }"
  echo "}"
  echo "*.$RIXI_HOSTNAME {"
  echo "  tls {"
  echo "    on_demand"
  echo "  }"
  echo "  forward_auth 127.0.0.1:9000 {"
  echo "    uri /preview/auth"
  echo "    header_up X-Rixi-Host {host}"
  echo "    header_up X-Rixi-Preview-Gate $RIXI_PREVIEW_GATE"
  echo "  }"
  echo "  reverse_proxy 127.0.0.1:{http.request.host.labels.$port_label} {"
  echo "    flush_interval -1"
  echo "  }"
  echo "}"
  echo "$RIXI_HOSTNAME {"
  # The preview gates answer Caddy on loopback only, never through the box's own name.
  echo "  @previewgate path /preview/ask /preview/auth"
  echo "  handle @previewgate {"
  echo "    respond 404"
  echo "  }"
  if [ -n "${RIXI_SERVE_KIND:-}" ]; then
    # One line per request, so the agent can count them and tell the gateway when the
    # endpoint last did any work (an idle endpoint should stop costing money).
    echo "  log {"
    echo "    output file /var/log/rixi/access.log"
    echo "    format json"
    echo "  }"
    # Two mutually exclusive handle blocks, NOT one block with a matcher and a fallback:
    # inside a single handle, Caddy runs directives in its own order and `respond` comes
    # before `reverse_proxy`, so the 401 would answer every request, key or not.
    echo "  @v1ok {"
    echo "    path /v1/*"
    echo "    header Authorization \"Bearer $RIXI_ENDPOINT_KEY\""
    echo "  }"
    echo "  handle @v1ok {"
    echo "    reverse_proxy 127.0.0.1:11434 {"
    # ollama refuses a request whose Host is not local, so present the upstream's own.
    echo "      header_up Host {upstream_hostport}"
    echo "      flush_interval -1"
    echo "    }"
    echo "  }"
    echo "  handle /v1/* {"
    echo "    respond \"unauthorized\" 401"
    echo "  }"
  fi
  echo "  handle {"
  echo "    reverse_proxy 127.0.0.1:9000 {"
  echo "      flush_interval -1"
  echo "    }"
  echo "  }"
  echo "}"
} > /etc/rixi/Caddyfile
mkdir -p /var/log/rixi

# 5. wait for our own name ----------------------------------------------------
# Caddy asks for a certificate as soon as it starts, and a failed challenge is cached by
# the CA. If the zone has a wildcard, an unpublished record resolves to the wildcard's
# host instead of this box, so wait until the name points at an address we actually hold.
own_ips="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
for _ in $(seq 1 60); do
  resolved="$(getent hosts "$RIXI_HOSTNAME" 2>/dev/null | awk '{print $1}' | head -1)"
  if [ -n "$resolved" ] && echo "$own_ips" | grep -qx "$resolved"; then
    log "$RIXI_HOSTNAME resolves here ($resolved)"
    break
  fi
  log "waiting for DNS: $RIXI_HOSTNAME -> ${resolved:-nothing} (ours: $(echo $own_ips | tr '\n' ' '))"
  sleep 10
done

# 6. services ----------------------------------------------------------------
# Model calls: every command, task and login on the box sees an OpenAI- (and, when given,
# Anthropic-) compatible endpoint and its key. Governed: a token only good for this box, swapped
# for the real key by a gateway. Direct: the customer's own endpoint and key.
: > /etc/rixi/workload.env
if [ -n "${RIXI_LLM_BASE_URL:-}" ] && [ -z "${RIXI_LLM_OPENAI_URL:-}" ]; then   # v0.5.0 form
  RIXI_LLM_OPENAI_URL="$RIXI_LLM_BASE_URL/v1"; RIXI_LLM_ANTHROPIC_URL="$RIXI_LLM_BASE_URL"
fi
if [ -n "${RIXI_LLM_TOKEN:-}" ]; then
  {
    echo "OPENAI_BASE_URL=$RIXI_LLM_OPENAI_URL"
    echo "OPENAI_API_KEY=$RIXI_LLM_TOKEN"
    if [ -n "${RIXI_LLM_ANTHROPIC_URL:-}" ]; then
      echo "ANTHROPIC_BASE_URL=$RIXI_LLM_ANTHROPIC_URL"
      echo "ANTHROPIC_API_KEY=$RIXI_LLM_TOKEN"
    fi
  } > /etc/rixi/workload.env
  echo 'set -a; . /etc/rixi/workload.env; set +a' > /etc/profile.d/rixi-llm.sh
fi
chmod 600 /etc/rixi/workload.env

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
EnvironmentFile=-/etc/rixi/workload.env
EnvironmentFile=-/etc/rixi/preview.env
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
