#!/usr/bin/env bash
# provision-scaleway-gpu.sh — create a Scaleway GPU instance and install the RIXI server on it.
#
# A repeatable companion to install-rixi.sh: it creates the VM, registers an SSH key, then hands
# off to install-rixi.sh. Paste your Scaleway API key + secret below (or export them), then run:
#     ./provision-scaleway-gpu.sh            # create the GPU + install RIXI, print the URL
#     ./provision-scaleway-gpu.sh --destroy  # delete the instance (stops billing)
#
# By default the server runs OPEN (--insecure, no auth) for quick testing. For a real deployment,
# set RIXI_SERVER_ARGS to forward JWT/AES flags, e.g.:
#     RIXI_SERVER_ARGS="--host 0.0.0.0 --public-key /path/jwt.pub --aes-key /path/aes.key"
set -euo pipefail

# ─────────────── paste your Scaleway credentials here (or export them) ───────────────
SCW_ACCESS_KEY="${SCW_ACCESS_KEY:-PASTE_ACCESS_KEY_HERE}"
SCW_SECRET_KEY="${SCW_SECRET_KEY:-PASTE_SECRET_KEY_HERE}"

# ─────────────── what to create (override via env) ───────────────
ZONE="${ZONE:-fr-par-1}"                  # falls back to fr-par-2 if out of stock
GPU_TYPE="${GPU_TYPE:-L4-1-24G}"          # a 24GB GPU comfortably fits a 7B QLoRA fine-tune
IMAGE="${IMAGE:-ubuntu_jammy_gpu_os_12}"  # Ubuntu 22.04 + NVIDIA drivers
ROOT_GB="${ROOT_GB:-100}"
NAME="${NAME:-rixi-gpu}"
RIXI_PORT="${RIXI_PORT:-9000}"
RIXI_SERVER_ARGS="${RIXI_SERVER_ARGS:---host 0.0.0.0 --insecure}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
RIXI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this repo
PIXI="${PIXI:-$(command -v pixi || echo "$HOME/.pixi/bin/pixi")}"
export PATH="$(dirname "$PIXI"):$PATH"

log(){ printf '\033[36m==>\033[0m %s\n' "$*" >&2; }
err(){ printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

[ "$SCW_ACCESS_KEY" != "PASTE_ACCESS_KEY_HERE" ] || { err "set SCW_ACCESS_KEY (paste at the top or export it)"; exit 1; }
command -v scw >/dev/null || { log "installing scaleway-cli…"; "$PIXI" global install scaleway-cli >/dev/null; }

# resolve org/project from the API key so you only need access + secret
log "resolving organization/project from the API key…"
PROJECT_ID=$(curl -s -H "X-Auth-Token: $SCW_SECRET_KEY" "https://api.scaleway.com/iam/v1alpha1/api-keys/$SCW_ACCESS_KEY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('default_project_id') or '')")
[ -n "$PROJECT_ID" ] || { err "could not resolve project — check the API key/secret"; exit 1; }
ORG_ID=$(curl -s -H "X-Auth-Token: $SCW_SECRET_KEY" "https://api.scaleway.com/account/v3/projects/$PROJECT_ID" | python3 -c "import sys,json;print(json.load(sys.stdin).get('organization_id') or '')")
export SCW_ACCESS_KEY SCW_SECRET_KEY
export SCW_DEFAULT_ORGANIZATION_ID="$ORG_ID" SCW_DEFAULT_PROJECT_ID="$PROJECT_ID"
export SCW_DEFAULT_ZONE="$ZONE" SCW_DEFAULT_REGION="${ZONE%-*}"

find_server(){ for z in "$ZONE" fr-par-1 fr-par-2; do
    local id; id=$(scw instance server list name="$NAME" zone="$z" -o json 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else '')")
    [ -n "$id" ] && { echo "$id $z"; return; }; done; }

# ─────────────── teardown ───────────────
if [ "${1:-}" = "--destroy" ]; then
  read -r sid szone < <(find_server) || true
  [ -z "${sid:-}" ] && { log "no instance named '$NAME' found."; exit 0; }
  log "terminating '$NAME' ($sid, $szone) incl. its IP + volumes…"
  scw instance server terminate "$sid" zone="$szone" with-ip=true with-block=true
  exit 0
fi

# ─────────────── ssh key ───────────────
[ -f "$SSH_KEY" ] || { log "generating SSH key $SSH_KEY…"; ssh-keygen -t ed25519 -N '' -f "$SSH_KEY" -C rixi-gpu >/dev/null; }
scw iam ssh-key create name="rixi-gpu" public-key="$(cat "$SSH_KEY.pub")" >/dev/null 2>&1 || log "(SSH key already registered)"

# ─────────────── create (or reuse) the GPU ───────────────
read -r SID SZONE < <(find_server) || true
if [ -z "${SID:-}" ]; then
  log "creating $GPU_TYPE in $ZONE (image=$IMAGE, root=${ROOT_GB}GB) — this is a PAID GPU…"
  create(){ scw instance server create name="$NAME" type="$GPU_TYPE" zone="$1" image="$IMAGE" \
              root-volume="block:${ROOT_GB}GB" ip=new project-id="$PROJECT_ID" -w -o json; }
  if ! create "$ZONE" >/tmp/scw_create.json 2>/tmp/scw_create.err; then
    if grep -qiE 'stock|exhausted|no server|not available' /tmp/scw_create.err; then
      log "$ZONE out of stock — retrying in fr-par-2…"; ZONE=fr-par-2; export SCW_DEFAULT_ZONE=$ZONE
      create fr-par-2 >/tmp/scw_create.json
    else cat /tmp/scw_create.err >&2; exit 1; fi
  fi
  SID=$(python3 -c "import json;print(json.load(open('/tmp/scw_create.json'))['id'])"); SZONE="$ZONE"
else log "reusing existing instance '$NAME' ($SID, $SZONE)"; fi

IP=$(scw instance server get "$SID" zone="$SZONE" -o json | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('public_ip') or {}).get('address') or (d.get('public_ips') or [{}])[0].get('address',''))")
log "instance up: $NAME @ $IP ($SZONE)"
ssh-keygen -R "$IP" >/dev/null 2>&1 || true   # drop any stale host key (Scaleway reuses IPs)

# ─────────────── wait for SSH ───────────────
log "waiting for SSH on $IP…"
for i in $(seq 1 60); do
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY" root@"$IP" true 2>/dev/null; then log "SSH ready"; break; fi
  sleep 5
done

# ─────────────── install + start RIXI ───────────────
log "installing RIXI server via install-rixi.sh…"
# shellcheck disable=SC2086
"$RIXI_DIR/install-rixi.sh" --host root@"$IP" --key "$SSH_KEY" --rixi-port "$RIXI_PORT" -- $RIXI_SERVER_ARGS

sleep 4
if curl -fs -m 10 "http://$IP:$RIXI_PORT/health" >/dev/null; then log "RIXI healthy ✅"; else err "RIXI not responding yet — check: ssh -i $SSH_KEY root@$IP 'systemctl --user status rixi'"; fi

echo
log "DONE ✅  RIXI server: http://$IP:$RIXI_PORT"
cat <<EOF

Use it by pointing your client at:  RIXI_SERVER=http://$IP:$RIXI_PORT
Tear down when done (stops billing):  $0 --destroy
EOF
