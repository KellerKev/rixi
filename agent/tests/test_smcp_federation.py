"""SMCP A2A federation port — conformance against the cross-language vectors.

``federation_conformance_vectors.json`` is the same file the Python reference
(smcp) and the Rust implementation (malgra) verify against. This proves rixi's
self-contained port reproduces the proof canonicalization/signing, ECDH session
derivation, AES-GCM (session-id AAD) framing, PS256 proof verification, and RS256
client-token verification byte-for-byte, plus a full sender↔receiver round-trip.
"""
import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("jwt")
import smcp_federation as fed  # noqa: E402  (agent/ on sys.path via conftest)

VECTORS = json.loads((Path(__file__).parent / "federation_conformance_vectors.json").read_text())


def test_proof_canonical_and_hmac_vector():
    proof = {
        "client_jwt": "CJWT", "forwarded_by": "nodeA", "forwarded_at": 1700000000.0,
        "task_hash": "abc", "forwarded_to": "nodeB", "nonce": "nonce-1", "expires_at": 1700000300.0,
    }
    canonical = fed.canonical_proof(proof)
    assert canonical == VECTORS["proof_canonical_message"]
    secret = VECTORS["proof_hmac"]["secret"]
    assert fed.hmac_sign_proof(secret, canonical) == VECTORS["proof_hmac"]["signature"]
    assert fed.hmac_verify_proof(secret, canonical, VECTORS["proof_hmac"]["signature"])
    assert not fed.hmac_verify_proof("wrong", canonical, VECTORS["proof_hmac"]["signature"])


def test_ecdh_session_vector():
    from cryptography.hazmat.primitives.asymmetric import ec
    e = VECTORS["ecdh"]
    a = ec.derive_private_key(int(e["priv_a_scalar_hex"], 16), ec.SECP256R1())
    pb = bytes.fromhex(e["pub_b_x962_hex"])
    key = fed.derive_ecdh_session(a, pb, e["node_a"], e["node_b"])
    assert key.hex() == e["session_key_hex"]


def test_gcm_aad_vector_and_roundtrip():
    g = VECTORS["gcm_aad"]
    key = bytes.fromhex(g["key_hex"])
    nonce = bytes.fromhex(g["nonce_hex"])
    ct_hex, tag_hex = fed.encrypt_session(key, nonce, g["plaintext_utf8"].encode(), g["session_id"])
    assert ct_hex == g["ciphertext_hex"]
    assert tag_hex == g["tag_hex"]
    pt = fed.decrypt_session(key, nonce, bytes.fromhex(g["ciphertext_hex"]),
                             bytes.fromhex(g["tag_hex"]), g["session_id"])
    assert pt.decode() == g["plaintext_utf8"]
    with pytest.raises(Exception):
        fed.decrypt_session(key, nonce, bytes.fromhex(g["ciphertext_hex"]),
                            bytes.fromhex(g["tag_hex"]), "attacker:victim")


def test_ps256_verify_vector():
    p = VECTORS["ps256"]
    assert fed.verify_ps256_proof(p["public_key_pem"], p["canonical"], p["signature_hex"])
    assert not fed.verify_ps256_proof(p["public_key_pem"], '{"tampered":true}', p["signature_hex"])


def test_rs256_token_vector():
    r = VECTORS["rs256_token"]
    claims = fed.verify_rs256_token(r["public_key_pem"], r["token"], r["issuer"], r["audience"])
    assert claims["user"] == r["user"]
    with pytest.raises(Exception):
        fed.verify_rs256_token(r["public_key_pem"], r["token"], r["issuer"], "other")


def test_validator_binding_and_replay():
    v = fed.ProofValidator("nodeB", "sekret")

    def signed(target, signer, nonce):
        proof = {
            "client_jwt": "CJWT", "forwarded_by": signer, "forwarded_at": 1700000000.0,
            "task_hash": "abc", "forwarded_to": target, "nonce": nonce, "expires_at": 4102444800.0,
        }
        sig = fed.hmac_sign_proof("sekret", fed.canonical_proof(proof))
        return {"proof": proof, "signature": sig, "sig_alg": "HS256"}

    assert v.verify(signed("nodeB", "nodeA", "n-1"), "nodeA")["forwarded_by"] == "nodeA"
    with pytest.raises(ValueError):  # wrong target
        v.verify(signed("nodeC", "nodeA", "n-2"), "nodeA")
    with pytest.raises(ValueError):  # empty target
        v.verify(signed("", "nodeA", "n-3"), "nodeA")
    with pytest.raises(ValueError):  # from_node mismatch
        v.verify(signed("nodeB", "nodeA", "n-4"), "attacker")
    s = signed("nodeB", "nodeA", "n-5")
    assert v.verify(s, "nodeA")
    with pytest.raises(ValueError):  # replay
        v.verify(s, "nodeA")


def test_validator_pins_registered_signer_to_ps256():
    v = fed.ProofValidator("nodeB", "sekret")
    v.register_peer_public_key("nodeA", VECTORS["ps256"]["public_key_pem"])
    proof = {
        "client_jwt": "CJWT", "forwarded_by": "nodeA", "forwarded_at": 1700000000.0,
        "task_hash": "abc", "forwarded_to": "nodeB", "nonce": "n-6", "expires_at": 4102444800.0,
    }
    signed = {"proof": proof, "signature": fed.hmac_sign_proof("sekret", fed.canonical_proof(proof)),
              "sig_alg": "HS256"}
    with pytest.raises(ValueError):  # HMAC from a registered-key signer is a downgrade
        v.verify(signed, "nodeA")


def test_sender_receiver_roundtrip_hmac():
    async def run():
        receiver = fed.FederationReceiver("nodeB", "shared-secret")

        async def invoke(tool_name, **params):
            if tool_name == "federated_key_exchange":
                return receiver.key_exchange(params)
            if tool_name == "federated_forward":
                return receiver.forward(params)
            raise AssertionError(tool_name)

        task = {"type": "ai_reasoning", "task_id": "t1", "prompt": "hi"}
        result = await fed.forward_request(invoke, "nodeA", "nodeB", task,
                                           client_jwt="unused", hmac_secret="shared-secret")
        assert result["status"] == "success"
        assert result["processed_by"] == "nodeB"
        assert result["task_type"] == "ai_reasoning"

    asyncio.run(run())


def test_receiver_requires_key_exchange_first():
    receiver = fed.FederationReceiver("nodeB", "shared-secret")
    with pytest.raises(ValueError):
        receiver.forward({"from_node": "nodeA", "encrypted_request": {
            "encrypted_data": "00", "nonce": "0" * 24, "tag": "00", "session_id": "nodeA:nodeB"}})


def test_server_dispatch_federation_end_to_end():
    """Drive the real SMCPToolServer dispatch: a federation-enabled rixi node
    answers federated_key_exchange + federated_forward over the signed/encrypted
    tool_invoke channel, using the sender helper end-to-end."""
    import smcp as rixi_smcp

    SECRET = "integration-secret-value-32-bytes-x"
    server = rixi_smcp.SMCPToolServer(
        {}, secret_key=SECRET, api_key="k", node_id="nodeB",
        federation_enabled=True, federation_hmac_secret="fed-shared-secret")
    fernet, mac = rixi_smcp._derive_keys(SECRET, "")
    conn_fed = server._new_fed_receiver()
    token = server._issue_token("nodeA")

    async def invoke(tool_name, **params):
        env = rixi_smcp._envelope(mac, fernet, "tool_invoke",
                                  {"token": token, "tool_name": tool_name, "parameters": params}, True)
        resp = await server.handle_message(env, fed=conn_fed)
        out = rixi_smcp._decrypt(fernet, resp)
        if resp.get("type") == "error":
            raise RuntimeError(out.get("error") if isinstance(out, dict) else out)
        return out["result"]

    async def run():
        task = {"type": "storage", "task_id": "t9", "data": "x"}
        result = await fed.forward_request(invoke, "nodeA", "nodeB", task,
                                           client_jwt="unused", hmac_secret="fed-shared-secret")
        assert result["status"] == "success"
        assert result["processed_by"] == "nodeB"
        assert result["task_type"] == "storage"

    asyncio.run(run())


def test_server_rejects_federation_when_disabled():
    import smcp as rixi_smcp

    SECRET = "integration-secret-value-32-bytes-x"
    server = rixi_smcp.SMCPToolServer({}, secret_key=SECRET, api_key="k", node_id="nodeB")
    fernet, mac = rixi_smcp._derive_keys(SECRET, "")
    token = server._issue_token("nodeA")

    async def run():
        env = rixi_smcp._envelope(mac, fernet, "tool_invoke",
                                  {"token": token, "tool_name": "federated_key_exchange",
                                   "parameters": {"peer_node": "nodeA", "peer_pub_hex": "00"}}, True)
        resp = await server.handle_message(env, fed=None)
        assert resp.get("type") == "error"
        err = rixi_smcp._decrypt(fernet, resp)
        assert "federation not enabled" in (err.get("error") if isinstance(err, dict) else str(err))

    asyncio.run(run())
