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
import hmac
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .. import offerings
from . import cloudinit
from .config import DirectConfig, Template
from .providers import BoxSpec
from .store import ENDED_STATES, LIVE_STATES, Box, Store

log = logging.getLogger("rixi.direct")

_TENANT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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

    def claim(self, caller: Caller, template: str, zone: Optional[str] = None,
              ttl: Optional[float] = None, idle_timeout: Optional[float] = -1,
              ssh_key: Optional[str] = None) -> Box:
        tenant = caller.tenant
        if not tenant or not _TENANT.match(tenant):
            raise Denied("token carries no usable tenant claim", 403)
        t = self.cfg.templates.get(template)
        if t is None:
            raise Denied(f"unknown template {template!r}", 404)
        zone = zone or t.zones[0]
        if zone not in t.zones:
            raise Denied(f"template {template!r} does not run in {zone}", 400)
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
        rate = self.rate(t)

        live = self.store.list(tenant=tenant, live_only=True)
        if lim.max_boxes is not None and len(live) >= lim.max_boxes:
            raise Denied(f"tenant box limit reached ({lim.max_boxes})", 429)
        burn = sum(b.eur_per_hour for b in live)
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

        box_id = secrets.token_hex(5)
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        box = Box(id=box_id, tenant=tenant, owner=caller.sub, template=t.name,
                  provider=t.provider, instance_type=t.instance_type, zone=zone,
                  eur_per_hour=rate, hostname=f"b-{box_id}.{self.cfg.box_domain}",
                  state="provisioning", created_at=now, expires_at=now + ttl,
                  idle_timeout=idle_timeout, hb_hash=_hash(secret))
        user_data = cloudinit.render(self.cfg, box_id=box_id, tenant=tenant,
                                     hostname=box.hostname, box_secret=secret, ssh_key=ssh_key)
        self.store.insert(box)
        self.audit("box.claimed", box=box_id, tenant=tenant, sub=caller.sub, template=t.name,
                   zone=zone, eur_per_hour=rate)
        self._begin(box_id)
        self._pool.submit(self._provision, box, t, user_data)
        return box

    def _provision(self, box: Box, t: Template, user_data: str) -> None:
        try:
            provider = self.providers[box.provider]

            def on_ip(ip: str) -> None:
                self.store.update(box.id, ip=ip)
                self.dns.set_a(box.hostname, ip)

            created = provider.create(BoxSpec(box_id=box.id, tenant=box.tenant,
                                              instance_type=box.instance_type, zone=box.zone,
                                              image=t.image, user_data=user_data,
                                              root_volume_gb=t.root_volume_gb), on_ip)
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
        if self.store.transition(box_id, ("provisioning", "booting", "ready"),
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
            except Exception as exc:          # a stale record is harmless; the box is gone
                log.warning("dns cleanup for %s failed: %s", box.id, exc)
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
                        "minutes": round(max(0.0, end - b.created_at) / 60, 4)})
        return out

    def revoke(self, caller: Caller, box_id: str, jti: str) -> None:
        box = self._visible(caller, self.store.get(box_id))
        if not jti or len(jti) > 128:
            raise Denied("bad jti", 400)
        self.store.revoke(box.id, jti)
        self.audit("token.revoked", box=box.id, tenant=box.tenant, by=caller.sub)

    # -- heartbeat --------------------------------------------------------
    def heartbeat(self, box_id: str, secret: str, active_tasks: int, tls_ready: bool) -> dict:
        box = self.store.get(box_id)
        if box is None or not secret or not hmac.compare_digest(_hash(secret), box.hb_hash):
            raise Denied("unknown box or bad credential", 401)
        if not box.live or box.state == "releasing":
            return {"state": box.state, "revoked_jti": []}
        now = self.clock()
        changes = {"last_heartbeat": now, "active_tasks": max(0, int(active_tasks))}
        if active_tasks > 0:
            changes["last_busy"] = now
        self.store.update(box_id, **changes)
        if box.state == "booting" and tls_ready:
            if self.store.transition(box_id, ("booting",), state="ready", ready_at=now,
                                     last_busy=now):
                self.audit("box.ready", box=box_id, tenant=box.tenant,
                           boot_s=round(now - box.created_at, 1))
        return {"state": self.store.get(box_id).state, "revoked_jti": self.store.revoked(box_id)}

    # -- reaper -----------------------------------------------------------
    def reap_once(self) -> None:
        now = self.clock()
        for b in self.store.list(live_only=True):
            if self.busy(b.id):
                continue
            if b.state == "releasing":
                self._schedule_destroy(b)                 # retry a failed destroy
            elif b.state == "provisioning":
                self.release(b.id, "interrupted")         # create never finished (restart)
            elif b.expires_at is not None and now >= b.expires_at:
                self.release(b.id, "ttl")
            elif b.state == "booting" and now - b.created_at > self.cfg.boot_timeout:
                self.release(b.id, "boot_timeout")
            elif b.state == "ready" and b.last_heartbeat is not None and \
                    now - b.last_heartbeat > self.cfg.heartbeat_timeout:
                self.release(b.id, "heartbeat_lost")
            elif b.state == "ready" and b.idle_timeout and b.active_tasks == 0 and \
                    now - (b.last_busy or b.ready_at or b.created_at) > b.idle_timeout:
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
