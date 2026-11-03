"""Direct mode — the gateway as a control plane only.

The gateway creates a box for a tenant, publishes its DNS name, and later tears it down. Clients
talk to the box itself over TLS that ends on the box; no workload byte ever passes through here.
What the gateway does see is metadata: the box's heartbeat (running-task count, readiness).

Every box is a row in the Store before anything exists at the provider, and every cloud resource is
tagged with its box id — so a crash at any point leaves state the reaper and reconciler can finish.

State machine:  provisioning → booting → ready → releasing → gone
                      └────────────┴────────┴──→ releasing (TTL, idle, lost heartbeat, release,
                                                            tenant stop, failed create)
"""
from __future__ import annotations

import hashlib
import json
import socket
import hmac
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from rixi import policy as netpolicy

from .. import offerings
from . import cloudinit
from .config import DirectConfig, Template
from .providers import BoxSpec
from .store import ENDED_STATES, LIVE_STATES, Box, Store, Volume

log = logging.getLogger("rixi.direct")

_TENANT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VOLNAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Denied(Exception):
    """A claim or operation was refused. `status` is the HTTP code the API should answer with."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Caller:
    sub: str
    tenant: Optional[str]
    admin: bool = False


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class DirectService:
    def __init__(self, cfg: DirectConfig, store: Store, providers: Dict[str, object], dns,
                 authorize: Optional[Callable[[dict], tuple]] = None, audit=None,
                 clock: Callable[[], float] = time.time, workers: int = 8):
        self.cfg = cfg
        self.store = store
        self.providers = providers
        self.dns = dns
        self.authorize = authorize          # payload -> (allow: bool, reason: str)
        self.audit = audit or (lambda event, **attrs: log.info("%s %s", event, attrs))
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rixi-box")
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        missing = {t.provider for t in cfg.templates.values()} - set(providers)
        if missing:
            raise ValueError(f"templates use providers with no configuration: {sorted(missing)}")

    # -- helpers ----------------------------------------------------------
    def rate(self, t: Template) -> float:
        if t.eur_per_hour is not None:
            return t.eur_per_hour
        rate = offerings.price_per_hour(t.provider, t.instance_type)
        if rate is None:
            raise Denied(f"no price known for {t.provider}/{t.instance_type}", 503)
        return rate

    def _visible(self, caller: Caller, box: Optional[Box]) -> Box:
        # Someone else's box is reported as missing, not forbidden: ids must not be probeable.
        if box is None or not (caller.admin or box.tenant == caller.tenant):
            raise NotFound()
        return box

    def _begin(self, box_id: str) -> bool:
        with self._lock:
            if box_id in self._inflight:
                return False
            self._inflight.add(box_id)
            return True

    def _end(self, box_id: str) -> None:
        with self._lock:
            self._inflight.discard(box_id)

    def busy(self, box_id: str) -> bool:
        with self._lock:
            return box_id in self._inflight

    # -- claim ------------------------------------------------------------
    def templates(self) -> List[dict]:
        out = []
        for t in self.cfg.templates.values():
            try:
                rate = self.rate(t)
            except Denied:
                rate = None
            off = offerings.lookup(t.provider, t.instance_type) or {}
            out.append({"name": t.name, "provider": t.provider, "instance_type": t.instance_type,
                        "zones": list(t.zones), "eur_per_hour": rate,
                        **{k: off[k] for k in ("vcpu", "ram_gb", "gpu", "gpu_count", "gpu_ram_gb")
                           if k in off}})
        return out

    SERVE_KINDS = ("ollama",)

    def claim(self, caller: Caller, template: str, zone: Optional[str] = None,
              ttl: Optional[float] = None, idle_timeout: Optional[float] = -1,
              ssh_key: Optional[str] = None, serve: Optional[dict] = None,
              policy: Optional[dict] = None, llm: Optional[dict] = None,
              volumes: Optional[List[str]] = None, on_idle: str = "release") -> Box:
        tenant = caller.tenant
        if not tenant or not _TENANT.match(tenant):
            raise Denied("token carries no usable tenant claim", 403)
        t = self.cfg.templates.get(template)
        if t is None:
            raise Denied(f"unknown template {template!r}", 404)
        if on_idle not in ("release", "stop"):
            raise Denied("on_idle must be release or stop", 400)
        vols = self._claimable_volumes(caller, volumes or [])
        if vols and not zone:
            zone = vols[0].zone                       # a volume lives in one zone: go to it
        zone = zone or t.zones[0]
        if zone not in t.zones:
            raise Denied(f"template {template!r} does not run in {zone}", 400)
        for v in vols:
            if v.zone != zone or v.provider != t.provider:
                raise Denied(f"volume {v.name} is in {v.zone}; the box must be there too", 400)
        if self.cfg.allowed_regions is not None and zone not in self.cfg.allowed_regions:
            raise Denied(f"region {zone} is not allowed", 403)
        lim = self.cfg.limits
        ttl = lim.default_ttl if ttl is None else float(ttl)
        if ttl <= 0 or ttl > lim.max_ttl:
            raise Denied(f"ttl must be between 1s and {lim.max_ttl:.0f}s", 400)
        if idle_timeout == -1:
            idle_timeout = lim.idle_timeout
        if ssh_key and not cloudinit.valid_ssh_key(ssh_key):
            raise Denied("ssh_key is not an OpenSSH public key", 400)
        if serve is not None:
            kind = str(serve.get("kind") or "ollama")
            if kind not in self.SERVE_KINDS:
                raise Denied(f"unknown serve kind {kind!r}", 400)
            if not cloudinit.valid_model(str(serve.get("model") or "")):
                raise Denied("serve.model is required and must be a model name", 400)
            serve = {"kind": kind, "model": str(serve["model"]),
                     "key": str(serve.get("key") or "") or secrets.token_urlsafe(32)}
        if llm is not None and not cloudinit.valid_llm(llm):
            raise Denied("llm must be {openai_base_url: https://…, anthropic_base_url?: "
                         "https://…, token: 8-256 key characters}", 400)
        rate = self.rate(t)

        live = self.store.list(tenant=tenant, live_only=True)
        if lim.max_boxes is not None and len(live) >= lim.max_boxes:
            raise Denied(f"tenant box limit reached ({lim.max_boxes})", 429)
        burn = sum(b.eur_per_hour for b in live if b.state != "stopped")
        if lim.max_eur_per_hour is not None and burn + rate > lim.max_eur_per_hour + 1e-9:
            raise Denied(f"tenant spend limit reached ({lim.max_eur_per_hour} €/h)", 429)
        if self.authorize is not None:
            allow, reason = self.authorize({
                "tenant": tenant, "sub": caller.sub, "template": t.name, "provider": t.provider,
                "instance_type": t.instance_type, "zone": zone, "eur_per_hour": rate,
                "ttl_s": ttl})
            if not allow:
                self.audit("box.denied", tenant=tenant, sub=caller.sub, template=t.name,
                           reason=reason)
                raise Denied(reason or "not authorized", 402)

        try:
            net = netpolicy.parse(policy)
        except netpolicy.PolicyError as exc:
            raise Denied(f"policy: {exc}", 400)
        if not net.is_default and not getattr(self.providers[t.provider], "supports_firewall",
                                              False):
            raise Denied(f"provider {t.provider!r} cannot enforce a network policy", 400)

        box_id = secrets.token_hex(5)
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        # Volumes are taken compare-and-set, so two claims can never get the same one.
        taken = []
        for v in vols:
            if not self.store.transition_volume(v.id, ("available",), state="attached",
                                                box_id=box_id):
                for got in taken:
                    self.store.transition_volume(got, ("attached",), state="available",
                                                 box_id=None)
                raise Denied(f"volume {v.name} is no longer available", 409)
            taken.append(v.id)
        disk_gb = t.root_volume_gb or self.cfg.root_disk_gb
        box = Box(id=box_id, tenant=tenant, owner=caller.sub, template=t.name,
                  provider=t.provider, instance_type=t.instance_type, zone=zone,
                  eur_per_hour=rate, hostname=f"b-{box_id}.{self.cfg.box_domain}",
                  state="provisioning", created_at=now, expires_at=now + ttl,
                  idle_timeout=idle_timeout, hb_hash=_hash(secret),
                  serve_kind=serve["kind"] if serve else None,
                  serve_model=serve["model"] if serve else None,
                  policy=None if net.is_default else json.dumps(net.to_dict()),
                  volumes=json.dumps([v.id for v in vols]) if vols else None, on_idle=on_idle,
                  stopped_eur_per_hour=round(disk_gb * self.cfg.volume_eur_per_gb_hour, 6))
        user_data = cloudinit.render(self.cfg, box_id=box_id, tenant=tenant,
                                     hostname=box.hostname, box_secret=secret, ssh_key=ssh_key,
                                     serve=serve, llm=llm,
                                     volumes=[(v.provider_id, v.name) for v in vols])
        self.store.insert(box)
        self.audit("box.claimed", box=box_id, tenant=tenant, sub=caller.sub, template=t.name,
                   zone=zone, eur_per_hour=rate)
        self._begin(box_id)
        self._pool.submit(self._provision, box, t, user_data)
        if serve:
            box.endpoint_key = serve["key"]      # returned once; never stored
        return box

    def _provision(self, box: Box, t: Template, user_data: str) -> None:
        try:
            provider = self.providers[box.provider]

            def on_ip(ip: str) -> None:
                self.store.update(box.id, ip=ip)
                self.dns.set_a(box.hostname, ip)
                # <port>.<hostname>: preview URLs for ports the box chooses to open. Each gets
                # its own certificate on the box, issued only for ports opened as previews.
                self.dns.set_a("*." + box.hostname, ip)
                # Boot only once every authoritative nameserver serves the new record: the box
                # asks for its certificate seconds after boot, and a zone wildcard would
                # otherwise answer for it and have the challenge served by the wrong host.
                if not self.dns.wait_published(box.hostname, ip, self.cfg.dns_publish_timeout):
                    self.audit("box.dns_slow", box=box.id, tenant=box.tenant,
                               hostname=box.hostname)

            vols = [self.store.get_volume(v) for v in box.volume_ids()]
            created = provider.create(BoxSpec(
                box_id=box.id, tenant=box.tenant, instance_type=box.instance_type,
                zone=box.zone, image=t.image, user_data=user_data,
                root_volume_gb=t.root_volume_gb or (self.cfg.root_disk_gb if vols else None),
                dedicated_firewall=bool(box.policy),
                volumes=[v.provider_id for v in vols if v is not None]), on_ip)
            if created.firewall_id:
                self.store.update(box.id, firewall_id=created.firewall_id)
            if self.store.transition(box.id, ("provisioning",), state="booting",
                                     provider_id=created.provider_id, ip=created.ip):
                self.audit("box.provisioned", box=box.id, tenant=box.tenant, ip=created.ip)
            else:
                self.store.update(box.id, provider_id=created.provider_id, ip=created.ip)
        except Exception as exc:
            log.exception("provisioning %s failed", box.id)
            self.store.update(box.id, error=str(exc)[:500])
            self.store.transition(box.id, ("provisioning",), state="releasing",
                                  end_reason="provision_failed")
            self.audit("box.provision_failed", box=box.id, tenant=box.tenant, error=str(exc)[:200])
        # Released while we were creating it (or the create failed): hand the in-flight claim
        # straight to the destroy job, so the reaper never sees the box idle in between.
        try:
            cur = self.store.get(box.id)
        except Exception:
            cur = None
        if cur is not None and cur.state == "releasing":
            self._pool.submit(self._destroy, cur)
        else:
            self._end(box.id)

    # -- release ----------------------------------------------------------
    def release(self, box_id: str, reason: str, caller: Optional[Caller] = None) -> Box:
        box = self.store.get(box_id)
        if caller is not None:
            box = self._visible(caller, box)
        elif box is None:
            raise NotFound()
        if box.state in ENDED_STATES or box.state == "releasing":
            return box
        if self.store.transition(box_id, ("provisioning", "booting", "ready", "stopping",
                                          "stopped", "starting"),
                                 state="releasing", end_reason=reason):
            self.audit("box.releasing", box=box_id, tenant=box.tenant, reason=reason,
                       by=caller.sub if caller else "system")
            self._schedule_destroy(self.store.get(box_id))
        return self.store.get(box_id)

    def _schedule_destroy(self, box: Box) -> None:
        if self._begin(box.id):
            self._pool.submit(self._destroy, box)

    def _destroy(self, box: Box) -> None:
        try:
            self.providers[box.provider].destroy(box.id, box.zone)
            try:
                self.dns.delete_a(box.hostname)
                self.dns.delete_a("*." + box.hostname)
            except Exception as exc:          # a stale record is harmless; the box is gone
                log.warning("dns cleanup for %s failed: %s", box.id, exc)
            for vid in box.volume_ids():          # the volume outlives the box
                self.store.transition_volume(vid, ("attached",), state="available", box_id=None)
            final = "failed" if box.end_reason == "provision_failed" else "gone"
            if self.store.transition(box.id, ("releasing",), state=final, ended_at=self.clock()):
                self.audit("box.gone", box=box.id, tenant=box.tenant, reason=box.end_reason,
                           minutes=round((self.clock() - box.created_at) / 60, 2))
        except Exception as exc:
            # Stays in 'releasing' — still billed, and retried by the reaper.
            log.exception("destroying %s failed", box.id)
            self.store.update(box.id, error=f"destroy: {exc}"[:500])
        finally:
            self._end(box.id)

    def stop_tenant(self, tenant: str, reason: str = "tenant_stopped") -> List[Box]:
        return [self.release(b.id, reason) for b in self.store.list(tenant=tenant, live_only=True)]

    # -- reads ------------------------------------------------------------
    def get(self, caller: Caller, box_id: str) -> Box:
        return self._visible(caller, self.store.get(box_id))

    def list(self, caller: Caller, tenant: Optional[str] = None,
             live_only: bool = False) -> List[Box]:
        if not caller.admin:
            tenant = caller.tenant
            if not tenant:
                return []
        return self.store.list(tenant=tenant, live_only=live_only)

    def usage(self, caller: Caller, since: float, tenant: Optional[str] = None) -> List[dict]:
        if not caller.admin:
            tenant = caller.tenant
        now = self.clock()
        out = []
        for b in self.store.usage_since(since, tenant=tenant):
            end = b.billed_until(now)
            out.append({"box": b.id, "tenant": b.tenant, "template": b.template,
                        "provider": b.provider, "instance_type": b.instance_type,
                        "zone": b.zone, "eur_per_hour": b.eur_per_hour, "state": b.state,
                        "started_at": b.created_at, "ended_at": b.ended_at,
                        "billed_until": end,
                        # compute is billed while it runs; a stopped box only for its disk
                        "minutes": round(b.running_seconds(now) / 60, 4),
                        "stopped_minutes": round(b.stopped_seconds(now) / 60, 4),
                        "stopped_eur_per_hour": b.stopped_eur_per_hour or 0.0})
        return out

    def volume_usage(self, caller: Caller, since: float,
                     tenant: Optional[str] = None) -> List[dict]:
        if not caller.admin:
            tenant = caller.tenant
        now = self.clock()
        out = []
        for v in self.store.list_volumes(tenant=tenant, since=since):
            end = v.deleted_at if v.deleted_at is not None else now
            out.append({"volume": v.id, "tenant": v.tenant, "name": v.name, "zone": v.zone,
                        "size_gb": v.size_gb, "eur_per_gb_hour": v.eur_per_gb_hour,
                        "state": v.state, "created_at": v.created_at, "deleted_at": v.deleted_at,
                        "gb_hours": round(v.size_gb * max(0.0, end - v.created_at) / 3600, 4)})
        return out

    def revoke(self, caller: Caller, box_id: str, jti: str) -> None:
        box = self._visible(caller, self.store.get(box_id))
        if not jti or len(jti) > 128:
            raise Denied("bad jti", 400)
        self.store.revoke(box.id, jti)
        self.audit("token.revoked", box=box.id, tenant=box.tenant, by=caller.sub)

    # -- heartbeat --------------------------------------------------------
    def heartbeat(self, box_id: str, secret: str, active_tasks: int, tls_ready: bool,
                  requests: int = 0, model_ready: bool = False,
                  resolvers: Optional[List[str]] = None) -> dict:
        box = self.store.get(box_id)
        if box is None or not secret or not hmac.compare_digest(_hash(secret), box.hb_hash):
            raise Denied("unknown box or bad credential", 401)
        if not box.live or box.state in ("releasing", "stopping", "stopped"):
            return {"state": box.state, "revoked_jti": []}
        if box.state == "starting" and tls_ready:
            now = self.clock()
            self.store.update(box_id, last_heartbeat=now, last_busy=now)
            if self.store.transition(box_id, ("starting",), state="ready", ready_at=now):
                self.audit("box.started", box=box_id, tenant=box.tenant)
            return {"state": "ready", "revoked_jti": self.store.revoked(box_id)}
        now = self.clock()
        requests = max(0, int(requests))
        changes = {"last_heartbeat": now, "active_tasks": max(0, int(active_tasks))}
        if active_tasks > 0 or requests > 0:
            changes["last_busy"] = now
        if requests:
            changes["requests_total"] = (box.requests_total or 0) + requests
            changes["last_request"] = now
        if box.serve_kind and model_ready and not box.model_ready:
            changes["model_ready"] = 1
        if resolvers and not box.policy_applied:
            clean = [r for r in resolvers if isinstance(r, str)][:4]
            changes["resolvers"] = json.dumps(clean)
        self.store.update(box_id, **changes)
        # A served box is only usable once its model has finished downloading.
        serving_ready = (not box.serve_kind) or model_ready or bool(box.model_ready)
        if box.state == "booting" and tls_ready and serving_ready and box.policy \
                and not box.policy_applied:
            # The box is otherwise ready, but nothing may run on it before its policy is live:
            # apply it now, and call the box ready on a later beat.
            self._schedule_policy(self.store.get(box_id))
        elif box.state == "booting" and tls_ready and serving_ready:
            if self.store.transition(box_id, ("booting",), state="ready", ready_at=now,
                                     last_busy=now):
                self.audit("box.ready", box=box_id, tenant=box.tenant,
                           boot_s=round(now - box.created_at, 1))
        return {"state": self.store.get(box_id).state, "revoked_jti": self.store.revoked(box_id)}

    # -- stop / start -----------------------------------------------------
    def stop_box(self, caller: Caller, box_id: str) -> Box:
        """Power a ready box off: its disk, name, firewall and volumes stay; compute stops."""
        box = self._visible(caller, self.store.get(box_id))
        if box.state != "ready":
            raise Denied(f"only a ready box can be stopped (this one is {box.state})", 409)
        self._schedule_stop(box)
        return self.store.get(box_id)

    def _schedule_stop(self, box: Box) -> None:
        now = self.clock()
        if not self.store.transition(box.id, ("ready", "starting"), state="stopping",
                                     stopped_at=now):
            return
        if self._begin(box.id):
            self._pool.submit(self._do_stop, box)

    def _do_stop(self, box: Box) -> None:
        try:
            self.providers[box.provider].stop(box.id, box.zone)
            self.store.transition(box.id, ("stopping",), state="stopped", active_tasks=0)
            self.audit("box.stopped", box=box.id, tenant=box.tenant)
        except Exception as exc:
            log.exception("stopping %s failed", box.id)
            cur = self.store.get(box.id)
            # still running: compute is billed again from now
            self.store.transition(box.id, ("stopping",), state="ready", stopped_at=None,
                                  stopped_total=(cur.stopped_total or 0.0) + max(
                                      0.0, self.clock() - (cur.stopped_at or self.clock())),
                                  error=f"stop: {exc}"[:500])
        finally:
            self._end(box.id)

    def start_box(self, caller: Caller, box_id: str) -> Box:
        box = self._visible(caller, self.store.get(box_id))
        if box.state != "stopped":
            raise Denied(f"only a stopped box can be started (this one is {box.state})", 409)
        lim = self.cfg.limits
        burn = sum(b.eur_per_hour for b in self.store.list(tenant=box.tenant, live_only=True)
                   if b.state != "stopped")
        if lim.max_eur_per_hour is not None and burn + box.eur_per_hour > lim.max_eur_per_hour + 1e-9:
            raise Denied(f"tenant spend limit reached ({lim.max_eur_per_hour} €/h)", 429)
        if self.authorize is not None:
            allow, reason = self.authorize({
                "tenant": box.tenant, "sub": caller.sub, "template": box.template,
                "provider": box.provider, "instance_type": box.instance_type, "zone": box.zone,
                "eur_per_hour": box.eur_per_hour, "ttl_s": 0, "action": "start"})
            if not allow:
                raise Denied(reason or "not authorized", 402)
        now = self.clock()
        paused = max(0.0, now - (box.stopped_at or now))
        if not self.store.transition(box_id, ("stopped",), state="starting", stopped_at=None,
                                     stopped_total=(box.stopped_total or 0.0) + paused,
                                     # the lifetime counts running time: push the end back
                                     expires_at=(box.expires_at + paused) if box.expires_at
                                     else None,
                                     ready_at=now, last_busy=now, error=None):
            raise Denied("the box changed state; try again", 409)
        try:
            self.providers[box.provider].start(box.id, box.zone)
        except Exception as exc:
            self.store.transition(box_id, ("starting",), state="stopped", stopped_at=now,
                                  stopped_total=(box.stopped_total or 0.0) + paused,
                                  error=f"start: {exc}"[:500])
            raise Denied(f"could not start the box: {exc}", 502)
        self.audit("box.starting", box=box_id, tenant=box.tenant, paused_s=round(paused))
        return self.store.get(box_id)

    # -- volumes ----------------------------------------------------------
    def _volume_provider(self, zone: str) -> str:
        for t in self.cfg.templates.values():
            if zone in t.zones and hasattr(self.providers[t.provider], "create_volume"):
                return t.provider
        raise Denied(f"no provider offers volumes in {zone}", 400)

    def _claimable_volumes(self, caller: Caller, ids: List[str]) -> List[Volume]:
        if len(ids) > 4:
            raise Denied("at most 4 volumes per box", 400)
        out = []
        for vid in dict.fromkeys(ids):
            v = self.store.get_volume(str(vid))
            if v is None or not (caller.admin or v.tenant == caller.tenant):
                raise Denied(f"no volume {vid!r}", 404)
            if v.state != "available":
                raise Denied(f"volume {v.name} is {v.state}, not available", 409)
            out.append(v)
        return out

    def create_volume(self, caller: Caller, name: str, size_gb: int,
                      zone: Optional[str] = None) -> Volume:
        tenant = caller.tenant
        if not tenant or not _TENANT.match(tenant):
            raise Denied("token carries no usable tenant claim", 403)
        if not isinstance(name, str) or not _VOLNAME.match(name):
            raise Denied("name: lowercase letters, digits and '-', up to 40", 400)
        lim = self.cfg.limits
        if isinstance(size_gb, bool) or not isinstance(size_gb, int) or \
                not 1 <= size_gb <= lim.max_volume_gb:
            raise Denied(f"size_gb must be 1-{lim.max_volume_gb}", 400)
        zones = [z for t in self.cfg.templates.values() for z in t.zones]
        zone = zone or zones[0]
        if zone not in zones:
            raise Denied(f"no box runs in {zone}", 400)
        live = self.store.list_volumes(tenant=tenant, live_only=True)
        if len(live) >= lim.max_volumes:
            raise Denied(f"volume limit reached ({lim.max_volumes})", 429)
        if any(v.name == name for v in live):
            raise Denied(f"you already have a volume called {name}", 409)
        v = Volume(id=secrets.token_hex(5), tenant=tenant, owner=caller.sub, name=name,
                   provider=self._volume_provider(zone), zone=zone, size_gb=size_gb,
                   eur_per_gb_hour=self.cfg.volume_eur_per_gb_hour, created_at=self.clock())
        self.store.insert_volume(v)
        self.audit("volume.created", volume=v.id, tenant=tenant, size_gb=size_gb, zone=zone)
        self._begin("vol:" + v.id)
        self._pool.submit(self._create_volume, v)
        return v

    def _create_volume(self, v: Volume) -> None:
        try:
            pid = self.providers[v.provider].create_volume(v.id, v.tenant, v.name, v.zone,
                                                           v.size_gb)
            self.store.transition_volume(v.id, ("creating",), state="available", provider_id=pid)
        except Exception as exc:
            log.exception("creating volume %s failed", v.id)
            self.store.transition_volume(v.id, ("creating",), state="failed",
                                         deleted_at=self.clock(), error=str(exc)[:500])
            try:
                self.providers[v.provider].delete_volume(v.id, v.zone)
            except Exception:
                pass
        finally:
            self._end("vol:" + v.id)

    def list_volumes(self, caller: Caller) -> List[Volume]:
        return self.store.list_volumes(tenant=None if caller.admin else caller.tenant,
                                       live_only=True)

    def delete_volume(self, caller: Caller, volume_id: str) -> Volume:
        v = self.store.get_volume(volume_id)
        if v is None or not (caller.admin or v.tenant == caller.tenant) or not v.live:
            raise NotFound()
        if not self.store.transition_volume(v.id, ("available", "failed"), state="deleting"):
            raise Denied(f"volume {v.name} is {v.state}; release its box first", 409)
        self._schedule_volume_delete(v)
        return self.store.get_volume(v.id)

    def _schedule_volume_delete(self, v: Volume) -> None:
        if self._begin("vol:" + v.id):
            self._pool.submit(self._delete_volume, v)

    def _delete_volume(self, v: Volume) -> None:
        try:
            self.providers[v.provider].delete_volume(v.id, v.zone)
            self.store.transition_volume(v.id, ("deleting",), state="deleted",
                                         deleted_at=self.clock())
            self.audit("volume.deleted", volume=v.id, tenant=v.tenant)
        except Exception as exc:
            log.exception("deleting volume %s failed", v.id)
            self.store.update_volume(v.id, error=str(exc)[:500])   # the reaper retries
        finally:
            self._end("vol:" + v.id)

    # -- network policy ---------------------------------------------------
    def control_plane_ips(self) -> List[str]:
        """Addresses the box must keep reaching: where it sends heartbeats and fetches keys."""
        if self.cfg.control_plane:
            return list(self.cfg.control_plane)
        ips = []
        for url in (self.cfg.heartbeat_url, self.cfg.box_jwks_url):
            host = urlparse(url).hostname
            try:
                ips += [ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)]
            except (socket.gaierror, TypeError):
                log.warning("cannot resolve control plane host %r", host)
        return list(dict.fromkeys(ips))

    def _schedule_policy(self, box: Box) -> None:
        if self._begin(box.id):
            self._pool.submit(self._apply_policy, box)

    def _apply_policy(self, box: Box) -> None:
        try:
            net = netpolicy.parse(json.loads(box.policy))
            if net.egress_mode != "open" and not box.resolvers:
                # Blocking egress before we know where the box resolves names would cut its DNS.
                raise RuntimeError("the box has not reported its DNS resolvers yet")
            compiled = netpolicy.compile_rules(
                net, own_ip=box.ip, control_plane=self.control_plane_ips(),
                resolvers=json.loads(box.resolvers) if box.resolvers else [])
            self.providers[box.provider].apply_firewall(box.id, box.zone, compiled)
            self.store.update(box.id, policy_applied=1)
            self.audit("box.policy_applied", box=box.id, tenant=box.tenant,
                       egress=net.egress_mode, rules=len(compiled.rules))
        except Exception as exc:
            # Stays booting: tried again on the next beat, released at the boot timeout.
            log.exception("applying the policy of %s failed", box.id)
            self.store.update(box.id, error=f"policy: {exc}"[:500])
            self.audit("box.policy_failed", box=box.id, tenant=box.tenant, error=str(exc)[:200])
        finally:
            self._end(box.id)

    # -- reaper -----------------------------------------------------------
    def reap_once(self) -> None:
        now = self.clock()
        for v in self.store.list_volumes(live_only=True):
            if v.state == "deleting" and not self.busy("vol:" + v.id):
                self._schedule_volume_delete(v)               # retry a failed delete
        for b in self.store.list(live_only=True):
            if self.busy(b.id):
                continue
            if b.state == "releasing":
                self._schedule_destroy(b)                 # retry a failed destroy
            elif b.state == "provisioning":
                self.release(b.id, "interrupted")         # create never finished (restart)
            elif b.state == "stopped":
                if b.stopped_at is not None and now - b.stopped_at > self.cfg.limits.max_stopped:
                    self.release(b.id, "stopped_too_long")
            elif b.state == "starting" and now - (b.ready_at or now) > self.cfg.boot_timeout:
                self.store.update(b.id, error="did not come back after start")
                self._schedule_stop(b)                    # don't let a failed start burn money
            elif b.state in ("stopping", "starting"):
                continue
            elif b.expires_at is not None and now >= b.expires_at:
                self.release(b.id, "ttl")
            elif b.state == "booting" and now - b.created_at > self.cfg.boot_timeout:
                self.release(b.id, "boot_timeout")
            elif b.state == "ready" and b.last_heartbeat is not None and \
                    now - b.last_heartbeat > self.cfg.heartbeat_timeout:
                self.release(b.id, "heartbeat_lost")
            elif b.state == "ready" and b.idle_timeout and b.active_tasks == 0 and \
                    now - (b.last_busy or b.ready_at or b.created_at) > b.idle_timeout:
                if (b.on_idle or "release") == "stop":
                    self._schedule_stop(b)
                    self.audit("box.idle_stop", box=b.id, tenant=b.tenant)
                else:
                    self.release(b.id, "idle")

    # -- reconciler -------------------------------------------------------
    def reconcile_once(self) -> dict:
        """Make the cloud match the store: destroy what the store does not own, and stop billing
        boxes the cloud no longer has."""
        report = {"orphans_destroyed": [], "vanished": []}
        for pname, provider in self.providers.items():
            zones = self.cfg.zones_for(pname)
            if not zones:
                continue
            seen = set()
            for m in provider.list_managed(zones):
                if m.box_id:
                    seen.add(m.box_id)
                box = self.store.get(m.box_id) if m.box_id else None
                if box is not None and (box.live or self.busy(box.id)):
                    continue
                if m.box_id is None:
                    log.warning("managed %s %s in %s has no box tag; leaving it", m.kind, m.id,
                                m.zone)
                    continue
                try:
                    provider.destroy(m.box_id, m.zone)
                    report["orphans_destroyed"].append(m.box_id)
                    self.audit("box.orphan_destroyed", box=m.box_id, kind=m.kind, zone=m.zone,
                               known=box is not None)
                except Exception as exc:
                    log.warning("orphan %s: destroy failed: %s", m.box_id, exc)
            for b in self.store.list(live_only=True):
                if b.provider != pname or b.state not in ("booting", "ready") or \
                        b.id in seen or self.busy(b.id):
                    continue
                if self.store.transition(b.id, ("booting", "ready"), state="gone",
                                         ended_at=self.clock(), end_reason="vanished"):
                    report["vanished"].append(b.id)
                    self.audit("box.vanished", box=b.id, tenant=b.tenant)
        return report

    # -- background loops -------------------------------------------------
    def start(self) -> None:
        def loop(fn, interval):
            while not self._stop.wait(interval):
                try:
                    fn()
                except Exception:
                    log.exception("%s failed", fn.__name__)
        try:
            self.reconcile_once()
        except Exception:
            log.exception("startup reconcile failed")
        self.reap_once()
        for fn, iv in ((self.reap_once, self.cfg.reap_interval),
                       (self.reconcile_once, self.cfg.reconcile_interval)):
            threading.Thread(target=loop, args=(fn, iv), daemon=True,
                             name=f"rixi-{fn.__name__}").start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._pool.shutdown(wait=wait)
