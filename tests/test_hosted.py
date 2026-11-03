"""`rixi up --provider eutonomous`: the hosted service as one more provider (fake portal API)."""
import json
import time

import pytest

from rixi import Sandbox
from rixi import sandbox as sandbox_mod
from rixi.cloud import hosted
from rixi.cloud import up as up_mod
from rixi.cloud.profiles import ProfileStore


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RIXI_CONFIG_DIR", str(tmp_path / "rixi"))
    monkeypatch.delenv("RIXI_PROFILE", raising=False)
    monkeypatch.setenv("RIXI_API_KEY", "rxk_test")


class FakeApi:
    url = "https://rixi.test"

    def __init__(self, ready_after=2, end_with=None):
        self.ready_after, self.end_with = ready_after, end_with
        self.polls, self.launched, self.released, self.minted = 0, [], [], 0

    def templates(self):
        return [{"name": "cpu-s", "eur_per_hour": 0.023, "allowed": True},
                {"name": "gpu-l4", "eur_per_hour": 0.9, "allowed": False, "reason": "GPU"}]

    def launch(self, body):
        self.launched.append(body)
        return {"id": "bx1", "url": "https://b-bx1.run.test", "zone": "fr-par-2",
                "instance_type": "DEV1-M", "eur_per_hour": 0.023, "state": "provisioning",
                "live": True, "policy": body.get("policy")}

    def box(self, box_id):
        self.polls += 1
        if self.end_with and self.polls > 1:
            return {"state": "gone", "live": False, "end_reason": self.end_with}
        return {"state": "ready" if self.polls > self.ready_after else "booting", "live": True}

    def token(self, box_id, label="rixi cli"):
        self.minted += 1
        return {"token": f"tok{self.minted}", "expires_at": time.time() + 3600}

    def release(self, box_id):
        self.released.append(box_id)
        return {"state": "releasing"}


def _up(api, **kw):
    params = dict(provider="eutonomous", size="sample-cpu", name="hosted1",
                  creds={"api_key": "rxk_test"}, _provider=api, _sleep=lambda s: None,
                  echo=lambda m: None)
    params.update(kw)
    return up_mod.up(**params)


def test_hosted_up_launches_waits_and_saves_a_token_not_the_key():
    api = FakeApi()
    profile = _up(api, policy={"egress": {"mode": "block"}}, allow_from="192.0.2.0/24",
                  ttl_hours=1.5)
    sent = api.launched[0]
    assert sent["template"] == "cpu-s" and sent["ttl"] == 5400       # sample-cpu → cpu-s
    assert sent["policy"]["egress"]["mode"] == "block"
    assert sent["policy"]["ingress"]["allow"] == ["192.0.2.0/24"]    # --allow-from rides along
    saved = ProfileStore().get("hosted1")
    assert saved.status == "ready" and saved.provider == "eutonomous"
    assert saved.server_url == "https://b-bx1.run.test" and saved.api_url == "https://rixi.test"
    stored = (profile.dir / "box_token.json").read_text()
    assert "tok1" in stored and "rxk_" not in stored                 # the key never lands here


def test_a_box_that_ends_while_starting_is_released_and_forgotten():
    api = FakeApi(end_with="boot_timeout")
    with pytest.raises(up_mod.UpError, match="boot_timeout"):
        _up(api)
    assert api.released == ["bx1"] and not ProfileStore().exists("hosted1")


def test_sizes_are_checked_against_the_service():
    with pytest.raises(up_mod.UpError, match="approval"):
        _up(FakeApi(), size="gpu-l4")
    with pytest.raises(up_mod.UpError, match="unknown size"):
        _up(FakeApi(), size="huge")


def test_the_box_token_is_renewed_through_the_api_key(monkeypatch):
    api = FakeApi()
    profile = _up(api)
    monkeypatch.setattr(hosted, "Api", lambda key, url=None: api)
    client = ProfileStore().get("hosted1").client()
    assert client.server_url == "https://b-bx1.run.test"
    assert hosted.box_token(profile) == "tok1"                       # still fresh: cached
    path = profile.dir / "box_token.json"
    path.write_text(json.dumps({"token": "tok1", "expires_at": time.time() + 60}))
    assert hosted.box_token(profile) == "tok2" and api.minted == 2   # about to expire: renewed


def test_down_releases_the_hosted_box():
    api = FakeApi()
    _up(api)
    up_mod.down("hosted1", {"api_key": "rxk_test"}, _provider=api, echo=lambda m: None)
    assert api.released == ["bx1"] and not ProfileStore().exists("hosted1")


def test_a_missing_key_says_where_to_get_one(monkeypatch):
    monkeypatch.delenv("RIXI_API_KEY")
    with pytest.raises(up_mod.UpError, match="/keys"):
        up_mod.up(provider="eutonomous", name="x", creds={}, echo=lambda m: None)


def test_a_hosted_sandbox_is_shut_down_after_the_block(monkeypatch):
    api = FakeApi()
    real_up = up_mod.up
    monkeypatch.setattr(up_mod, "up", lambda **kw: real_up(**kw, _provider=api,
                                                           _sleep=lambda s: None))
    monkeypatch.setattr(hosted, "Api", lambda key, url=None: api)
    with Sandbox.create(provider="eutonomous", size="cpu-s") as sb:
        assert sb.profile.provider == "eutonomous"
    assert api.released == ["bx1"]
    sandbox_mod._owned.clear()


def test_model_access_is_the_customers_choice_and_passes_through():
    api = FakeApi()
    direct = {"mode": "direct", "openai_base_url": "https://models.example.org/v1",
              "api_key": "sk-own"}
    _up(api, model_access=direct)
    assert api.launched[0]["model_access"] == direct
    up_mod.down("hosted1", {"api_key": "rxk_test"}, _provider=api, echo=lambda m: None)
    api2 = FakeApi()
    _up(api2, name="hosted2")                          # omitted: the service decides
    assert "model_access" not in api2.launched[0]


def test_model_access_on_your_own_cloud_points_to_exec_env():
    with pytest.raises(up_mod.UpError, match="exec --env"):
        up_mod.up(provider="hetzner", name="own", creds={"token": "t"}, model_access="governed",
                  _check=False, echo=lambda m: None)


class PersistApi(FakeApi):
    def __init__(self):
        super().__init__(ready_after=0)
        self.actions, self.vols = [], {}

    def stop(self, box_id):
        self.actions.append(("stop", box_id))

    def start(self, box_id):
        self.actions.append(("start", box_id))

    def volumes(self):
        return list(self.vols.values())

    def create_volume(self, name, size_gb, zone=None):
        v = {"id": "v1", "name": name, "size_gb": size_gb, "zone": zone or "fr-par-2",
             "state": "creating", "box_id": None}
        self.vols["v1"] = v
        return v

    def delete_volume(self, volume_id):
        self.vols.pop(volume_id)


def test_volumes_and_idle_stop_ride_along_with_the_launch():
    api = PersistApi()
    _up(api, volumes=["v1"], on_idle="stop")
    assert api.launched[0]["volumes"] == ["v1"] and api.launched[0]["on_idle"] == "stop"


def test_stop_and_start_a_hosted_profile():
    api = PersistApi()
    profile = _up(api)
    hosted.stop(profile, echo=lambda m: None, _api=api)
    hosted.start(profile, echo=lambda m: None, _api=api, _sleep=lambda s: None)
    assert api.actions == [("stop", "bx1"), ("start", "bx1")]


def test_own_cloud_boxes_do_not_take_volumes_yet():
    with pytest.raises(up_mod.UpError, match="hosted service"):
        up_mod.up(provider="hetzner", name="own", creds={"token": "t"}, volumes=["v1"],
                  _check=False, echo=lambda m: None)


def test_cli_volumes(monkeypatch, capsys):
    from rixi import _cli
    api = PersistApi()
    monkeypatch.setattr(hosted, "api_from_env", lambda url=None: api)
    assert _cli.main(["volumes", "create", "data", "--size", "50"]) == 0
    assert "v1  data  50 GB" in capsys.readouterr().out
    assert _cli.main(["volumes"]) == 0 and "data" in capsys.readouterr().out
    assert _cli.main(["volumes", "rm", "v1"]) == 0 and api.vols == {}
