# Security Policy

## Supported Versions

Only the latest code on the `main` branch is supported with security fixes.

## Reporting a Vulnerability

Please report vulnerabilities privately — do **not** open a public issue for
security problems.

- Preferred: open a private report via
  [GitHub Security Advisories](../../security/advisories/new) on this repository.
- Alternatively: email <kevin@fineupp.com>.

You can expect an initial response within roughly 7 days. Please include
reproduction steps and the affected component (server, agent, clients, or
installer) where possible.

## Deployment Hardening

RIXI executes code on remote machines by design, so harden any non-local
deployment:

- Enable JWT authentication (`--public-key` or `--jwks-url`) so only holders of
  valid tokens can submit work.
- Enable payload encryption with `--aes-key`.
- Keep the default loopback bind unless authentication is enabled; never expose
  an unauthenticated server on a public interface.
- Prefer the `RIXI_KEY_SECRET` and `RIXI_AES_KEY` environment variables over
  CLI flags — flag values are visible in `ps` output and service definitions.
  The installer (`install-rixi.sh`) does this automatically, writing secrets to
  a `chmod 600` env file on the target.
