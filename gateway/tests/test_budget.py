"""Tests for budget caps (spend guardrails enforced at provision time)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.auth import Identity  # noqa: E402
from gateway.config import ResourceDef  # noqa: E402
from gateway.policy import (  # noqa: E402
    GlobalPolicy, PolicyEngine, Quotas, RolePolicy, UsageView,
)

ADMIN = Identity(sub="root", roles=("admin",))


def _engine(**quota_kw):
    glob = GlobalPolicy(roles={"admin": RolePolicy(allowed_ops=("*",))},
                        quotas=Quotas(**quota_kw), enabled=True)
    return PolicyEngine(glob, {})


# scaleway L4-1-24G is priced at 0.75 EUR/hour in offerings.toml
GPU = ResourceDef(name="gpu", provider="scaleway", vars={"instance_type": "L4-1-24G"})


def test_no_caps_allows():
    d = _engine().evaluate(ADMIN, "request_compute", resource=GPU, usage=UsageView())
    assert d.allow


def test_hourly_cap_denies_when_new_box_exceeds():
    d = _engine(max_eur_per_hour=0.5).evaluate(ADMIN, "request_compute", resource=GPU,
                                               usage=UsageView())
    assert not d.allow and "€/hour" in d.reason


def test_hourly_cap_allows_under_budget():
    d = _engine(max_eur_per_hour=1.0).evaluate(ADMIN, "request_compute", resource=GPU,
                                               usage=UsageView())
    assert d.allow


def test_hourly_cap_counts_existing_fleet_burn():
    # 0.5 already burning + a 0.75/hr box = 1.25 > 1.0 cap → deny
    d = _engine(max_eur_per_hour=1.0).evaluate(
        ADMIN, "request_compute", resource=GPU, usage=UsageView(fleet_eur_per_hour=0.5))
    assert not d.allow and "€/hour" in d.reason


def test_fleet_spend_cap_denies_when_exhausted():
    d = _engine(max_fleet_eur=10.0).evaluate(
        ADMIN, "request_compute", resource=GPU, usage=UsageView(total_eur_spent=10.0))
    assert not d.allow and "fleet spend" in d.reason


def test_budget_applies_to_admin():
    # caps are a spend guardrail: even admins are denied over budget
    d = _engine(max_eur_per_hour=0.1).evaluate(ADMIN, "request_compute", resource=GPU,
                                               usage=UsageView())
    assert not d.allow
