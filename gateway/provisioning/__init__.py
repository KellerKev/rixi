"""OpenTofu/Terraform provisioning for the gateway (cloud-agnostic).

A provider module (terraform/providers/<name>) stands up a box and has it install the rixi server +
tunnel agent (via the open bootstrap, rendered from terraform/modules/iface/cloud-init.tftpl) and
dial the gateway with a one-time token as its node_id. tofu.py drives `tofu`/`terraform`.
"""
from .tofu import TofuRunner, find_tofu

__all__ = ["TofuRunner", "find_tofu"]
