"""Audit logging — OpenTelemetry-compatible, with a queryable DuckDB store.

Every security-relevant gateway action emits an AuditEvent through `Auditor.record(...)`, fanned out
to configured sinks. None of this is allowed to break the data path: each sink swallows its own
errors. Sinks:
  • JsonLogSink   — always on; structured JSON lines mirroring the open rixi server's log schema.
  • DuckDBSink    — append to a local DuckDB table for the management API / console to query.
  • OtelSink      — OTLP log export (best-effort; no-op unless an endpoint is configured).

Event taxonomy: auth.success|denied, resource.requested|provisioned|denied, session.opened|closed,
teardown, policy.changed, config.loaded.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

# --- event ----------------------------------------------------------------

# taxonomy constants
AUTH_OK = "auth.success"
AUTH_DENIED = "auth.denied"
RES_REQUESTED = "resource.requested"
RES_PROVISIONED = "resource.provisioned"
RES_DENIED = "resource.denied"
SESSION_OPENED = "session.opened"
SESSION_CLOSED = "session.closed"
TEARDOWN = "teardown"
POLICY_CHANGED = "policy.changed"
CONFIG_LOADED = "config.loaded"
SPOT_FALLBACK = "spot.fallback"        # spot capacity exhausted → provisioned on-demand instead
SPOT_INTERRUPTED = "spot.interrupted"  # a spot box was preempted (tunnel dropped); re-provisioning


@dataclass(frozen=True)
class AuditEvent:
    ts: float
    event: str
    actor: str
    action: str
    target: Optional[str] = None
    decision: str = "n/a"           # allow | deny | n/a
    reason: Optional[str] = None
    request_id: str = ""
    attrs: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        iso = datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return {"timestamp": iso, "level": "INFO", "event": self.event, "actor": self.actor,
                "action": self.action, "target": self.target, "decision": self.decision,
                "reason": self.reason, "request_id": self.request_id, "attrs": self.attrs}


# --- sinks ----------------------------------------------------------------

class JsonLogSink:
    """Structured JSON lines; mirrors the core server's JSONFormatter shape. Always-on fallback."""

    def __init__(self, path: Optional[str] = None, max_bytes: int = 10 * 1024 * 1024,
                 backups: int = 10):
        self._log = logging.getLogger("rixi.audit")
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        if not self._log.handlers:
            if path:
                from logging.handlers import RotatingFileHandler
                h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                        encoding="utf-8")
            else:
                h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(h)

    async def emit(self, ev: AuditEvent) -> None:
        self._log.info(json.dumps(ev.as_dict()))

    async def close(self) -> None:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  ts TIMESTAMP, event VARCHAR, actor VARCHAR, action VARCHAR, target VARCHAR,
  decision VARCHAR, reason VARCHAR, request_id VARCHAR, attrs JSON
)"""
_INSERT = "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?)"


class DuckDBSink:
    """Append events to DuckDB via a single writer task; reads share the connection under a lock.

    DuckDB connections are not safe for concurrent use, so ALL access (the writer loop + API
    queries) is serialized through one connection + an asyncio.Lock; writes are batched off a
    bounded queue so `emit` never blocks the tunnel (overflow drops oldest + counts).
    """

    def __init__(self, path: str, max_queue: int = 10000):
        self.path = str(path)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._lock = asyncio.Lock()
        self._conn = None
        self._task: Optional[asyncio.Task] = None
        self.dropped = 0

    async def start(self) -> None:
        import duckdb
        self._conn = await asyncio.to_thread(duckdb.connect, self.path)
        await asyncio.to_thread(self._conn.execute, _SCHEMA)
        self._task = asyncio.create_task(self._writer())

    async def emit(self, ev: AuditEvent) -> None:
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
                self.dropped += 1
                self._q.put_nowait(ev)
            except Exception:
                pass

    async def _writer(self) -> None:
        while True:
            ev = await self._q.get()
            batch = [ev]
            while not self._q.empty() and len(batch) < 256:
                try:
                    batch.append(self._q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            rows = [self._row(e) for e in batch]
            async with self._lock:
                try:
                    await asyncio.to_thread(self._conn.executemany, _INSERT, rows)
                except Exception:
                    pass

    @staticmethod
    def _row(e: AuditEvent):
        return (datetime.fromtimestamp(e.ts, tz=timezone.utc), e.event, e.actor, e.action,
                e.target, e.decision, e.reason, e.request_id, json.dumps(e.attrs))

    async def query(self, *, since=None, until=None, actor=None, event=None, decision=None,
                    target=None, limit: int = 100, offset: int = 0) -> List[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._query_sync, since, until, actor, event,
                                           decision, target, limit, offset)

    def _query_sync(self, since, until, actor, event, decision, target, limit, offset):
        where, params = [], []
        for col, val in (("actor", actor), ("event", event), ("decision", decision),
                         ("target", target)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("ts >= ?")
            params.append(datetime.fromtimestamp(float(since), tz=timezone.utc))
        if until is not None:
            where.append("ts <= ?")
            params.append(datetime.fromtimestamp(float(until), tz=timezone.utc))
        sql = "SELECT ts,event,actor,action,target,decision,reason,request_id,attrs FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        cur = self._conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["ts"] = d["ts"].replace(tzinfo=timezone.utc).isoformat() if d["ts"] else None
            if isinstance(d.get("attrs"), str):
                try:
                    d["attrs"] = json.loads(d["attrs"])
                except Exception:
                    pass
            out.append(d)
        return out

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self._conn is not None:
            async with self._lock:
                try:
                    await asyncio.to_thread(self._conn.close)
                except Exception:
                    pass


class OtelSink:
    """OTLP log export (best-effort). No-op if OpenTelemetry isn't available or unconfigured."""

    def __init__(self, endpoint: Optional[str] = None, service_name: str = "rixi-gateway",
                 _exporter=None):
        self._logger = None
        try:
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.resources import Resource
            exporter = _exporter
            if exporter is None:
                if not endpoint:
                    return
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
                exporter = OTLPLogExporter(endpoint=endpoint)
            provider = LoggerProvider(resource=Resource.create({"service.name": service_name}))
            provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
            self._provider = provider
            pylog = logging.getLogger("rixi.audit.otel")
            pylog.setLevel(logging.INFO)
            pylog.propagate = False
            pylog.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
            self._logger = pylog
        except Exception:
            self._logger = None

    async def emit(self, ev: AuditEvent) -> None:
        if self._logger is None:
            return
        try:
            self._logger.info(ev.event, extra={
                "enduser.id": ev.actor, "event.name": ev.event, "rixi.action": ev.action,
                "rixi.target": ev.target, "rixi.decision": ev.decision, "rixi.reason": ev.reason,
                "rixi.request_id": ev.request_id, **{f"rixi.{k}": v for k, v in ev.attrs.items()}})
        except Exception:
            pass

    async def close(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:
            pass


# --- auditor --------------------------------------------------------------

class Auditor:
    def __init__(self, sinks: List):
        self.sinks = sinks

    async def start(self) -> None:
        for s in self.sinks:
            start = getattr(s, "start", None)
            if start:
                try:
                    await start()
                except Exception:
                    pass

    async def record(self, event: str, actor: str, action: str, *, target=None, decision="n/a",
                     reason=None, attrs: Optional[Dict] = None, request_id=None) -> AuditEvent:
        ev = AuditEvent(ts=time.time(), event=event, actor=actor, action=action, target=target,
                        decision=decision, reason=reason,
                        request_id=request_id or uuid.uuid4().hex[:12], attrs=attrs or {})
        for s in self.sinks:
            try:
                await s.emit(ev)
            except Exception:
                pass
        return ev

    @property
    def duckdb(self) -> Optional["DuckDBSink"]:
        for s in self.sinks:
            if isinstance(s, DuckDBSink):
                return s
        return None

    async def close(self) -> None:
        for s in self.sinks:
            try:
                await s.close()
            except Exception:
                pass


def build_auditor(json_path: Optional[str] = None, duckdb_path: Optional[str] = None,
                  otlp_endpoint: Optional[str] = None) -> Auditor:
    sinks: List = [JsonLogSink(json_path)]
    if duckdb_path:
        sinks.append(DuckDBSink(duckdb_path))
    if otlp_endpoint:
        sinks.append(OtelSink(endpoint=otlp_endpoint))
    return Auditor(sinks)
