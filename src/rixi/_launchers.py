"""Console-script launchers for the server-side rixi components.

`pip install rixi` gives you the client SDK + the `rixi` CLI. These launchers
(`rixi-server`, `rixi-gateway`, `rixi-tunnel`, `rixi-agent`, `rixi-proxy`,
`rixi-inference`) are convenience wrappers that locate the component script in the repo
checkout and run it with your arguments. The components carry heavy deps (fastapi, torch,
duckdb, …) and are normally deployed with Pixi; set RIXI_HOME to the repo root if the
launcher can't find it automatically.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _repo_root() -> Path:
    env = os.getenv("RIXI_HOME")
    if env and Path(env).is_dir():
        return Path(env)
    # Editable/src install: src/rixi/_launchers.py → repo root is parents[2].
    cand = Path(__file__).resolve().parents[2]
    if (cand / "server" / "rixi_server.py").exists():
        return cand
    # Fallback: current working directory.
    return Path.cwd()


def _run_script(rel_path: str) -> "None":
    root = _repo_root()
    script = root / rel_path
    if not script.exists():
        sys.stderr.write(
            f"rixi: cannot find {rel_path} under {root}. Set RIXI_HOME to your rixi "
            f"checkout, or run this component with Pixi from its directory.\n")
        raise SystemExit(2)
    # Component scripts use bare imports (e.g. `from rixi_transport import …`); put their
    # own directory first on sys.path so those resolve.
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")


def _run_module(module: str, chdir_root: bool = True) -> "None":
    root = _repo_root()
    sys.path.insert(0, str(root))
    if chdir_root:
        os.chdir(root)
    runpy.run_module(module, run_name="__main__")


def server() -> None:
    _run_script("server/rixi_server.py")


def client() -> None:
    _run_script("clients/rixi_client.py")


def tunnel() -> None:
    _run_script("tunnel/rixi_tunnel.py")


def agent() -> None:
    _run_script("agent/start_agent.py")


def proxy() -> None:
    _run_script("proxy/proxy.py")


def inference() -> None:
    _run_script("inference-server/inference_server.py")


def gateway() -> None:
    _run_module("gateway")
