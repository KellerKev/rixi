# Tests for the rixi reverse tunnel: encrypted framing, auth, and end-to-end TCP forwarding.
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("websockets")
from rixi_tunnel import derive_key, enc, dec, _proof, run_listen, run_connect  # noqa: E402

SECRET = "tunnel-test-secret"


def test_frame_roundtrip():
    key = derive_key(SECRET)
    obj = {"type": "session_data", "sid": "abc", "data": "ZGF0YQ=="}
    assert dec(enc(obj, key), key) == obj


def test_wrong_key_cannot_decrypt():
    obj = {"type": "x"}
    blob = enc(obj, derive_key(SECRET))
    with pytest.raises(Exception):
        dec(blob, derive_key("different"))


def test_proof_is_stable_and_keyed():
    assert _proof(SECRET, "abc") == _proof(SECRET, "abc")
    assert _proof(SECRET, "abc") != _proof("other", "abc")


async def _free_port():
    s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = s.sockets[0].getsockname()[1]
    s.close()
    await s.wait_closed()
    return port


async def _echo_target(host="127.0.0.1"):
    """A trivial upper-casing TCP server standing in for 'the rixi server'."""
    async def handle(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, host, 0)
    return server, server.sockets[0].getsockname()[1]


async def _wait_port(host, port, timeout=5.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            r, w = await asyncio.open_connection(host, port)
            w.close()
            return True
        except OSError:
            await asyncio.sleep(0.05)
    return False


def test_end_to_end_forwarding_and_concurrency():
    async def run():
        target_server, target_port = await _echo_target()
        local_port = await _free_port()
        ws_port = await _free_port()

        listen_task = asyncio.create_task(
            run_listen(f"127.0.0.1:{local_port}", f"127.0.0.1:{ws_port}", SECRET))
        connect_task = asyncio.create_task(
            run_connect(f"ws://127.0.0.1:{ws_port}", f"127.0.0.1:{target_port}", SECRET))
        try:
            assert await _wait_port("127.0.0.1", local_port)

            async def one(msg: bytes) -> bytes:
                r, w = await asyncio.open_connection("127.0.0.1", local_port)
                w.write(msg)
                await w.drain()
                out = await asyncio.wait_for(r.read(len(msg)), timeout=5)
                w.close()
                return out

            # poll until the agent has dialed in + authenticated (round-trip works)
            loop = asyncio.get_event_loop()
            end = loop.time() + 8
            while loop.time() < end:
                try:
                    if await one(b"ready?") == b"READY?":
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("tunnel never became ready")

            # single round-trip through the encrypted tunnel
            assert await one(b"hello tunnel") == b"HELLO TUNNEL"
            # several concurrent sessions multiplexed over the one tunnel
            results = await asyncio.gather(*[one(f"msg{i}".encode()) for i in range(5)])
            assert results == [f"MSG{i}".encode() for i in range(5)]
        finally:
            connect_task.cancel()
            listen_task.cancel()
            for t in (connect_task, listen_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            target_server.close()
            await target_server.wait_closed()

    asyncio.run(run())


def test_wrong_secret_is_rejected():
    async def run():
        target_server, target_port = await _echo_target()
        local_port = await _free_port()
        ws_port = await _free_port()

        listen_task = asyncio.create_task(
            run_listen(f"127.0.0.1:{local_port}", f"127.0.0.1:{ws_port}", SECRET))
        # connect with the WRONG secret → auth fails → no session forwarding
        connect_task = asyncio.create_task(
            run_connect(f"ws://127.0.0.1:{ws_port}", f"127.0.0.1:{target_port}", "WRONG-SECRET"))
        try:
            assert await _wait_port("127.0.0.1", ws_port)
            assert await _wait_port("127.0.0.1", local_port)
            await asyncio.sleep(0.5)
            # a local connection should NOT round-trip (no authenticated agent)
            r, w = await asyncio.open_connection("127.0.0.1", local_port)
            w.write(b"should not pass")
            await w.drain()
            with pytest.raises((asyncio.TimeoutError, ConnectionError)):
                got = await asyncio.wait_for(r.read(4), timeout=1.5)
                if got == b"":
                    raise ConnectionError("closed, not forwarded")
            w.close()
        finally:
            connect_task.cancel()
            listen_task.cancel()
            for t in (connect_task, listen_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            target_server.close()
            await target_server.wait_closed()

    asyncio.run(run())
