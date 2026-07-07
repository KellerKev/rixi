"""Pluggable gateway 'fire actions'.

An action provisions/operates infrastructure and wires the result back into the gateway.
The built-in action provisions compute via OpenTofu (see provision.py).
"""
from __future__ import annotations

from typing import Any, Callable, Dict

_REGISTRY: Dict[str, Callable] = {}


def register_action(name: str):
    def deco(fn: Callable):
        _REGISTRY[name] = fn
        return fn
    return deco


async def dispatch(name: str, args: dict, gateway: Any):
    fn = _REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"unknown action: {name}")
    return await fn(args, gateway)


# Import actions so they self-register.
from . import provision  # noqa: E402,F401
