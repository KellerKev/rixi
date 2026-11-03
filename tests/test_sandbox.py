"""rixi.Sandbox: create → drive → destroy as one object, over a fake cloud and a fake box."""
import pytest

from rixi import ExecResult, Sandbox
from rixi import sandbox as sandbox_mod
from rixi.cloud import up as up_mod
from rixi.cloud.profiles import Profile, ProfileStore
from rixi.cloud.providers import BoxInfo


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RIXI_CONFIG_DIR", str(tmp_path / "rixi"))
    monkeypatch.delenv("RIXI_PROFILE", raising=False)


class Cloud:
    supports_policy = True

    def __init__(self):
        self.created, self.destroyed, self.applied = [], [], []

    def create(self, spec, region):
        self.created.append(spec.name)
        return BoxInfo("hetzner", spec.name, "srv-1", "192.0.2.7", region, spec.instance_type)

    def destroy(self, name, region=None):
        self.destroyed.append(name)

    def apply_firewall(self, name, region, compiled):
        self.applied.append(name)


class Box:
    """The box's API as the Sandbox sees it, through Profile.client()."""

    def __init__(self):
        self.files = {"/run/systemd/resolve/resolv.conf": "nameserver 10.1.0.2\n"}

    def health(self):
        return {"status": "ok"}

    def handshake(self, secret):
        return "Q" * 44

    def exec(self, cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "cat":
            text = self.files.get(cmd[1], "")
            return ExecResult(exit_code=0 if text else 1, status="completed", stdout=text)
        return ExecResult(exit_code=0, status="completed", stdout=f"ran {cmd}\n")

    def write_file(self, path, data):
        self.files[path] = data
        return {"path": path}

    def read_file(self, path):
        return self.files[path].encode()


@pytest.fixture
def fake(monkeypatch):
    cloud, box = Cloud(), Box()
    monkeypatch.setattr(up_mod, "make_provider", lambda provider, creds: cloud)
    monkeypatch.setattr(up_mod, "_check_ref", lambda ref: None)
    monkeypatch.setattr(Profile, "client", lambda self, **_: box)
    yield cloud, box
    sandbox_mod._owned.clear()


def test_a_sandbox_is_created_driven_and_destroyed(fake):
    cloud, box = fake
    with Sandbox.create(provider="hetzner", policy={"egress": {"mode": "block"}},
                        creds={"token": "t"}) as sb:
        assert sb.name.startswith("sb-") and sb.policy["egress"]["mode"] == "block"
        assert cloud.applied == [sb.name]                       # sealed before it is handed over
        sb.write_file("main.py", "print(42)")
        assert sb.read_file("main.py") == b"print(42)"
        assert sb.exec("python3 main.py").stdout == "ran python3 main.py\n"
        name = sb.name
    assert cloud.destroyed == [name] and sb.destroyed
    assert not ProfileStore().exists(name)                      # the profile is gone too
    with pytest.raises(RuntimeError, match="destroyed"):
        sb.exec("true")


def test_a_failing_agent_still_destroys_its_sandbox(fake):
    cloud, _ = fake
    with pytest.raises(ZeroDivisionError):
        with Sandbox.create(provider="hetzner", creds={"token": "t"}) as sb:
            1 / 0
    assert cloud.destroyed == [sb.name]


def test_a_forgotten_sandbox_is_destroyed_at_exit(fake):
    cloud, _ = fake
    sb = Sandbox.create(provider="hetzner", creds={"token": "t"})
    sandbox_mod._destroy_leftovers()
    assert cloud.destroyed == [sb.name] and sandbox_mod._owned == {}
    sb.destroy()                                               # twice is fine
    assert cloud.destroyed == [sb.name]


def test_attach_drives_an_existing_profile_without_owning_it(fake):
    cloud, _ = fake
    made = Sandbox.create(provider="hetzner", name="keeper", creds={"token": "t"})
    sandbox_mod._owned.clear()                                 # as if from another process
    with Sandbox.attach("keeper") as sb:
        assert sb.exec("ls").ok
    assert cloud.destroyed == [] and not sb.owned
    made.destroy()
