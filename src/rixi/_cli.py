"""`rixi` command — a thin client CLI over the SDK.

    rixi up --provider hetzner --size sample-cpu --name mybox   # create a box + save a profile
    rixi run --task train ./my-project                          # runs on the default profile
    rixi stream --profile mybox --task serve --keep-alive .
    rixi profiles                                               # list saved boxes
    rixi down mybox                                             # destroy the box

    rixi health --server http://box:9000                        # or address a server directly

Connection precedence: an explicit --server (or RIXI_SERVER) wins, with --token/--aes-key (or
RIXI_TOKEN/RIXI_AES_KEY_B64); otherwise --profile, $RIXI_PROFILE, or the default profile; otherwise
http://127.0.0.1:9000. Server-side components have their own entry points (rixi-server, …).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .client import Client, RixiError

_LOCAL = "http://127.0.0.1:9000"


def _client(args) -> Client:
    explicit = args.server or os.getenv("RIXI_SERVER")
    if not explicit:
        from .cloud.profiles import ProfileStore
        profile = ProfileStore().resolve(args.profile)
        if profile is not None:
            if profile.status != "ready":
                raise RixiError(f"profile {profile.name!r} is not ready (status: {profile.status})")
            return profile.client(verify_ssl=not args.no_verify_ssl)
    return Client(explicit or _LOCAL, token=args.token or os.getenv("RIXI_TOKEN"),
                  aes_key=args.aes_key or os.getenv("RIXI_AES_KEY_B64"),
                  verify_ssl=not args.no_verify_ssl)


_UP_HELP = """\
Create a cloud server, install the rixi server on it, and save a connection profile.

Sizes:
  sample-cpu   a small general-purpose box   hetzner cx23 · scaleway DEV1-M · aws t3.medium
  sample-gpu   one GPU for ML and inference  scaleway L4-1-24G · aws g6.xlarge (Hetzner has none)

Credentials come from the flags, the provider's usual env vars (HCLOUD_TOKEN; SCW_SECRET_KEY +
SCW_DEFAULT_PROJECT_ID or SCW_ACCESS_KEY; AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY), or
~/.rixi/credentials.json when saved with --save-credentials.

The box serves plain HTTP on port 9000. Every request needs a JWT signed by a key generated here
(it never leaves this machine); request and response bodies are AES-256-GCM sealed with a key
negotiated over RSA right after boot. The AWS provider is experimental (not yet run against a live
account) and needs `pip install 'rixi[aws]'`.
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rixi", description="rixi client — run Pixi projects on a remote rixi server")
    p.add_argument("--version", action="version", version=f"rixi {__version__}")
    p.add_argument("--server", default=None,
                   help="server URL (or RIXI_SERVER); overrides profiles")
    p.add_argument("--token", help="JWT bearer token (or RIXI_TOKEN)")
    p.add_argument("--aes-key", help="base64 of a 32-byte AES key (or RIXI_AES_KEY_B64)")
    p.add_argument("--profile", help="saved profile to use (or RIXI_PROFILE; default: the default)")
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
        sp.add_argument("--profile", dest="sub_profile", default=None, help=argparse.SUPPRESS)
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

    up = sub.add_parser("up", help="create a cloud box with rixi installed + save a profile",
                        description=_UP_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    up.add_argument("--provider", required=True, choices=["hetzner", "scaleway", "aws"])
    up.add_argument("--size", default="sample-cpu", choices=["sample-cpu", "sample-gpu"])
    up.add_argument("--name", help="profile/box name (default: <provider>-<size>)")
    up.add_argument("--type", dest="instance_type", help="instance type override (e.g. cx33)")
    up.add_argument("--region", help="region/zone (default: the provider's, with stock fallback)")
    up.add_argument("--allow-from", default="0.0.0.0/0",
                    help="CIDRs allowed to reach the box (comma-separated), or 'auto' for this "
                         "machine's public IP (default: 0.0.0.0/0)")
    up.add_argument("--ssh-key", default="auto",
                    help="SSH public key to authorize for debugging (default: ~/.ssh/id_*.pub; "
                         "'none' to skip)")
    up.add_argument("--rixi-ref", help="rixi git tag/branch/commit to install (default: "
                                       "this client's release)")
    up.add_argument("--save-credentials", action="store_true",
                    help="store the cloud credentials in ~/.rixi/credentials.json (0600)")
    up.add_argument("--no-default", action="store_true", help="don't make it the default profile")
    up.add_argument("--keep-on-failure", action="store_true",
                    help="keep the box if setup fails (for debugging; it keeps billing)")
    up.add_argument("--timeout", type=float, default=15, help="minutes to wait for the box (15)")
    _cred_flags(up)

    down = sub.add_parser("down", help="destroy a box created with `rixi up` and forget its profile")
    down.add_argument("name", nargs="?", help="profile name (default: the default profile)")
    _cred_flags(down)

    sub.add_parser("profiles", help="list saved profiles")
    use = sub.add_parser("use", help="make a profile the default")
    use.add_argument("name")

    args = p.parse_args(argv)
    args.server = getattr(args, "sub_server", None) or args.server
    args.token = getattr(args, "sub_token", None) or args.token
    args.aes_key = getattr(args, "sub_aes_key", None) or args.aes_key
    args.profile = getattr(args, "sub_profile", None) or args.profile
    args.no_verify_ssl = args.no_verify_ssl or getattr(args, "sub_no_verify_ssl", False)
    if args.cmd in ("up", "down", "profiles", "use"):
        return _cloud(args)
    return _dispatch(args)


def _cred_flags(sp) -> None:
    g = sp.add_argument_group("cloud credentials (or env vars)")
    g.add_argument("--hetzner-token", help="Hetzner Cloud API token (HCLOUD_TOKEN)")
    g.add_argument("--scw-secret-key", help="Scaleway secret key (SCW_SECRET_KEY)")
    g.add_argument("--scw-access-key", help="Scaleway access key (SCW_ACCESS_KEY)")
    g.add_argument("--scw-project-id", help="Scaleway project id (SCW_DEFAULT_PROJECT_ID)")
    g.add_argument("--aws-access-key-id", help="AWS access key id (AWS_ACCESS_KEY_ID)")
    g.add_argument("--aws-secret-access-key", help="AWS secret key (AWS_SECRET_ACCESS_KEY)")


def _cred_values(args) -> dict:
    return {"token": args.hetzner_token, "secret_key": args.scw_secret_key,
            "access_key": args.scw_access_key, "project_id": args.scw_project_id,
            "access_key_id": args.aws_access_key_id,
            "secret_access_key": args.aws_secret_access_key}


def _cloud(args) -> int:
    from .cloud import up as up_mod
    from .cloud.presets import PresetError
    from .cloud.profiles import ProfileError, ProfileStore
    from .cloud.providers import ProviderError

    store = ProfileStore()
    try:
        if args.cmd == "profiles":
            return _list_profiles(store)
        if args.cmd == "use":
            store.set_default(args.name)
            print(f"default profile: {args.name}")
            return 0
        if args.cmd == "up":
            creds = up_mod.resolve_credentials(args.provider, _cred_values(args))
            profile = up_mod.up(
                provider=args.provider, size=args.size, name=args.name, creds=creds,
                instance_type=args.instance_type, region=args.region,
                allow_from=args.allow_from,
                ssh_key=None if args.ssh_key == "none" else args.ssh_key,
                rixi_ref=args.rixi_ref, save_creds=args.save_credentials,
                make_default=not args.no_default, wait_timeout=args.timeout * 60,
                keep_on_failure=args.keep_on_failure, store=store)
            flag = "" if store.default_name() == profile.name else f" --profile {profile.name}"
            print(f"\n  try it:   rixi run{flag} --task hello examples/hello")
            print(f"  destroy:  rixi down {profile.name}")
            return 0
        if args.cmd == "down":
            name = args.name or store.default_name()
            if not name:
                print("rixi: no profile to destroy", file=sys.stderr)
                return 1
            creds = up_mod.resolve_credentials(store.get(name).provider, _cred_values(args))
            up_mod.down(name, creds, store=store)
            return 0
    except (up_mod.UpError, PresetError, ProfileError, ProviderError, RixiError) as exc:
        print(f"rixi: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


def _list_profiles(store) -> int:
    names = store.names()
    if not names:
        print("no profiles — create one with `rixi up --provider hetzner --size sample-cpu`")
        return 0
    default = store.default_name()
    rows = []
    for n in names:
        pr = store.get(n)
        age_h = (time.time() - pr.created_at) / 3600
        cost = f"€{pr.eur_per_hour * age_h:.2f} so far" if pr.eur_per_hour else ""
        rows.append(("*" if n == default else " ", n, pr.provider, pr.instance_type, pr.region,
                     pr.server_url, pr.status, f"{age_h:.1f}h", cost))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def _dispatch(args) -> int:
    try:
        client = _client(args)
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
    except Exception as exc:
        from .cloud.profiles import ProfileError
        if isinstance(exc, ProfileError):
            sys.stderr.write(f"rixi: {exc}\n")
            return 1
        raise
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
