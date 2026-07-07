#!/usr/bin/env python3
"""Run one GitHub Actions job on an on-demand rixi box, then tear the box down.

    PYTHONPATH=<rixi-repo> python rixi_ci_runner.py \
        --repo owner/name --gateway ws://127.0.0.1:7100 --secret <gateway-secret> \
        --resource ci-runner --labels rixi-ephemeral

The flow, reusing the existing control + task machinery (no new gateway code):

  1. Mint a short-lived *runner registration token* from the GitHub API using a PAT read from
     $GH_PAT (or $GITHUB_TOKEN). The PAT never leaves this process; only the derived registration
     token is sent onward, and it travels the encrypted rixi tunnel to the box.
  2. Ask the gateway for a box (`GatewayClient.request_resource`) and wait for it to dial back.
  3. Ship this directory (run_ephemeral.sh + a generated ci.env) to the box and run the
     `ci-runner` pixi task, which registers an *ephemeral* runner, runs one job, and exits.
  4. Tear the box down (`release` for always-fresh resources, else `teardown`).

Needs the rixi repo importable (PYTHONPATH=<repo>) for `gateway.client` and `rixi`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_RUNNER_VERSION = "2.319.1"


# ── GitHub API ──────────────────────────────────────────────────────────────
def _gh_api(method: str, url: str, token: str | None = None, data: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed github.com host
        return json.loads(r.read().decode())


def mint_registration_token(repo: str, token: str) -> str:
    """POST /repos/{repo}/actions/runners/registration-token → a short-lived (~1h) token."""
    url = "https://api.github.com/repos/%s/actions/runners/registration-token" % repo
    try:
        return _gh_api("POST", url, token)["token"]
    except urllib.error.HTTPError as e:
        raise SystemExit("GitHub token mint failed (%s): %s — the PAT needs admin/repo rights on %s"
                         % (e.code, e.reason, repo))


def latest_runner_version() -> str:
    try:
        tag = _gh_api("GET", "https://api.github.com/repos/actions/runner/releases/latest")["tag_name"]
        return tag.lstrip("v")
    except Exception:
        return _DEFAULT_RUNNER_VERSION


# ── staging ─────────────────────────────────────────────────────────────────
def _staged_runner(reg_token: str, repo: str, labels: str, name: str, version: str):
    """Copy this dir to a temp project and write ci.env for run_ephemeral.sh to source."""
    tmp = tempfile.mkdtemp(prefix="rixi-ci-")
    dst = os.path.join(tmp, "proj")
    shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns(
        ".git", ".pixi", "__pycache__", "*.pyc", "ci.env", "rixi_ci_runner.py", "README.md"))
    env = {
        "RUNNER_URL": "https://github.com/%s" % repo,
        "RUNNER_TOKEN": reg_token,
        "RUNNER_LABELS": labels,
        "RUNNER_NAME": name,
        "RUNNER_VERSION": version,
    }
    path = os.path.join(dst, "ci.env")
    with open(path, "w") as f:
        for k, v in env.items():
            f.write("%s=%s\n" % (k, shlex.quote(v)))
    os.chmod(path, 0o600)
    return tmp, dst


# ── run the task on the box (keep_alive + poll for the authoritative exit code) ──
def _run_and_wait(server_url: str, proj: str, poll_secs: float) -> int:
    import requests

    from rixi import Client

    client = Client(server_url)
    result = client.run(proj, task="ci-runner", keep_alive=True)
    if result.output:
        print(result.output.rstrip("\n"), flush=True)
    if result.error:
        print("ci-runner error: %s" % result.error, flush=True)
        return 1
    code, tid = result.exit_code, result.task_id
    if code is None and tid:  # the stream can end before the process; the task record is authoritative
        deadline = time.time() + poll_secs
        while time.time() < deadline:
            try:
                j = requests.get("%s/task/%s" % (server_url.rstrip("/"), tid), timeout=10).json()
            except Exception:
                break
            code = j.get("exit_code")
            if code is not None or j.get("status") in ("completed", "failed", "error", "terminated"):
                break
            time.sleep(2)
    return code if code is not None else 0


async def _drive(args, tmp, proj) -> int:
    from gateway.client import GatewayClient

    gc = GatewayClient(args.gateway, args.secret, node_id="ci-runner-driver",
                       token=args.token, kdf_salt=args.kdf_salt)
    await gc.connect()
    reply = await gc.request_resource(args.resource)
    claim_token = reply.get("token")  # present for always-fresh (reuse=false) resources
    if reply.get("reused"):
        print("♻️  reusing '%s'" % args.resource, flush=True)
    else:
        print("⏳ provisioning '%s'…" % args.resource, flush=True)
    try:
        node = await gc.wait_ready(timeout=args.provision_timeout)
        print("✅ box ready: %s" % node, flush=True)
        srv, lport = await gc.serve_local(node, bind_port=0)
        try:
            return await asyncio.to_thread(_run_and_wait, "http://127.0.0.1:%d" % lport,
                                           proj, args.job_timeout)
        finally:
            srv.close()
    finally:
        try:
            if claim_token:
                await gc.release(claim_token)
            else:
                await gc.teardown(args.resource)
            print("🧹 box torn down", flush=True)
        except Exception as e:  # noqa: BLE001
            print("teardown warning: %s" % e, flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Run one GitHub Actions job on an ephemeral rixi box.")
    p.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    p.add_argument("--gateway", default=os.environ.get("RIXI_GATEWAY_URL"),
                   help="gateway ws URL (or RIXI_GATEWAY_URL)")
    p.add_argument("--secret", default=os.environ.get("RIXI_GATEWAY_SECRET"),
                   help="gateway shared secret (or RIXI_GATEWAY_SECRET)")
    p.add_argument("--kdf-salt", default=os.environ.get("RIXI_TUNNEL_SALT", ""),
                   help="gateway KDF salt (or RIXI_TUNNEL_SALT)")
    p.add_argument("--token", default=None, help="optional JWT for gateway RBAC")
    p.add_argument("--resource", default="ci-runner", help="catalog resource to provision")
    p.add_argument("--labels", default="rixi", help="runner labels a job's runs-on targets")
    p.add_argument("--name", default=None, help="runner name (default rixi-ci-<ts>)")
    p.add_argument("--runner-version", default=None, help="actions/runner version (default: latest)")
    p.add_argument("--provision-timeout", type=float, default=900, help="seconds to wait for the box")
    p.add_argument("--job-timeout", type=float, default=3600, help="seconds to wait for the job")
    args = p.parse_args()

    if not args.gateway or not args.secret:
        return _fail("set --gateway and --secret (or RIXI_GATEWAY_URL / RIXI_GATEWAY_SECRET)")
    pat = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not pat:
        return _fail("set $GH_PAT (or $GITHUB_TOKEN) to a PAT with runner-registration rights")

    version = args.runner_version or latest_runner_version()
    name = args.name or ("rixi-ci-%d" % int(time.time()))
    reg_token = mint_registration_token(args.repo, pat)
    print("🎟️  minted a runner registration token for %s (runner v%s)" % (args.repo, version),
          flush=True)

    tmp, proj = _staged_runner(reg_token, args.repo, args.labels, name, version)
    try:
        return asyncio.run(_drive(args, tmp, proj))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fail(msg: str) -> int:
    print("error: %s" % msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
