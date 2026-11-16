# Security Policy

## Supported Versions

Only the latest code on the `main` branch is supported with security fixes.

## Reporting a Vulnerability

Please report vulnerabilities privately — do **not** open a public issue for
security problems.

Open a private report via
[GitHub Security Advisories](../../security/advisories/new) on this repository.

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
- Tokens are the only authentication. An AES key (or the key handshake) hides
  what is sent; it does not say who may send it, and it does not stop a
  recorded request from being sent again. Across a network you do not control,
  use TLS: `rixi up` gives each box a certificate of its own and pins it in the
  profile; for your own server pass `--tls-cert/--tls-key` or put it behind a
  reverse proxy that terminates TLS. Where that is not possible and
  the client signs a fresh token per request, `--single-use-tokens` makes a
  token seen on the wire worthless (each is accepted once).
- When several servers trust one issuer, scope their tokens with `--audience`
  (and `--required-claim`), and require `exp` (`--require-exp`). Without an
  audience, a token issued for any of them works on all of them. The gateway
  takes `--jwt-audience` and `--jwt-issuer` for the same reason.
- The default mode (loopback, no tokens) refuses requests that a web page could
  make from the operator's browser: any request naming another site in `Origin`,
  and any request reaching the server under a name other than localhost. Keep it
  on loopback; reach it through an SSH tunnel or the rixi tunnel.
- The key handshake never sends the secret (both sides prove they know it), but
  a short or guessable secret can still be tried offline by someone who records
  a handshake. Use a long random one (`rixi up` generates 32 random bytes).
- Brokered gateway: every box and client holds the tunnel secret, so it
  authenticates membership, not identity. Boxes the gateway provisions prove a
  per-box registration token; clients are told apart by their JWT (set
  `require_jwt` in the policy) — without JWTs, a client is only as distinct as
  the connection it holds. Give the gateway a certificate
  (`--tls-cert/--tls-key`, or a TLS proxy and `--public-ws-url wss://…`) when
  the network between is not yours; the tunnel and the SMCP server take the
  same flags. The `dummy` provider runs boxes as processes on the
  gateway's own host and is off unless the gateway listens on loopback.
- The inference proxy forwards to its backend with its own token: give it an
  API key or JWT (`authentication` in its config) before it listens anywhere
  but loopback.
- A task asks the command-line client for something (a local file, a local
  service) by printing a request line. Only lines carrying the token the client
  made for that run are acted on (`.rixi_backchannel` in the task's working
  directory), so text a task merely prints cannot pose as a request.
- `rixi run` leaves files that look like credentials (`.env`, `*.pem`, keys,
  cloud credential directories) out of what it ships; `.rixiignore` adds
  patterns or, with `!`, sends one after all.
