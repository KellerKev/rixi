"""`rixi` command — a thin client CLI over the SDK.

    rixi health --server http://box:9000
    rixi run   --server http://box:9000 --task train ./my-project
    rixi stream --server http://box:9000 --task serve --keep-alive ./my-project

This is the ergonomic front door the README used to fake with a shell alias. Server-side
components have their own entry points (rixi-server, rixi-gateway, …).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .client import Client, RixiError


def _client(args) -> Client:
    return Client(args.server, token=args.token or os.getenv("RIXI_TOKEN"),
                  aes_key=args.aes_key or os.getenv("RIXI_AES_KEY_B64"),
                  verify_ssl=not args.no_verify_ssl)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rixi", description="rixi client — run Pixi projects on a remote rixi server")
    p.add_argument("--version", action="version", version=f"rixi {__version__}")
    p.add_argument("--server", default=None,
                   help="server URL (or RIXI_SERVER; default http://127.0.0.1:9000)")
    p.add_argument("--token", help="JWT bearer token (or RIXI_TOKEN)")
    p.add_argument("--aes-key", help="base64 of a 32-byte AES key (or RIXI_AES_KEY_B64)")
    p.add_argument("--no-verify-ssl", action="store_true", help="skip TLS cert verification (dev only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def connection_flags(sp):
        """Accept the connection flags after the subcommand too.

        `rixi run --server …` is what the docs show and what people type, but argparse only
        accepts a parent flag before the subcommand. The copies write to their own names:
        sharing a name would have the subparser's unset default overwrite a value the parent
        had already parsed.
        """
        sp.add_argument("--server", dest="sub_server", default=None, help=argparse.SUPPRESS)
        sp.add_argument("--token", dest="sub_token", default=None, help=argparse.SUPPRESS)
        sp.add_argument("--aes-key", dest="sub_aes_key", default=None, help=argparse.SUPPRESS)
        sp.add_argument("--no-verify-ssl", dest="sub_no_verify_ssl", action="store_true",
                        default=False, help=argparse.SUPPRESS)

    connection_flags(sub.add_parser("health", help="check server health"))

    for name, help_ in (("run", "run a task and print the collected output"),
                        ("stream", "run a task and stream output live")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("project_dir", nargs="?", default=".", help="project directory (default: .)")
        sp.add_argument("--task", default="default", help="pixi task to run (default: default)")
        sp.add_argument("--keep-alive", action="store_true", help="keep the task alive after it finishes")
        connection_flags(sp)

    args = p.parse_args(argv)
    args.server = (getattr(args, "sub_server", None) or args.server
                   or os.getenv("RIXI_SERVER", "http://127.0.0.1:9000"))
    args.token = getattr(args, "sub_token", None) or args.token
    args.aes_key = getattr(args, "sub_aes_key", None) or args.aes_key
    args.no_verify_ssl = args.no_verify_ssl or getattr(args, "sub_no_verify_ssl", False)
    return _dispatch(args)


def _dispatch(args) -> int:
    client = _client(args)

    try:
        if args.cmd == "health":
            print(client.health())
            return 0
        if args.cmd == "stream":
            for chunk in client.stream(args.project_dir, task=args.task, keep_alive=args.keep_alive):
                sys.stdout.write(chunk)
                sys.stdout.flush()
            return 0
        if args.cmd == "run":
            result = client.run(args.project_dir, task=args.task, keep_alive=args.keep_alive)
            sys.stdout.write(result.output)
            if not result.ok:
                sys.stderr.write(f"\nrixi: {result.error}\n")
                return 1
            return 0
    except RixiError as exc:
        sys.stderr.write(f"rixi: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
