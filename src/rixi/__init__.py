"""rixi — secure remote execution for Pixi projects.

The pip-installable client SDK. Package a local project, ship it to a rixi server, run a
task, and stream results back:

    from rixi import Client
    rixi = Client("http://127.0.0.1:9000", token="…")
    print(rixi.run(".", task="train").output)

The server, gateway, tunnel, agent, proxy, and inference components ship in the repo and
are deployed with Pixi (they carry heavier deps); this package is the lightweight client.
"""
from __future__ import annotations

from .client import Client, ExecResult, RixiError, RunResult
from .sandbox import Sandbox

__all__ = ["Client", "ExecResult", "RixiError", "RunResult", "Sandbox", "__version__"]
__version__ = "0.10.0"
