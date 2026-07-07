"""Tests for the canonical rixi framing (src/rixi/crypto.py)."""
import os

from rixi.crypto import NONCE_LEN, decrypt, encrypt, frame, iter_frames


def _chunked(blob: bytes, size: int):
    return (blob[i:i + size] for i in range(0, len(blob), size))


def test_encrypt_decrypt_round_trip():
    key = os.urandom(32)
    ct = encrypt(key, b"secret payload")
    assert ct[:NONCE_LEN] != b"secret payload"[:NONCE_LEN]
    assert decrypt(key, ct) == b"secret payload"


def test_plaintext_passthrough_when_no_key():
    assert encrypt(None, b"hi") == b"hi"
    assert decrypt(None, b"hi") == b"hi"


def test_iter_frames_encrypted_split_chunks():
    key = os.urandom(32)
    payloads = [b'{"output":"a"}', b'{"status":"ok"}', b'{"error":"x"}']
    wire = b"".join(frame(key, p) for p in payloads)
    # 3-byte chunks force partial-frame buffering across reads.
    assert list(iter_frames(key, _chunked(wire, 3))) == payloads


def test_iter_frames_plaintext():
    wire = frame(None, b"line1") + frame(None, b"line2")
    assert list(iter_frames(None, _chunked(wire, 4))) == [b"line1", b"line2"]
