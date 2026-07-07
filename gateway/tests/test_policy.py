# Policy engine tests — admin floor / user tighten, RBAC, quotas, security resolution.
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.auth import ANON, Identity  # noqa: E402
from gateway.config import ResourceDef, parse_global_policy  # noqa: E402
from gateway.policy import (  # noqa: E402
    GlobalPolicy, PolicyEngine, PolicyViolation, Quotas, ResourcePolicy, RolePolicy, UsageView,
    tighten_allowlist, tighten_bool, tighten_quota,
)

USER = Identity(sub="alice", roles=("user",))
ML = Identity(sub="bob", roles=("ml",))
ADMIN = Identity(sub="root", roles=("admin",))


def _glob(**kw):
    base = dict(
        admin_role="admin", default_role="user",
        roles={"user": RolePolicy(allowed_ops=("list", "request_compute", "route"),
                                  allowed_providers=("dummy",)),
               "ml": RolePolicy(allowed_ops=("request_compute", "route")),
               "admin": RolePolicy(allowed_ops=("*",))},
        enabled=True)
    base.update(kw)
    return GlobalPolicy(**base)


def _res(name="gpu", provider="dummy", region=None, policy=None):
    return ResourceDef(name=name, provider=provider,
                       vars={"region": region} if region else {}, policy=policy)


# ---- tighten combinators --------------------------------------------------

def test_tighten_primitives():
    assert tighten_bool(True, False) is True        # admin floor wins
    assert tighten_bool(False, True) is True        # user may opt in
    assert tighten_bool(False, None) is False
    assert tighten_quota(5, 2) == 2 and tighten_quota(None, 3) == 3 and tighten_quota(4, None) == 4
    assert tighten_allowlist(("a", "b"), ("a",)) == ("a",)     # narrow ok
    assert tighten_allowlist(None, ("x",)) == ("x",)
    with pytest.raises(PolicyViolation):
        tighten_allowlist(("a",), ("a", "b"))                  # widen rejected


# ---- RBAC / admit ---------------------------------------------------------

def test_anon_denied_under_require_jwt():
    eng = PolicyEngine(_glob(require_jwt=True), {})
    assert eng.evaluate(ANON, "list").allow is False
    # admin (even if somehow anon-less) and non-jwt floor allow
    assert PolicyEngine(_glob(require_jwt=False), {}).evaluate(ANON, "list").allow is True


def test_op_allowed_per_role():
    eng = PolicyEngine(_glob(), {})
    assert eng.evaluate(USER, "request_compute").allow is True
    assert eng.evaluate(USER, "teardown").allow is False        # not in user's allowed_ops
    assert eng.evaluate(ADMIN, "teardown").allow is True        # admin bypass ("*")


def test_provider_allowlist():
    eng = PolicyEngine(_glob(), {})
    assert eng.evaluate(USER, "request_compute", resource=_res(provider="dummy")).allow is True
    d = eng.evaluate(USER, "request_compute", resource=_res(provider="scaleway"))
    assert d.allow is False and "provider" in d.reason


def test_resource_allowed_roles_and_region():
    pol = ResourcePolicy(allowed_roles=("ml",), allowed_regions=("fr-par-2",))
    eng = PolicyEngine(_glob(), {})
    res_ok = _res(provider="dummy", region="fr-par-2", policy=pol)
    assert eng.evaluate(ML, "request_compute", resource=res_ok).allow is True
    assert eng.evaluate(USER, "request_compute", resource=res_ok).allow is False  # role not allowed
    res_bad_region = _res(provider="dummy", region="us-east", policy=pol)
    assert eng.evaluate(ML, "request_compute", resource=res_bad_region).allow is False
    assert eng.evaluate(ADMIN, "request_compute", resource=res_bad_region).allow is True  # admin bypass


def test_quota_max_concurrent():
    eng = PolicyEngine(_glob(quotas=Quotas(max_concurrent_resources=2)), {})
    res = _res()
    usage = UsageView(per_identity_concurrent={"alice": 2})
    assert eng.evaluate(USER, "request_compute", resource=res, usage=usage).allow is False
    usage2 = UsageView(per_identity_concurrent={"alice": 1})
    assert eng.evaluate(USER, "request_compute", resource=res, usage=usage2).allow is True


# ---- security resolution (the enforcement floor) --------------------------

def test_security_floor_and_user_tighten():
    # global require_e2e off, resource floor on → enforced
    eng = PolicyEngine(_glob(), {})
    res = _res(policy=ResourcePolicy(require_e2e=True))
    d = eng.evaluate(USER, "request_compute", resource=res)
    assert d.allow and d.security.require_e2e is True
    # user cannot loosen an admin floor
    d2 = eng.evaluate(USER, "request_compute", resource=res, request={"require_e2e": False})
    assert d2.security.require_e2e is True
    # user may tighten where admin left it optional
    eng2 = PolicyEngine(_glob(require_jwt=False), {})
    d3 = eng2.evaluate(USER, "request_compute", resource=_res(), request={"require_e2e": True})
    assert d3.security.require_e2e is True
    # global require_jwt floor propagates into security
    eng3 = PolicyEngine(_glob(require_jwt=True), {})
    d4 = eng3.evaluate(USER, "request_compute", resource=_res())
    assert d4.security.require_jwt is True


# ---- config parsing -------------------------------------------------------

def test_parse_global_policy_from_toml_dict():
    g = parse_global_policy({
        "require_jwt": True, "admin_role": "ops", "default_role": "user",
        "allowed_providers": ["scaleway", "dummy"],
        "quotas": {"max_concurrent_resources": 4},
        "roles": {"user": {"allowed_ops": ["list", "route"], "quotas": {"max_concurrent_resources": 2}},
                  "ops": {"allowed_ops": ["*"]}},
    })
    assert g.enabled and g.require_jwt and g.admin_role == "ops"
    assert g.allowed_providers == ("scaleway", "dummy")
    assert g.quotas.max_concurrent_resources == 4
    assert g.roles["user"].allowed_ops == ("list", "route")
    assert g.roles["user"].quotas.max_concurrent_resources == 2
    assert parse_global_policy(None).enabled is False
