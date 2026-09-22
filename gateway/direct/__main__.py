"""python -m gateway.direct --config rixi.toml --bind 127.0.0.1:7101"""
from __future__ import annotations

import argparse
import logging
import os

from ..auth import JwtVerifier
from . import build_service, load
from .web import asgi, build_app


def main() -> None:
    ap = argparse.ArgumentParser(description="rixi gateway, direct mode (control plane only)")
    ap.add_argument("--config", default=os.getenv("RIXI_GATEWAY_CONFIG", "rixi.toml"))
    ap.add_argument("--bind", default=os.getenv("RIXI_DIRECT_BIND", "127.0.0.1:7101"))
    ap.add_argument("--audit-log", default=os.getenv("RIXI_AUDIT_LOG"),
                    help="JSON-lines audit file (default: stderr)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load(args.config)
    if not (cfg.jwt_public_key or cfg.jwt_jwks_url):
        raise SystemExit("[direct] needs jwt_public_key or jwt_jwks_url: the API is never open")

    import json
    import time
    import uuid

    from ..audit import AuditEvent, JsonLogSink
    sink = JsonLogSink(args.audit_log)

    def audit(event: str, **attrs) -> None:
        # Called from worker threads, so write synchronously (the sink's emit is a coroutine).
        ev = AuditEvent(ts=time.time(), event=event, actor=str(attrs.pop("sub", "system")),
                        action=event, target=attrs.get("box"), request_id=uuid.uuid4().hex,
                        attrs=attrs)
        sink._log.info(json.dumps(ev.as_dict()))

    svc = build_service(cfg, audit=audit)
    verifier = JwtVerifier(public_key_pem=cfg.jwt_public_key, jwks_url=cfg.jwt_jwks_url,
                           roles_claim=cfg.roles_claim, audience=cfg.jwt_audience)
    app = asgi(build_app(svc, verifier))
    svc.start()
    host, _, port = args.bind.rpartition(":")
    import uvicorn
    try:
        uvicorn.run(app, host=host or "127.0.0.1", port=int(port), log_level="info")
    finally:
        svc.stop(wait=False)


if __name__ == "__main__":
    main()
