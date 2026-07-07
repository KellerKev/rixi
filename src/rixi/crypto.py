"""Canonical AES-256-GCM framing shared by the rixi SDK.

This is the single source of truth for the length-prefixed encrypted frame format the
rixi server streams back:

    frame = len(4, big-endian) || nonce(12) || AES-256-GCM(payload)

When no AES key is negotiated the payload is sent in the clear (still length-prefixed).
Components historically copy-pasted these helpers; new code should import from here.
"""
from __future__ import annotations

import os
from typing import Iterator, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12


def encrypt(key: Optional[bytes], data: bytes) -> bytes:
    """Return nonce||ciphertext, or the plaintext unchanged when key is None."""
    if not key:
        return data
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(key: Optional[bytes], blob: bytes) -> bytes:
    """Inverse of encrypt(); returns blob unchanged when key is None."""
    if not key:
        return blob
    return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], None)


def frame(key: Optional[bytes], data: bytes) -> bytes:
    """Length-prefix an (optionally encrypted) payload for the wire."""
    enc = encrypt(key, data)
    return len(enc).to_bytes(4, "big") + enc


def iter_frames(key: Optional[bytes], chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Decode a stream of 4-byte-length-prefixed frames into decrypted payloads.

    Feed it the raw chunks from an HTTP response (``resp.iter_content``); it yields one
    decrypted payload per complete frame, buffering partial frames across chunks.
    """
    buf = b""
    need: Optional[int] = None
    for chunk in chunks:
        buf += chunk
        while True:
            if need is None:
                if len(buf) < 4:
                    break
                need = int.from_bytes(buf[:4], "big")
                buf = buf[4:]
            if len(buf) < need:
                break
            enc, buf = buf[:need], buf[need:]
            need = None
            yield decrypt(key, enc)
