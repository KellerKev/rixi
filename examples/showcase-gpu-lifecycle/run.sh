#!/usr/bin/env bash
# showcase-gpu-lifecycle/run.sh — drive the RIXI GPU lifecycle end to end.
#
# It automates the cleanly-scriptable steps (preflight → fine-tune → teardown) and prints the
# exact commands for the two interactive steps (serve behind a keep-alive task + the OpenAI proxy).
# The full narrative is in README.md.
#
# Usage:
#   RIXI_SERVER=https://gpu-box:9000 ./run.sh            # against a server you already have
#   RIXI_SERVER=https://gpu-box:9000 PROVISIONED=1 ./run.sh   # also tear the box down at the end
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RIXI_SERVER="${RIXI_SERVER:-}"
PROXY_PORT="${PROXY_PORT:-8002}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── preflight ────────────────────────────────────────────────────────────────
if [ -z "$RIXI_SERVER" ]; then
  echo "Set RIXI_SERVER to a reachable rixi server first. Get one with:" >&2
  echo "  $REPO/provision-scaleway-gpu.sh        # provisions a GPU, prints an SSH-tunnel command" >&2
  echo "  # then, e.g.:  export RIXI_SERVER=http://127.0.0.1:9000   (the tunnelled port)" >&2
  exit 1
fi
command -v rixi >/dev/null || { echo "rixi CLI not found — run: pip install rixi" >&2; exit 1; }
log "target server: $RIXI_SERVER"

# ── step 1: fine-tune on the GPU box (automated) ─────────────────────────────
step "1/4  QLoRA fine-tune on the remote GPU"
log "shipping examples/finetune-qlora + its env, running the 'finetune' task, streaming logs…"
rixi run --server "$RIXI_SERVER" --task finetune "$REPO/examples/finetune-qlora"

# ── steps 2-3: serve behind an OpenAI-compatible API (interactive) ───────────
step "2/4  Serve the model as a keep-alive task, then put the proxy in front"
cat <<EOF
Run these two (each stays in the foreground):

  # a) deploy the inference backend as a long-lived task — note the printed Task ID
  cd "$REPO/inference-server"
  pixi run --manifest-path "$REPO/clients/pixi.toml" \\
    python "$REPO/clients/rixi_client.py" --server "$RIXI_SERVER" --task start --keep-alive

  # b) front that task with the OpenAI-compatible proxy (use the Task ID from step a)
  cd "$REPO/proxy"
  pixi run proxy -- --backend "$RIXI_SERVER" --inference-task <TASK_ID> --port $PROXY_PORT
EOF

step "3/4  Call your fine-tuned model like any OpenAI endpoint"
cat <<EOF
  curl http://localhost:$PROXY_PORT/v1/chat/completions \\
    -H 'content-type: application/json' \\
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"hello"}]}'
EOF

# ── step 4: tear the box down so billing stops ───────────────────────────────
step "4/4  Tear down"
if [ "${PROVISIONED:-}" = "1" ]; then
  log "destroying the provisioned GPU box (stops billing)…"
  "$REPO/provision-scaleway-gpu.sh" --destroy
else
  echo "When done, stop billing with:  $REPO/provision-scaleway-gpu.sh --destroy" >&2
fi

log "done."
