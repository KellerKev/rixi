# Audit tests — DuckDB round-trip + filters, OTel in-memory exporter, JSON fallback shape.
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.audit import (  # noqa: E402
    AUTH_OK, RES_DENIED, AuditEvent, Auditor, DuckDBSink, JsonLogSink, OtelSink,
)


def test_duckdb_roundtrip_and_filters(tmp_path):
    async def run():
        sink = DuckDBSink(str(tmp_path / "audit.duckdb"))
        au = Auditor([sink])
        await au.start()
        await au.record(AUTH_OK, "alice", "connect", decision="allow")
        await au.record(RES_DENIED, "bob", "request_compute", target="gpu",
                        decision="deny", reason="quota exceeded", attrs={"provider": "scaleway"})
        await asyncio.sleep(0.2)   # let the writer drain
        allrows = await sink.query(limit=10)
        assert len(allrows) == 2
        denies = await sink.query(decision="deny")
        assert len(denies) == 1 and denies[0]["actor"] == "bob"
        assert denies[0]["target"] == "gpu" and denies[0]["attrs"]["provider"] == "scaleway"
        by_actor = await sink.query(actor="alice")
        assert len(by_actor) == 1 and by_actor[0]["event"] == AUTH_OK
        await au.close()
    asyncio.run(run())


def test_json_sink_shape(capsys):
    async def run():
        au = Auditor([JsonLogSink()])   # StreamHandler → stderr
        await au.record(AUTH_OK, "alice", "connect", decision="allow", request_id="r1")
    asyncio.run(run())
    err = capsys.readouterr().err.strip().splitlines()[-1]
    rec = json.loads(err)
    assert rec["event"] == AUTH_OK and rec["actor"] == "alice" and rec["decision"] == "allow"
    assert rec["request_id"] == "r1" and "timestamp" in rec and rec["timestamp"].endswith("Z")


def test_otel_inmemory_exporter():
    pytest.importorskip("opentelemetry.sdk._logs")
    from opentelemetry.sdk._logs.export import InMemoryLogExporter

    async def run():
        exp = InMemoryLogExporter()
        sink = OtelSink(_exporter=exp)
        if sink._logger is None:
            pytest.skip("otel logging bridge unavailable")
        au = Auditor([sink])
        await au.record(AUTH_OK, "alice", "connect", decision="allow")
        await asyncio.sleep(0.1)
        await sink.close()        # flush the BatchLogRecordProcessor
        logs = exp.get_finished_logs()
        assert len(logs) >= 1
    asyncio.run(run())


def test_auditor_never_raises():
    class Boom:
        async def emit(self, ev):
            raise RuntimeError("sink down")
        async def close(self):
            raise RuntimeError("nope")

    async def run():
        au = Auditor([Boom()])
        ev = await au.record("x", "a", "act")   # must not raise
        assert isinstance(ev, AuditEvent)
        await au.close()
    asyncio.run(run())
