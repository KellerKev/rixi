#!/usr/bin/env bash
#
# Box-side launcher: register THIS box as an *ephemeral* GitHub Actions self-hosted runner, run
# exactly one job, then let the runner deregister itself and exit. Shipped to the box and invoked
# as the `ci-runner` pixi task by rixi_ci_runner.py; its config (repo URL + a short-lived
# registration token) arrives in ci.env, which the driver writes into the shipped project.
#
# ci.env (written by the driver, 0600):
#   RUNNER_URL        https://github.com/<owner>/<repo>            [required]
#   RUNNER_TOKEN      short-lived runner registration token         [required]
#   RUNNER_LABELS     comma-separated labels a job targets          [default "rixi"]
#   RUNNER_NAME       runner name shown in the GitHub UI            [default "rixi-<host>"]
#   RUNNER_VERSION    actions/runner release, no leading v          [default 2.319.1]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$HERE/ci.env"

: "${RUNNER_URL:?ci.env must set RUNNER_URL}"
: "${RUNNER_TOKEN:?ci.env must set RUNNER_TOKEN}"
LABELS="${RUNNER_LABELS:-rixi}"
NAME="${RUNNER_NAME:-rixi-$(hostname)}"
VERSION="${RUNNER_VERSION:-2.319.1}"

case "$(uname -m)" in
  x86_64|amd64)  ARCH="x64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "[ci-runner] unsupported arch $(uname -m)" >&2; exit 2 ;;
esac

WORK="$(mktemp -d)"
cd "$WORK"
TARBALL="actions-runner-linux-${ARCH}-${VERSION}.tar.gz"
echo "[ci-runner] downloading $TARBALL"
curl -fsSL -o "$TARBALL" \
  "https://github.com/actions/runner/releases/download/v${VERSION}/${TARBALL}"
tar xzf "$TARBALL"

# The runner refuses to configure/run as root unless this is set; pods and cloud-init boxes are root.
export RUNNER_ALLOW_RUNASROOT=1
# System libs the runner needs (libicu, etc.). Best-effort — the box may already have them.
if [ -x ./bin/installdependencies.sh ]; then
  ./bin/installdependencies.sh || echo "[ci-runner] installdependencies.sh failed (continuing)" >&2
fi

# Belt-and-suspenders: an ephemeral runner auto-removes after its one job, but if run.sh dies
# before that, drop the registration so it doesn't linger as "offline" in the GitHub UI.
cleanup() { ./config.sh remove --token "$RUNNER_TOKEN" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[ci-runner] configuring ephemeral runner '$NAME' (labels: $LABELS)"
./config.sh --unattended --ephemeral --replace \
  --url "$RUNNER_URL" --token "$RUNNER_TOKEN" \
  --name "$NAME" --labels "$LABELS"

echo "[ci-runner] waiting for one job…"
./run.sh
