"""rixi gateway client agent — dial the gateway and reach a brokered server.

Dials OUT to the gateway (open tunnel wire format), authenticates as role="client", and exposes a
LOCAL TCP port on the client's own machine. Each local connection is opened as a routed session to a
target server `node_id`; the gateway bridges it to that server's tunnel. Point a normal rixi client
at the local port. Can also `request_compute` to provision a server on demand and auto-route to it.

    python -m gateway.client --to ws://GATEWAY:7100 --secret S --node-id me \
        --request-compute --provider dummy --bind 127.0.0.1:9100
    # then:  rixi_client.py --server http://127.0.0.1:9100 --task hello
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid

import websockets

from .protocol import AgentConn, dec, derive_keys, enc, proof


class GatewayClient:
    def __init__(self, gateway_url: str, secret: str, node_id: str = "client",
                 token: str | None = None, kdf_salt: str = ""):
        self.url = gateway_url
        self.secret = secret
        self.key, self.proof_key = derive_keys(secret, kdf_salt)
        self.node_id = node_id
        self.token = token       # JWT presented for RBAC (optional)
        self.conn: AgentConn | None = None
        self._ctrl: dict[str, asyncio.Future] = {}
        self.ready = asyncio.Event()
        self.ready_node: str | None = None
        self._loop_task: asyncio.Task | None = None

    async def connect(self):
        ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=None)
        ch = dec(await asyncio.wait_for(ws.recv(), timeout=10), self.key)
        if ch.get("type") != "challenge":
            raise RuntimeError("expected challenge")
        auth = {"type": "auth", "proof": proof(self.proof_key, ch["nonce"]),
                "node_id": self.node_id, "role": "client"}
        if self.token:
            auth["token"] = self.token
        await ws.send(enc(auth, self.key))
        self.conn = AgentConn(ws, self.key)
        self._loop_task = asyncio.create_task(self._recv_loop(ws))

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                obj = dec(raw, self.key)
                t = obj.get("type")
                if t == "session_data":
                    await self.conn.on_session_data(obj["sid"], obj["data"])
                elif t == "session_close":
                    await self.conn.close_session(obj["sid"], notify=False)
                elif t == "control":
                    self._on_control(obj)
        except Exception:
            pass

    def _on_control(self, obj):
        op = obj.get("op")
        if op == "compute_ready":
            self.ready_node = obj.get("node_id")
            self.ready.set()
            return
        fut = self._ctrl.pop(op, None)
        if fut is not None and not fut.done():
            fut.set_result(obj)

    async def _control(self, op: str, **kw):
        fut = asyncio.get_event_loop().create_future()
        self._ctrl[op] = fut
        await self.conn.send({"type": "control", "op": op, **kw})
        reply = await asyncio.wait_for(fut, timeout=30)
        if "error" in reply:
            raise RuntimeError(f"control {op} failed: {reply['error']}")
        return reply.get("result", {})

    async def list_nodes(self):
        return (await self._control("list")).get("nodes", [])

    async def request_compute(self, provider: str = "dummy", spec: dict | None = None):
        return await self._control("request_compute", provider=provider, spec=spec or {})

    async def request_resource(self, name: str, tighten: dict | None = None):
        """Ask for a named resource from the gateway catalog (by name; no cloud details here).

        `tighten` may raise the security floor (e.g. {"require_e2e": True}) — never lower it.
        """
        kw = {"resource": name}
        if tighten:
            kw["tighten"] = tighten
        return await self._control("request_compute", **kw)

    async def request_capability(self, select: dict, tighten: dict | None = None):
        """Ask for the cheapest catalog resource matching capability `select` (gpu, gpu_count,
        min_ram_gb, min_vcpu, arch, provider, region). The gateway picks the box; use wait_ready()."""
        kw = {"select": select}
        if tighten:
            kw["tighten"] = tighten
        return await self._control("request_compute", **kw)

    async def wait_ready(self, timeout: float = 600) -> str:
        await asyncio.wait_for(self.ready.wait(), timeout=timeout)
        return self.ready_node

    async def release(self, token: str):
        return await self._control("release", token=token)

    async def teardown(self, name: str):
        return await self._control("teardown", resource=name)

    async def list_resources(self):
        return (await self._control("list_resources")).get("resources", [])

    async def serve_local(self, route: str, bind_host: str = "127.0.0.1", bind_port: int = 0):
        """Expose a local TCP port; each connection is a routed session to `route` (a server node_id)."""
        async def on_local(reader, writer):
            sid = uuid.uuid4().hex
            self.conn.sessions[sid] = writer
            try:
                await self.conn.send({"type": "session_open", "sid": sid, "route": route})
            except Exception:
                writer.close()
                self.conn.sessions.pop(sid, None)
                return
            await self.conn.pump_tcp_to_ws(sid, reader)

        server = await asyncio.start_server(on_local, bind_host, bind_port)
        return server, server.sockets[0].getsockname()[1]


def _select_from_args(args) -> dict:
    """Build a capability `select` dict from the CLI flags (empty if none set)."""
    m = {"gpu": args.gpu, "gpu_count": args.gpu_count, "min_ram_gb": args.min_ram,
         "min_vcpu": args.min_vcpu, "arch": args.arch, "region": args.in_region}
    return {k: v for k, v in m.items() if v is not None}


async def _run(args):
    gc = GatewayClient(args.to, args.secret, args.node_id, token=args.token, kdf_salt=args.kdf_salt)
    await gc.connect()
    if args.teardown:
        r = await gc.teardown(args.teardown)
        print(f"🧹 teardown '{args.teardown}': {'destroyed' if r.get('destroyed') else 'nothing to do'}",
              flush=True)
        return
    route = args.route
    select = _select_from_args(args)
    if args.resource:
        r = await gc.request_resource(args.resource)
        if r.get("reused"):
            print(f"♻️  reusing resource '{args.resource}' ({r['node_id']})", flush=True)
        else:
            print(f"⏳ provisioning resource '{args.resource}'; waiting for the box…", flush=True)
        route = await gc.wait_ready()
        print(f"✅ ready: {route}", flush=True)
    elif select:
        await gc.request_capability(select)
        print(f"⏳ selecting cheapest box for {select}; waiting…", flush=True)
        route = await gc.wait_ready()
        print(f"✅ ready: {route}", flush=True)
    elif args.request_compute:
        r = await gc.request_compute(args.provider, {})
        print(f"⏳ requested compute (token {r['token'][:14]}…); waiting for the server…", flush=True)
        route = await gc.wait_ready()
        print(f"✅ server ready: {route}", flush=True)
    if not route:
        raise SystemExit("need --route, --resource, --select (--gpu/--min-ram/…), or --request-compute")
    host, _, port = args.bind.rpartition(":")
    server, lport = await gc.serve_local(route, host or "127.0.0.1", int(port))
    print(f"🔌 local {host or '127.0.0.1'}:{lport} → server '{route}' via the gateway", flush=True)
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="rixi gateway client agent")
    ap.add_argument("--to", required=True, help="gateway ws URL, e.g. ws://gateway:7100")
    ap.add_argument("--secret", default=os.getenv("RIXI_GATEWAY_SECRET"), help="shared secret")
    ap.add_argument("--node-id", default="client", help="this client's id")
    ap.add_argument("--kdf-salt", default=os.getenv("RIXI_GATEWAY_SALT", ""), help="per-deployment KDF salt (or RIXI_GATEWAY_SALT); must match peers")
    ap.add_argument("--token", default=os.getenv("RIXI_JWT"), help="JWT for RBAC (or RIXI_JWT)")
    ap.add_argument("--bind", default="127.0.0.1:9100", help="local TCP port to expose")
    ap.add_argument("--route", help="target server node_id to reach")
    ap.add_argument("--resource", help="named resource from the gateway catalog (provision/reuse, then route)")
    ap.add_argument("--teardown", help="tear down a named resource (tofu destroy) and exit")
    ap.add_argument("--request-compute", action="store_true", help="provision a server then route to it")
    ap.add_argument("--provider", default="dummy", help="provisioning provider (with --request-compute)")
    # capability-based selection: pick the cheapest catalog resource matching these
    ap.add_argument("--gpu", help="require a GPU of this type (e.g. L4)")
    ap.add_argument("--gpu-count", type=int, help="require at least this many GPUs")
    ap.add_argument("--min-ram", type=float, help="require at least this much RAM (GB)")
    ap.add_argument("--min-vcpu", type=int, help="require at least this many vCPUs")
    ap.add_argument("--arch", help="require this CPU architecture (x86|arm)")
    ap.add_argument("--in-region", dest="in_region", help="require availability in this region")
    args = ap.parse_args()
    if not args.secret:
        ap.error("a --secret (or RIXI_GATEWAY_SECRET) is required")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
