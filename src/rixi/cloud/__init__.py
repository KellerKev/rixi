"""`rixi up` — create a cloud box, install the rixi server, and save a connection profile.

    rixi up --provider hetzner --size sample-cpu --name mybox   # HCLOUD_TOKEN in the env
    rixi run --task train .                                     # uses the default profile
    rixi down mybox

See `rixi up --help`. The box serves plain HTTP on an open port; every request needs a short-lived
JWT signed by a key that never leaves this machine, and request + response bodies are AES-256-GCM
sealed with a key negotiated over RSA right after boot.
"""
from .profiles import Profile, ProfileError, ProfileStore
from .providers import CapacityError, ProviderError

__all__ = ["Profile", "ProfileError", "ProfileStore", "CapacityError", "ProviderError"]
