"""`rixi mcp`: Rixi boxes as tools for an agent (Model Context Protocol over stdio).

Add it to an MCP client — Claude Code, Cursor, an agent framework:

    {"mcpServers": {"rixi": {"command": "rixi", "args": ["mcp"],
                             "env": {"RIXI_API_KEY": "rxk_…"}}}}

The agent can then create a box (hosted on eutonomous, or in your own cloud account), run
commands and long-lived processes on it, move files, and destroy it. The server runs on your
machine and talks to the boxes directly, exactly like the CLI: command output and files go
agent ↔ box, never through a hosted service.

Boxes are profiles, as with `rixi up`, so `rixi tasks --profile <name>` and friends see the same
boxes the agent made. Hosted boxes get a lifetime (`ttl_hours`, default 2) so a forgotten box
stops billing on its own; boxes in your own cloud account have none — destroy them.

The protocol is implemented here (JSON-RPC 2.0, one message per line on stdin/stdout), so this
needs nothing beyond the rixi package. Diagnostics go to stderr; stdout carries only protocol.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TEXT_LIMIT = 60_000              # characters of command output / file content per tool result
DEFAULT_PROVIDER = os.environ.get("RIXI_MCP_PROVIDER", "eutonomous")


class ToolError(Exception):
    """A failure the agent should see as a tool result (isError), not a protocol error."""


def _clip(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT // 2] + f"\n… [{len(text) - TEXT_LIMIT} characters cut] …\n" + \
        text[-TEXT_LIMIT // 2:]


def _str(args: dict, key: str, required: bool = True, default: Optional[str] = None):
    v = args.get(key, default)
    if v is None:
        if required:
            raise ToolError(f"{key} is required")
        return None
    if not isinstance(v, str):
        raise ToolError(f"{key} must be a string")
    return v


_BOX = {"type": "string", "description": "the box's name (from create_box or list_boxes)"}

TOOLS: List[dict] = [
    {"name": "create_box",
     "description": "Create a box (a whole Linux machine, CPU or GPU) and wait until it is ready, "
                    "about two minutes. It bills per minute until destroy_box or its lifetime "
                    "ends. Returns its name for the other tools.",
     "inputSchema": {"type": "object", "properties": {
         "size": {"type": "string", "description": "eutonomous: cpu-s, cpu-l, gpu-l4, gpu-l40s; "
                  "other providers: sample-cpu, sample-gpu. Default cpu-s / sample-cpu."},
         "provider": {"type": "string", "description": "eutonomous (hosted, EU-only; default), "
                      "scaleway, hetzner or aws (your own cloud account)"},
         "name": {"type": "string", "description": "optional; lowercase letters, digits, dashes"},
         "ttl_hours": {"type": "number", "description": "eutonomous: shut down after this many "
                       "hours (default 2)"},
         "egress": {"type": "string", "enum": ["open", "allowlist", "block"],
                    "description": "what the box may reach once ready (default open)"},
         "egress_allow": {"type": "array", "items": {"type": "string"},
                          "description": "with allowlist: IPs/CIDRs, optional :port"},
         "model_access": {"type": "string", "enum": ["none", "governed"],
                          "description": "eutonomous: governed gives the box OPENAI_BASE_URL "
                                         "through malgra (needs a linked key); default none"},
         "volumes": {"type": "array", "items": {"type": "string"},
                     "description": "eutonomous: volume ids to attach, mounted at "
                                    "/workspace/data/<name>"},
         "on_idle": {"type": "string", "enum": ["release", "stop"],
                     "description": "eutonomous: when idle, shut down (default) or stop and "
                                    "keep the disk"}}}},
    {"name": "list_boxes", "description": "The boxes you have (all profiles), with status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "destroy_box", "description": "Destroy a box; billing stops and its files are gone.",
     "inputSchema": {"type": "object", "properties": {"box": _BOX}, "required": ["box"]}},
    {"name": "stop_box",
     "description": "Hosted boxes: power off, keeping the disk and its files; compute stops "
                    "billing until resume_box.",
     "inputSchema": {"type": "object", "properties": {"box": _BOX}, "required": ["box"]}},
    {"name": "resume_box", "description": "Hosted boxes: start a stopped box and wait for it.",
     "inputSchema": {"type": "object", "properties": {"box": _BOX}, "required": ["box"]}},
    {"name": "list_volumes", "description": "Hosted: your persistent volumes.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_volume",
     "description": "Hosted: a persistent disk that outlives boxes (datasets, caches). Attach "
                    "it with create_box volumes=[id]. Billed per GB while it exists.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "size_gb": {"type": "integer"},
         "zone": {"type": "string"}}, "required": ["name", "size_gb"]}},
    {"name": "delete_volume", "description": "Hosted: delete a volume and its data.",
     "inputSchema": {"type": "object", "properties": {"volume_id": {"type": "string"}},
                     "required": ["volume_id"]}},
    {"name": "exec",
     "description": "Run a shell command in the box's workspace and wait for it. Returns exit "
                    "code, stdout and stderr. For servers and long jobs use spawn instead.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "command": {"type": "string"},
         "cwd": {"type": "string", "description": "directory inside the workspace"},
         "timeout": {"type": "number", "description": "seconds (default 600)"}},
         "required": ["box", "command"]}},
    {"name": "spawn",
     "description": "Start a command in the background (a dev server, a watcher, a long job) and "
                    "return its task id at once. Read it with task_output.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "command": {"type": "string"}, "cwd": {"type": "string"}},
         "required": ["box", "command"]}},
    {"name": "list_tasks", "description": "Tasks on a box: running or finished, with exit codes.",
     "inputSchema": {"type": "object", "properties": {"box": _BOX}, "required": ["box"]}},
    {"name": "task_output",
     "description": "What a task has printed so far and whether it is still running. With "
                    "wait_seconds, first wait up to that long for it to finish.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "task_id": {"type": "string"},
         "wait_seconds": {"type": "number", "description": "0-300 (default 0)"}},
         "required": ["box", "task_id"]}},
    {"name": "send_input",
     "description": "Write text to a running task's stdin (a newline is added if missing).",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "task_id": {"type": "string"}, "text": {"type": "string"}},
         "required": ["box", "task_id", "text"]}},
    {"name": "kill_task", "description": "Stop a task and forget it.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "task_id": {"type": "string"}}, "required": ["box", "task_id"]}},
    {"name": "open_preview",
     "description": "Serve a port of the box (a web app you started with spawn) at its own "
                    "HTTPS URL. Private by default: the returned link carries a token; "
                    "public=true lets anyone with the URL in.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "port": {"type": "integer"}, "public": {"type": "boolean"}},
         "required": ["box", "port"]}},
    {"name": "write_file", "description": "Write a text file in the box's workspace.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["box", "path", "content"]}},
    {"name": "read_file", "description": "Read a text file from the box's workspace.",
     "inputSchema": {"type": "object", "properties": {"box": _BOX, "path": {"type": "string"}},
                     "required": ["box", "path"]}},
    {"name": "list_files", "description": "List a directory in the box's workspace.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "path": {"type": "string", "description": "default: the workspace"}},
         "required": ["box"]}},
    {"name": "run_project",
     "description": "Ship a local Pixi project (code and its environment) to the box and run "
                    "one of its tasks; with background=true return its task id at once.",
     "inputSchema": {"type": "object", "properties": {
         "box": _BOX, "project_dir": {"type": "string", "description": "a local directory "
                                      "with a pixi.toml"},
         "task": {"type": "string"}, "background": {"type": "boolean"}},
         "required": ["box", "project_dir", "task"]}},
]


class Tools:
    """The tool implementations, over the SDK. `sandbox_for` and `up` are seams for tests."""

    def __init__(self, store=None, sandbox_for: Optional[Callable] = None,
                 up: Optional[Callable] = None, down: Optional[Callable] = None,
                 sleep=time.sleep, api=None):
        from .cloud.profiles import ProfileStore
        self.store = store or ProfileStore()
        self._sandbox_for = sandbox_for
        self._up, self._down, self._sleep, self._api = up, down, sleep, api

    def sandbox(self, name: str):
        if self._sandbox_for:
            return self._sandbox_for(name)
        from .sandbox import Sandbox
        if not self.store.exists(name):
            raise ToolError(f"no box named {name!r} (list_boxes)")
        return Sandbox.attach(name, store=self.store)

    # ── boxes ─────────────────────────────────────────────────────────────
    def create_box(self, a: dict) -> str:
        from .cloud import up as up_mod
        provider = _str(a, "provider", False, DEFAULT_PROVIDER)
        size = _str(a, "size", False, "cpu-s" if provider == "eutonomous" else "sample-cpu")
        policy = None
        egress = _str(a, "egress", False, "open")
        if egress != "open" or a.get("egress_allow"):
            policy = {"egress": {"mode": egress, "allow": list(a.get("egress_allow") or [])}}
        kw = dict(provider=provider, size=size, name=_str(a, "name", False) or
                  f"mcp-{os.urandom(3).hex()}", policy=policy, make_default=False,
                  ssh_key=None, store=self.store, echo=lambda m: print(m, file=sys.stderr))
        if provider == "eutonomous":
            kw["ttl_hours"] = float(a.get("ttl_hours") or 2)
            access = _str(a, "model_access", False)
            if access:
                kw["model_access"] = access
            if a.get("volumes"):
                kw["volumes"] = [str(v) for v in a["volumes"]]
            if a.get("on_idle"):
                kw["on_idle"] = _str(a, "on_idle")
        up = self._up or up_mod.up
        kw["creds"] = up_mod.resolve_credentials(provider, {})
        try:
            profile = up(**kw)
        except Exception as exc:
            raise ToolError(f"could not create the box: {exc}")
        life = f", shuts down after {kw['ttl_hours']:g} h" if "ttl_hours" in kw else \
            " — in your own cloud account it runs until destroy_box"
        return (f"box {profile.name!r} is ready ({profile.instance_type} in {profile.region}"
                f"{life}). Workspace: /workspace.")

    def list_boxes(self, a: dict) -> str:
        names = self.store.names()
        if not names:
            return "no boxes"
        rows = []
        for n in names:
            p = self.store.get(n)
            age = (time.time() - p.created_at) / 3600
            rows.append(f"{n}: {p.provider} {p.instance_type} {p.region} {p.status}, "
                        f"{age:.1f} h old")
        return "\n".join(rows)

    def destroy_box(self, a: dict) -> str:
        from .cloud import up as up_mod
        name = _str(a, "box")
        if not self.store.exists(name):
            raise ToolError(f"no box named {name!r}")
        down = self._down or up_mod.down
        provider = self.store.get(name).provider
        down(name, up_mod.resolve_credentials(provider, {}), store=self.store,
             echo=lambda m: print(m, file=sys.stderr))
        return f"box {name!r} destroyed"

    def _hosted_api(self):
        if self._api is not None:
            return self._api
        from .cloud import hosted
        return hosted.api_from_env()

    def stop_box(self, a: dict) -> str:
        self.sandbox(_str(a, "box")).stop()
        return "stopping; the disk is kept (resume_box to start it again)"

    def resume_box(self, a: dict) -> str:
        self.sandbox(_str(a, "box")).resume()
        return "running again"

    def list_volumes(self, a: dict) -> str:
        vols = self._hosted_api().volumes()
        if not vols:
            return "no volumes"
        return "\n".join(f"{v['id']}: {v['name']} {v['size_gb']} GB {v['zone']} "
                         f"{('attached to ' + v['box_id']) if v.get('box_id') else v['state']}"
                         for v in vols)

    def create_volume(self, a: dict) -> str:
        size = a.get("size_gb")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ToolError("size_gb must be a whole number of GB")
        v = self._hosted_api().create_volume(_str(a, "name"), size, _str(a, "zone", False))
        return f"volume {v['id']} ({v['name']}, {v['size_gb']} GB, {v['zone']}) is {v['state']}"

    def delete_volume(self, a: dict) -> str:
        self._hosted_api().delete_volume(_str(a, "volume_id"))
        return "deleting"

    # ── commands and tasks ────────────────────────────────────────────────
    def exec(self, a: dict) -> str:
        sb = self.sandbox(_str(a, "box"))
        r = sb.exec(_str(a, "command"), cwd=_str(a, "cwd", False),
                    timeout=float(a["timeout"]) if a.get("timeout") else None)
        parts = [f"exit code: {r.exit_code} ({r.status})"]
        if r.stdout:
            parts.append("stdout:\n" + _clip(r.stdout))
        if r.stderr:
            parts.append("stderr:\n" + _clip(r.stderr))
        return "\n".join(parts)

    def spawn(self, a: dict) -> str:
        tid = self.sandbox(_str(a, "box")).spawn(_str(a, "command"), cwd=_str(a, "cwd", False))
        return f"started; task id: {tid}"

    def list_tasks(self, a: dict) -> str:
        rows = self.sandbox(_str(a, "box")).tasks()
        if not rows:
            return "no tasks"
        out = []
        for tid, t in sorted(rows.items(), key=lambda kv: kv[1].get("started_at", 0)):
            code = "" if t.get("exit_code") is None else f", exit {t['exit_code']}"
            out.append(f"{tid}: {t.get('status')}{code} — {t.get('task_name', '')}")
        return "\n".join(out)

    def task_output(self, a: dict) -> str:
        sb, tid = self.sandbox(_str(a, "box")), _str(a, "task_id")
        wait = max(0.0, min(300.0, float(a.get("wait_seconds") or 0)))
        deadline = time.monotonic() + wait
        while True:
            t = sb.tasks().get(tid)
            if t is None:
                raise ToolError(f"no task {tid!r} on this box")
            if t.get("status") not in ("queued", "extracting", "starting", "running") or \
                    time.monotonic() >= deadline:
                break
            self._sleep(1.0)
        code = "" if t.get("exit_code") is None else f", exit code {t['exit_code']}"
        return f"status: {t.get('status')}{code}\noutput:\n" + _clip(sb.output(tid))

    def send_input(self, a: dict) -> str:
        text = _str(a, "text")
        self.sandbox(_str(a, "box")).send_input(_str(a, "task_id"),
                                                text if text.endswith("\n") else text + "\n")
        return "sent"

    def kill_task(self, a: dict) -> str:
        self.sandbox(_str(a, "box")).kill(_str(a, "task_id"))
        return "killed"

    def run_project(self, a: dict) -> str:
        sb = self.sandbox(_str(a, "box"))
        project, task = _str(a, "project_dir"), _str(a, "task")
        if not os.path.isfile(os.path.join(project, "pixi.toml")):
            raise ToolError(f"{project} has no pixi.toml")
        if a.get("background"):
            return f"started; task id: {sb.start(project, task=task)}"
        r = sb.run(project, task=task)
        return f"exit code: {r.exit_code}\noutput:\n" + _clip(r.output) + \
            (f"\nerror: {r.error}" if r.error else "")

    def open_preview(self, a: dict) -> str:
        port = a.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ToolError("port must be a number")
        v = self.sandbox(_str(a, "box")).preview(port, public=bool(a.get("public")))
        return f"{'public' if v['public'] else 'private'} preview: {v['share_url']}"

    # ── files ─────────────────────────────────────────────────────────────
    def write_file(self, a: dict) -> str:
        info = self.sandbox(_str(a, "box")).write_file(_str(a, "path"), _str(a, "content"))
        return f"wrote {info.get('path')} ({info.get('bytes')} bytes)"

    def read_file(self, a: dict) -> str:
        return _clip(self.sandbox(_str(a, "box")).read_file(_str(a, "path"))
                     .decode("utf-8", "replace"))

    def list_files(self, a: dict) -> str:
        entries = self.sandbox(_str(a, "box")).list_files(_str(a, "path", False, "."))
        if not entries:
            return "(empty)"
        return "\n".join(f"{e['name']}{'/' if e['type'] == 'dir' else ''}  {e['size']}"
                         for e in entries)


class Server:
    def __init__(self, tools: Optional[Tools] = None):
        self.tools = tools or Tools()

    def handle(self, msg: Any) -> Optional[dict]:
        """One JSON-RPC message in, the response out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600,
                          "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None                              # notifications need no answer
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                return _result(mid, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "rixi", "version": __version__},
                    "instructions": "Rixi boxes: create_box, then exec / spawn / files on it, "
                                    "and destroy_box when done — boxes bill per minute."})
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": TOOLS})
            if method == "tools/call":
                return _result(mid, self._call(params.get("name"), params.get("arguments") or {}))
            return _error(mid, -32601, f"unknown method {method!r}")
        except Exception as exc:  # noqa: BLE001 — never kill the session over one message
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, str(exc))

    def _call(self, name, args) -> dict:
        if name not in {t["name"] for t in TOOLS}:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                    "isError": True}
        if not isinstance(args, dict):
            return {"content": [{"type": "text", "text": "arguments must be an object"}],
                    "isError": True}
        try:
            text = getattr(self.tools, name)(args)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # noqa: BLE001 — a failed call is a result the agent can read
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):            # a batch (2025-03-26)
                    replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                    reply = replies or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> int:
    Server().serve()
    return 0
