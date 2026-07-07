"""Provider-module conformance: every provider under provisioning/terraform/providers must honor
the modules/iface contract (accept the standard vars + return node_id), so a new provider is a
true drop-in. Grep-based — no tofu binary needed."""
import os

import pytest

_PROVIDERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "provisioning", "terraform", "providers")

_REQUIRED_VARS = ("gateway_ws_url", "node_id", "tunnel_secret")


def _provider_dirs():
    return [d for d in sorted(os.listdir(_PROVIDERS))
            if os.path.isfile(os.path.join(_PROVIDERS, d, "main.tf"))]


def test_at_least_the_known_providers_exist():
    dirs = set(_provider_dirs())
    assert {"dummy", "scaleway", "hetzner", "kubernetes"} <= dirs


@pytest.mark.parametrize("provider", _provider_dirs())
def test_provider_honors_iface_contract(provider):
    main = open(os.path.join(_PROVIDERS, provider, "main.tf")).read()
    for var in _REQUIRED_VARS:
        assert 'variable "%s"' % var in main, f"{provider} missing variable {var}"
    assert 'output "node_id"' in main, f"{provider} missing output node_id"
    # tunnel_secret is a credential-grade value → must be marked sensitive
    assert "sensitive" in main, f"{provider} should mark secret vars sensitive"
