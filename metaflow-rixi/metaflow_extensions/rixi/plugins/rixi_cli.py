"""The trampoline `rixi` CLI. `runtime_step_cli` reroutes a `@rixi` step to `rixi step`, which
runs the ordinary `python flow.py ... step ...` command on a rixi box.

Mirrors metaflow/plugins/kubernetes/kubernetes_cli.py, but instead of launching a container it:
  1. builds the inner step command (top-level flow args + the step + its args),
  2. ships the flow project to a rixi box (a rixi server directly via `--server`, or one
     provisioned/reused through the gateway via `--resource`),
  3. runs the step there against the shared S3 datastore (so Metaflow moves the artifacts itself),
  4. streams logs back and exits with the step's exit code,
  5. tears the box down (gateway path).
"""
import os
import shlex
import shutil
import sys
import tempfile
import time

from metaflow import util
from metaflow._vendor import click

# S3/datastore credentials the box needs (datastore *type* + root travel in the top-level args).
_S3_ENV_PASSTHROUGH = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION",
    "AWS_REGION", "METAFLOW_S3_ENDPOINT_URL", "METAFLOW_DATASTORE_SYSROOT_S3",
    "METAFLOW_DEFAULT_DATASTORE",
)


@click.group()
def cli():
    pass


@cli.group(help="Commands related to RIXI compute.")
def rixi():
    pass


@rixi.command(help="Execute a single step on a rixi box. Used internally by the @rixi decorator.",
              context_settings=dict(ignore_unknown_options=True))
@click.argument("step-name")
# --- @rixi attributes (forwarded by runtime_step_cli) ---
@click.option("--server", default=None, help="rixi server URL to run the step on directly.")
@click.option("--resource", default=None, help="gateway resource name to provision/reuse.")
@click.option("--gateway", default=None, help="gateway ws URL (with --resource).")
@click.option("--secret", default=None, help="gateway shared secret (or RIXI_GATEWAY_SECRET).")
@click.option("--provider", default=None, help="gateway provider for an ad-hoc box.")
@click.option("--token", default=None, help="JWT bearer token for the rixi server.")
@click.option("--aes-key", default=None, help="base64 AES key (matches server --aes-key).")
@click.option("--task", default="rixi-step", help="pixi task in the flow project that runs a step.")
@click.option("--teardown/--no-teardown", default=True, help="tear the box down after (gateway).")
# --- passthrough options for the inner `step` command (mirror metaflow's top-level step) ---
@click.option("--run-id", help="Passed to the top-level 'step'.")
@click.option("--task-id", help="Passed to the top-level 'step'.")
@click.option("--input-paths", help="Passed to the top-level 'step'.")
@click.option("--input-paths-filename", help="Passed to the top-level 'step'.")
@click.option("--split-index", help="Passed to the top-level 'step'.")
@click.option("--clone-run-id", help="Passed to the top-level 'step'.")
@click.option("--clone-only", help="Passed to the top-level 'step'.")
@click.option("--tag", multiple=True, default=None, help="Passed to the top-level 'step'.")
@click.option("--namespace", default=None, help="Passed to the top-level 'step'.")
@click.option("--retry-count", default=0, help="Passed to the top-level 'step'.")
@click.option("--max-user-code-retries", default=0, help="Passed to the top-level 'step'.")
@click.option("--num-parallel", default=None, help="Passed to the top-level 'step'.")
@click.option("--ubf-context", default="none",
              type=click.Choice(["none", "ubf_control", "ubf_task"]))
@click.pass_context
def step(ctx, step_name, server, resource, gateway, secret, provider, token, aes_key, task,
         teardown, **kwargs):
    # Build the inner step command: `python -u flow.py <top_args> step <step> <step_args>`.
    # `python` (not an absolute path) so it resolves to the box's pixi-env interpreter.
    kwargs.pop("ubf_context", None)  # not a step CLI option
    top_args = " ".join(util.dict_to_cli_options(ctx.parent.parent.params))
    step_args = " ".join(util.dict_to_cli_options(kwargs))
    flow_file = os.path.basename(sys.argv[0])
    step_cli = "python -u %s %s step %s %s" % (flow_file, top_args, step_name, step_args)

    ctx.obj.echo("Running step '%s' on rixi (%s)" % (
        step_name, ("server " + server) if server else ("resource " + str(resource))))

    code = _run_on_box(step_cli, flow_file, task=task, server=server, token=token,
                       aes_key=aes_key, resource=resource, gateway=gateway, secret=secret,
                       provider=provider, teardown=teardown, echo=ctx.obj.echo)
    if code != 0:
        raise click.ClickException("rixi step '%s' failed with exit code %s" % (step_name, code))


def _metaflow_rixi_root():
    # metaflow-rixi/metaflow_extensions/rixi/plugins/rixi_cli.py → metaflow-rixi/
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _staged_project(flow_file, step_cli, task):
    """Copy the flow's directory to a temp dir, inject the step script + a pixi task, return it.

    Also bundles the metaflow-rixi source into `_deps/metaflow-rixi` so the box can install the
    `@rixi` decorator (needed to import the flow) without PyPI — self-contained, air-gap friendly.
    The flow's pixi.toml should declare `metaflow-rixi = {{ path = "_deps/metaflow-rixi" }}`.
    """
    flow_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    tmp = tempfile.mkdtemp(prefix="rixi-mf-")
    dst = os.path.join(tmp, "proj")
    ignore = shutil.ignore_patterns(".git", ".pixi", "__pycache__", ".metaflow", "*.pyc",
                                    ".venv", "venv", ".mypy_cache", ".pytest_cache", "*.egg-info",
                                    "dist", "build", "_deps")
    shutil.copytree(flow_dir, dst, ignore=ignore)

    # Bundle the metaflow-rixi source so the box can `pip install` it via a pixi path dep.
    deps = os.path.join(dst, "_deps", "metaflow-rixi")
    shutil.copytree(_metaflow_rixi_root(), deps, ignore=ignore)

    # Script the box runs: export the S3 creds it needs + a stable Metaflow identity, then exec the
    # step command. The box has no $USER, so without METAFLOW_USER Metaflow aborts the step with
    # "could not determine your user name"; forward the orchestrator's identity so both sides agree.
    user = (os.environ.get("METAFLOW_USER") or os.environ.get("USER")
            or os.environ.get("USERNAME") or "rixi")
    exports = "export METAFLOW_USER=%s\n" % shlex.quote(user) + "\n".join(
        "export %s=%s" % (k, shlex.quote(os.environ[k]))
        for k in _S3_ENV_PASSTHROUGH if os.environ.get(k))
    script = "#!/usr/bin/env bash\nset -e\n%s\nexec %s\n" % (exports, step_cli)
    with open(os.path.join(dst, "rixi_step.sh"), "w") as f:
        f.write(script)

    # Ensure the pixi project (a) supports the box's Linux arches so the env resolves there
    # (boxes are Linux; could be x86 or ARM), and (b) defines the task that runs the step script.
    pixi_toml = os.path.join(dst, "pixi.toml")
    task_line = '%s = "bash rixi_step.sh"' % task
    if os.path.exists(pixi_toml):
        text = open(pixi_toml).read()
        text = _ensure_platforms(text, ("linux-64", "linux-aarch64"))
        if task_line not in text:
            if "[tasks]" in text:
                text = text.replace("[tasks]", "[tasks]\n%s" % task_line, 1)
            else:
                text += "\n[tasks]\n%s\n" % task_line
        open(pixi_toml, "w").write(text)
    return tmp, dst


def _ensure_platforms(text, platforms):
    """Add the given platforms to a pixi.toml `platforms = [...]` array if missing (the box runs
    Linux; the user's flow env may only list their laptop's platform)."""
    import re
    m = re.search(r"platforms\s*=\s*\[([^\]]*)\]", text)
    if not m:
        return text
    cur = m.group(1)
    add = [p for p in platforms if ('"%s"' % p) not in cur]
    if not add:
        return text
    newlist = cur.rstrip().rstrip(",") + ", " + ", ".join('"%s"' % p for p in add)
    return text[:m.start(1)] + newlist + text[m.end(1):]


def _run_on_box(step_cli, flow_file, *, task, server, token, aes_key, resource, gateway, secret,
                provider, teardown, echo):
    """Ship the flow to a box and run one step; returns the step's exit code."""
    from rixi import Client, RixiError

    tmp, proj = _staged_project(flow_file, step_cli, task)

    try:
        if server:
            return _run_and_wait(server, token, aes_key, proj, task, echo)
        # gateway path
        return _run_via_gateway(proj, task, resource, gateway, secret, provider, token, aes_key,
                                teardown, echo)
    except RixiError as e:
        echo("rixi error: %s" % e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_and_wait(server, token, aes_key, proj, task, echo):
    """Run the step to completion on `server` and return its exit code.

    keep_alive=True is required: the non-keep-alive /upload stream returns before the task
    finishes. We poll GET /task/{id} for the authoritative exit code, then terminate the task.
    """
    import requests

    from rixi import Client

    client = Client(server, token=token, aes_key=aes_key)
    headers = {"Authorization": "Bearer %s" % token} if token else {}
    result = client.run(proj, task=task, keep_alive=True)
    if result.output:
        echo(result.output.rstrip("\n"))
    if result.error:
        echo("error: %s" % result.error)
        _terminate(server, result.task_id, headers)
        return 1

    code = result.exit_code
    tid = result.task_id
    if code is None and tid:  # authoritative fallback: poll the task record
        for _ in range(600):
            try:
                j = requests.get("%s/task/%s" % (server.rstrip("/"), tid), headers=headers,
                                 timeout=10).json()
            except Exception:
                break
            code = j.get("exit_code")
            if code is not None or j.get("status") in ("completed", "failed", "error", "terminated"):
                break
            time.sleep(2)
    _terminate(server, tid, headers)
    return code if code is not None else 0


def _terminate(server, tid, headers):
    if not tid:
        return
    try:
        import requests
        requests.delete("%s/task/%s" % (server.rstrip("/"), tid), headers=headers, timeout=10)
    except Exception:
        pass


def _run_via_gateway(proj, task, resource, gateway, secret, provider, token, aes_key, teardown, echo):
    import asyncio

    from rixi import Client
    try:
        from gateway.client import GatewayClient
    except ImportError as e:
        raise click.ClickException(
            "gateway path needs the rixi gateway importable (set PYTHONPATH to the rixi repo): %s" % e)

    gw_url = gateway or os.environ.get("RIXI_GATEWAY_URL")
    gw_secret = secret or os.environ.get("RIXI_GATEWAY_SECRET")
    if not gw_url or not gw_secret:
        raise click.ClickException("gateway path needs --gateway and --secret (or RIXI_GATEWAY_URL/SECRET)")

    async def _go():
        gc = GatewayClient(gw_url, gw_secret, node_id="metaflow", token=token)
        await gc.connect()
        tok = None
        try:
            if resource:
                await gc.request_resource(resource)
            else:
                r = await gc.request_compute(provider=provider or "dummy")
                tok = r.get("token")
            node_id = await gc.wait_ready(timeout=900)
            srv, lport = await gc.serve_local(node_id, bind_port=0)
            try:
                url = "http://127.0.0.1:%d" % lport
                return await asyncio.to_thread(_run_and_wait, url, token, aes_key, proj, task, echo)
            finally:
                srv.close()
        finally:
            if teardown and resource:
                try:
                    await gc.teardown(resource)
                except Exception:
                    pass
            elif tok:
                try:
                    await gc.release(tok)
                except Exception:
                    pass

    return asyncio.run(_go())
