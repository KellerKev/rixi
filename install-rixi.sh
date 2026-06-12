#!/usr/bin/env bash
#
# install-rixi.sh — provision the RIXI server onto a remote Linux or macOS host over SSH.
#
# Runs from your control machine (macOS or Linux). It:
#   1. connects over SSH (key or password auth),
#   2. installs the Pixi package manager on the target if missing,
#   3. copies the local server/ directory to the target (rsync, scp fallback),
#   4. installs dependencies with pixi (adding the target's platform on macOS),
#   5. installs a user-level service (systemd --user on Linux, launchd LaunchAgent on
#      macOS) that runs rixi_server.py and restarts on failure,
#   6. starts it and prints status.
#
# No root/sudo is required on the target — everything is per-user.
#
# Usage:
#   ./install-rixi.sh --host user@1.2.3.4 [--key ~/.ssh/id_ed25519] [options] [-- <rixi server args>]
#
# Examples:
#   # Key auth, listen on default port 9000
#   ./install-rixi.sh --host deploy@10.0.0.5 --key ~/.ssh/id_ed25519
#
#   # Password auth (needs sshpass), custom listen port, JWT public key on the target
#   ./install-rixi.sh --host root@box --ask-pass --rixi-port 8443 -- --public-key /etc/rixi/jwt.pub
#
#   # Forward arbitrary server flags after "--"
#   ./install-rixi.sh --host me@host --key ~/.ssh/id_rsa -- --log-level DEBUG --key-secret s3cr3t
#
set -euo pipefail

# ----------------------------------------------------------------------------- defaults
SSH_HOST=""              # user@host or host
SSH_USER=""              # overrides the user part of --host
SSH_PORT="22"
SSH_KEY=""
SSH_PASSWORD=""
ASK_PASS=0
REMOTE_DIR=""            # default resolved on target to ~/rixi
SERVICE_NAME="rixi"
RIXI_PORT="9000"
START=1                  # 0 = install only, don't start
RIXI_EXTRA_ARGS=()       # everything after "--" forwarded to rixi_server.py

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_SERVER_DIR="$SCRIPT_DIR/server"

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

err() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
log() { printf '\033[36m==>\033[0m %s\n' "$*" >&2; }

# ----------------------------------------------------------------------------- parse args
while [ $# -gt 0 ]; do
  case "$1" in
    -H|--host)         SSH_HOST="$2"; shift 2 ;;
    -u|--user)         SSH_USER="$2"; shift 2 ;;
    -p|--ssh-port)     SSH_PORT="$2"; shift 2 ;;
    -k|--key)          SSH_KEY="$2"; shift 2 ;;
    -P|--password)     SSH_PASSWORD="$2"; shift 2 ;;
    --ask-pass)        ASK_PASS=1; shift ;;
    --remote-dir)      REMOTE_DIR="$2"; shift 2 ;;
    --service-name)    SERVICE_NAME="$2"; shift 2 ;;
    --rixi-port)       RIXI_PORT="$2"; shift 2 ;;
    --no-start)        START=0; shift ;;
    -h|--help)         usage 0 ;;
    --)                shift; RIXI_EXTRA_ARGS=("$@"); break ;;
    *) err "unknown option: $1"; usage 1 ;;
  esac
done

# ----------------------------------------------------------------------------- validate
[ -n "$SSH_HOST" ] || { err "--host is required"; usage 1; }
[ -d "$LOCAL_SERVER_DIR" ] || { err "server/ not found next to this script ($LOCAL_SERVER_DIR)"; exit 1; }

# Split user@host so an explicit --user wins.
if printf '%s' "$SSH_HOST" | grep -q '@'; then
  [ -n "$SSH_USER" ] || SSH_USER="${SSH_HOST%@*}"
  SSH_HOST="${SSH_HOST#*@}"
fi
[ -n "$SSH_USER" ] || { err "no SSH user given (use user@host or --user)"; exit 1; }

if [ "$ASK_PASS" -eq 1 ] && [ -z "$SSH_PASSWORD" ]; then
  printf 'SSH password for %s@%s: ' "$SSH_USER" "$SSH_HOST" >&2
  read -r -s SSH_PASSWORD; echo >&2
fi

# Build the SSH/SCP transport. Password auth uses sshpass; otherwise key/agent auth.
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -p "$SSH_PORT")
SCP_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -P "$SSH_PORT")
PASS_PREFIX=()
if [ -n "$SSH_KEY" ]; then
  [ -f "$SSH_KEY" ] || { err "key file not found: $SSH_KEY"; exit 1; }
  SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
  SCP_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
fi
if [ -n "$SSH_PASSWORD" ]; then
  command -v sshpass >/dev/null 2>&1 || {
    err "password auth needs 'sshpass' on this machine (brew install sshpass / apt install sshpass)"; exit 1; }
  PASS_PREFIX=(sshpass -e)   # reads SSHPASS from env (set per-invocation below)
fi

TARGET="$SSH_USER@$SSH_HOST"

run_ssh() {  # run a command string on the target
  if [ -n "$SSH_PASSWORD" ]; then
    SSHPASS="$SSH_PASSWORD" "${PASS_PREFIX[@]}" ssh "${SSH_OPTS[@]}" "$TARGET" "$@"
  else
    ssh "${SSH_OPTS[@]}" "$TARGET" "$@"
  fi
}

scp_push() {  # scp_push LOCAL_FILE REMOTE_PATH   (REMOTE_PATH may use ~ — expanded by remote)
  if [ -n "$SSH_PASSWORD" ]; then
    SSHPASS="$SSH_PASSWORD" "${PASS_PREFIX[@]}" scp "${SCP_OPTS[@]}" "$1" "$TARGET:$2"
  else
    scp "${SCP_OPTS[@]}" "$1" "$TARGET:$2"
  fi
}

fetch_to_control() {  # fetch_to_control URL OUTFILE  — download on THIS machine
  if command -v curl >/dev/null 2>&1; then
    curl -fSL --retry 3 "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    err "this control machine has neither curl nor wget to download the pixi bootstrap"; return 1
  fi
}

pixi_target() {  # echo the pixi release triple for the given "OS/ARCH", or empty if unknown
  case "$1" in
    Darwin/arm64|Darwin/aarch64) echo aarch64-apple-darwin ;;
    Darwin/x86_64)               echo x86_64-apple-darwin ;;
    Linux/aarch64|Linux/arm64)   echo aarch64-unknown-linux-musl ;;
    Linux/x86_64|Linux/amd64)    echo x86_64-unknown-linux-musl ;;
    Linux/riscv64)               echo riscv64gc-unknown-linux-gnu ;;
    *) echo "" ;;
  esac
}

# Push the self-contained pixi binary from the control machine to the target.
# Used when the target has no curl/wget, so the usual `curl | bash` bootstrap can't run.
# pixi carries its own HTTPS client, so after this the target needs no downloader at all.
push_pixi_binary() {
  local triple url tmp
  triple="$(pixi_target "$REMOTE_OS/$REMOTE_ARCH")"
  [ -n "$triple" ] || { err "no prebuilt pixi binary for $REMOTE_OS/$REMOTE_ARCH and target has no curl/wget — cannot bootstrap"; return 1; }
  url="https://github.com/prefix-dev/pixi/releases/latest/download/pixi-${triple}"
  tmp="$(mktemp "${TMPDIR:-/tmp}/pixi-bin.XXXXXX")"
  log "Target has no curl/wget — downloading pixi ($triple) here and pushing it over SSH…"
  fetch_to_control "$url" "$tmp" || { rm -f "$tmp"; return 1; }
  run_ssh 'mkdir -p "$HOME/.pixi/bin"'
  scp_push "$tmp" '~/.pixi/bin/pixi'
  rm -f "$tmp"
  run_ssh 'chmod +x "$HOME/.pixi/bin/pixi" && "$HOME/.pixi/bin/pixi" --version' \
    || { err "pushed pixi binary failed to run on target (arch mismatch?)"; return 1; }
  log "pixi staged on target at ~/.pixi/bin/pixi"
}

# ----------------------------------------------------------------------------- connect & detect OS / tooling
log "Connecting to $TARGET (port $SSH_PORT)…"
# One round-trip: OS, arch, home, existing pixi path, and whether curl/wget exist.
REMOTE_INFO="$(run_ssh '
  P=""; if [ -x "$HOME/.pixi/bin/pixi" ]; then P="$HOME/.pixi/bin/pixi"; elif command -v pixi >/dev/null 2>&1; then P="$(command -v pixi)"; fi
  c=no; command -v curl >/dev/null 2>&1 && c=yes
  w=no; command -v wget >/dev/null 2>&1 && w=yes
  printf "%s|%s|%s|%s|%s|%s\n" "$(uname -s)" "$(uname -m)" "$HOME" "$P" "$c" "$w"
')" || { err "SSH connection failed"; exit 1; }

REMOTE_OS="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f1)"
REMOTE_ARCH="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f2)"
REMOTE_HOME="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f3)"
TARGET_PIXI="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f4)"
TARGET_HAS_CURL="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f5)"
TARGET_HAS_WGET="$(printf '%s' "$REMOTE_INFO" | cut -d'|' -f6)"
[ -n "$REMOTE_DIR" ] || REMOTE_DIR="$REMOTE_HOME/rixi"
log "Target: $REMOTE_OS / $REMOTE_ARCH — installing to $REMOTE_DIR"

case "$REMOTE_OS" in
  Linux|Darwin) ;;
  *) err "unsupported target OS: $REMOTE_OS (need Linux or Darwin)"; exit 1 ;;
esac

# ----------------------------------------------------------------------------- ensure a downloader / pixi
if [ -n "$TARGET_PIXI" ]; then
  log "Pixi already present on target: $TARGET_PIXI"
elif [ "$TARGET_HAS_CURL" = yes ] || [ "$TARGET_HAS_WGET" = yes ]; then
  log "Target has $( [ "$TARGET_HAS_CURL" = yes ] && echo curl || echo wget ) — pixi will self-install on the target"
else
  # No pixi, no curl, no wget: stage the pixi binary from here.
  push_pixi_binary
  TARGET_PIXI='~/.pixi/bin/pixi'
fi

# ----------------------------------------------------------------------------- transfer code
log "Copying server/ to $TARGET:$REMOTE_DIR/server/ …"
run_ssh "mkdir -p '$REMOTE_DIR'"
if command -v rsync >/dev/null 2>&1 && run_ssh 'command -v rsync >/dev/null 2>&1'; then
  RSH="ssh ${SSH_OPTS[*]}"
  if [ -n "$SSH_PASSWORD" ]; then
    SSHPASS="$SSH_PASSWORD" rsync -az --delete -e "sshpass -e ssh ${SSH_OPTS[*]}" \
      "$LOCAL_SERVER_DIR/" "$TARGET:$REMOTE_DIR/server/"
  else
    rsync -az --delete -e "$RSH" "$LOCAL_SERVER_DIR/" "$TARGET:$REMOTE_DIR/server/"
  fi
else
  log "rsync unavailable on one side — falling back to scp"
  run_ssh "mkdir -p '$REMOTE_DIR/server'"
  if [ -n "$SSH_PASSWORD" ]; then
    SSHPASS="$SSH_PASSWORD" "${PASS_PREFIX[@]}" scp "${SCP_OPTS[@]}" -r "$LOCAL_SERVER_DIR/." "$TARGET:$REMOTE_DIR/server/"
  else
    scp "${SCP_OPTS[@]}" -r "$LOCAL_SERVER_DIR/." "$TARGET:$REMOTE_DIR/server/"
  fi
fi

# ----------------------------------------------------------------------------- build remote provisioning script
# Quote each forwarded rixi arg for safe embedding in the remote unit/plist.
RIXI_ARGS_QUOTED=""
for a in "${RIXI_EXTRA_ARGS[@]:-}"; do
  [ -n "$a" ] || continue
  RIXI_ARGS_QUOTED+=" $(printf '%q' "$a")"
done

# The remote script is assembled in a temp file from two heredocs: a header of
# locally-substituted values, then a quoted body that must stay literal so its
# remote-evaluated parts ($HOME, $(id -u), …) are NOT expanded on this machine.
# (Built via a redirection group rather than $(cat <<…) — bash 3.2 on macOS
# mishandles heredocs nested in command substitution.)
REMOTE_SCRIPT_FILE="$(mktemp "${TMPDIR:-/tmp}/rixi-provision.XXXXXX")"
trap 'rm -f "$REMOTE_SCRIPT_FILE"' EXIT
{
cat <<HEADER
set -euo pipefail
REMOTE_DIR='$REMOTE_DIR'
SERVICE_NAME='$SERVICE_NAME'
RIXI_PORT='$RIXI_PORT'
REMOTE_OS='$REMOTE_OS'
REMOTE_ARCH='$REMOTE_ARCH'
START='$START'
RIXI_ARGS='$RIXI_ARGS_QUOTED'
HEADER
cat <<'BODY'
log() { printf '\033[36m  [remote]\033[0m %s\n' "$*" >&2; }

# --- ensure pixi ----------------------------------------------------------------
# pixi may already be present, or have been staged by the control machine
# (~/.pixi/bin/pixi) when this host had no curl/wget. Otherwise self-install here.
PIXI="$HOME/.pixi/bin/pixi"
if ! [ -x "$PIXI" ] && command -v pixi >/dev/null 2>&1; then PIXI="$(command -v pixi)"; fi
if ! [ -x "$PIXI" ]; then
  if command -v curl >/dev/null 2>&1; then
    log "Installing Pixi via curl…"
    curl -fsSL https://pixi.sh/install.sh | bash >/dev/null
  elif command -v wget >/dev/null 2>&1; then
    log "Installing Pixi via wget…"
    wget -qO- https://pixi.sh/install.sh | bash >/dev/null
  else
    echo "no curl/wget and pixi was not pre-staged — cannot install pixi" >&2; exit 1
  fi
  PIXI="$HOME/.pixi/bin/pixi"
fi
[ -x "$PIXI" ] || { echo "pixi install failed" >&2; exit 1; }
log "Pixi: $("$PIXI" --version)"

cd "$REMOTE_DIR/server"

# --- add the target platform on macOS (manifest ships linux-64 only) ------------
if [ "$REMOTE_OS" = "Darwin" ]; then
  case "$REMOTE_ARCH" in
    arm64)  PLAT="osx-arm64" ;;
    x86_64) PLAT="osx-64" ;;
    *)      PLAT="" ;;
  esac
  if [ -n "$PLAT" ] && ! grep -q "$PLAT" pixi.toml; then
    log "Adding platform $PLAT to manifest…"
    "$PIXI" workspace platform add "$PLAT" 2>/dev/null \
      || "$PIXI" project platform add "$PLAT" 2>/dev/null \
      || true
  fi
fi

log "Installing dependencies (pixi install)… this can take a while"
"$PIXI" install

# --- build the ExecStart argument list ------------------------------------------
# Run with --no-reload (the reload path references a stale module) on the chosen port.
EXEC_ARGS="run --manifest-path $REMOTE_DIR/server/pixi.toml python rixi_server.py --no-reload --port $RIXI_PORT $RIXI_ARGS"

if [ "$REMOTE_OS" = "Linux" ]; then
  # ---- user-level systemd service ----------------------------------------------
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=RIXI Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REMOTE_DIR/server
ExecStart=$PIXI $EXEC_ARGS
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  # Keep the service running after logout / across reboots when permitted.
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  systemctl --user daemon-reload
  if [ "$START" = "1" ]; then
    systemctl --user enable --now "$SERVICE_NAME.service"
    sleep 2
    systemctl --user --no-pager status "$SERVICE_NAME.service" || true
  else
    systemctl --user enable "$SERVICE_NAME.service"
    log "Installed but not started (--no-start). Start with: systemctl --user start $SERVICE_NAME"
  fi
  log "Manage with: systemctl --user {status|restart|stop} $SERVICE_NAME ; logs: journalctl --user -u $SERVICE_NAME -f"

else
  # ---- macOS launchd LaunchAgent -----------------------------------------------
  LABEL="com.rixi.$SERVICE_NAME"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  # Emit one <string> per whitespace-split token of EXEC_ARGS.
  ARGS_XML=""
  for tok in $EXEC_ARGS; do
    ARGS_XML="$ARGS_XML        <string>$tok</string>
"
  done
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PIXI</string>
$ARGS_XML    </array>
    <key>WorkingDirectory</key><string>$REMOTE_DIR/server</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>$REMOTE_DIR/rixi.out.log</string>
    <key>StandardErrorPath</key><string>$REMOTE_DIR/rixi.err.log</string>
</dict>
</plist>
PLIST
  UID_NUM="$(id -u)"
  launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  if [ "$START" = "1" ]; then
    launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
    sleep 2
    launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E 'state|pid' | head -3 || true
    log "Started LaunchAgent $LABEL"
  else
    log "Installed but not started (--no-start). Start with: launchctl bootstrap gui/$UID_NUM '$PLIST'"
  fi
  log "Logs: $REMOTE_DIR/rixi.out.log / rixi.err.log"
fi
BODY
} > "$REMOTE_SCRIPT_FILE"

# ----------------------------------------------------------------------------- execute remotely
log "Provisioning on target…"
run_ssh "bash -s" < "$REMOTE_SCRIPT_FILE"

log "Done. RIXI server should be listening on $SSH_HOST:$RIXI_PORT"
log "Health check: curl http://$SSH_HOST:$RIXI_PORT/health"
