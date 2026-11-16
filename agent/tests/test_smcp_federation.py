"""SMCP A2A federation port — conformance against the cross-language vectors.

``federation_conformance_vectors.json`` is the same file the Python reference
(smcp) and the Rust implementation (malgra) verify against. This proves rixi's
self-contained port reproduces the proof canonicalization/signing, ECDH session
derivation, AES-GCM (session-id AAD) framing, PS256 proof verification, and RS256
client-token verification byte-for-byte, plus a full sender↔receiver round-trip.
"""
import asyncio
import time
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
            "task_hash": "abc", "forwarded_to": target, "nonce": nonce,
            "expires_at": time.time() + 300,
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


# ── a proof is for one task, once, for a short while ─────────────────────────

def _forward_params(receiver, task, proof_task=None, secret="shared-secret", **proof_over):
    """What a sender puts on the wire for `task`, with a proof signed for `proof_task`."""
    ephemeral, my_pub = fed.initiator_ecdh_start()
    peer_pub = receiver.key_exchange({"peer_node": "nodeA", "peer_pub_hex": my_pub})["peer_pub_hex"]
    key = fed.initiator_ecdh_finish(ephemeral, peer_pub, "nodeA", receiver.node_id)
    signed = fed.build_signed_proof("unused", task if proof_task is None else proof_task,
                                    receiver.node_id, "nodeA", hmac_secret=secret)
    if proof_over:
        signed["proof"].update(proof_over)
        signed["signature"] = fed.hmac_sign_proof(secret, fed.canonical_proof(signed["proof"]))
    payload = {"task": task, "auth_proof": signed, "forwarding_metadata": {}}
    enc = fed.encrypt_request(key, fed.session_id_for("nodeA", receiver.node_id), payload)
    return {"from_node": "nodeA", "encrypted_request": enc}, signed


def test_a_proof_carries_only_the_task_it_was_signed_for():
    ran = []
    receiver = fed.FederationReceiver("nodeB", "shared-secret",
                                      dispatch=lambda task, user, meta: ran.append(task) or "ok")
    harmless, harmful = {"type": "harmless"}, {"type": "write_file", "path": "/etc/passwd"}
    params, _ = _forward_params(receiver, harmful, proof_task=harmless)
    with pytest.raises(ValueError, match="task_hash"):
        receiver.forward(params)
    assert ran == []
    params, _ = _forward_params(receiver, harmless)
    assert receiver.forward(params) == "ok" and ran == [harmless]
    # a sender that hashes compact JSON (no spaces) is the same task
    import hashlib
    import json
    compact = hashlib.sha256(json.dumps(harmless, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
    params, _ = _forward_params(receiver, harmless, task_hash=compact)
    assert receiver.forward(params) == "ok"


def test_a_proof_used_on_one_connection_is_used_up_on_all():
    shared = {}
    first = fed.FederationReceiver("nodeB", "shared-secret", seen_nonces=shared)
    second = fed.FederationReceiver("nodeB", "shared-secret", seen_nonces=shared)   # a new connection
    task = {"type": "storage"}
    params, signed = _forward_params(first, task)
    assert first.forward(params)["status"] == "success"
    # the same signed proof, sealed afresh for the new connection
    ephemeral, my_pub = fed.initiator_ecdh_start()
    peer_pub = second.key_exchange({"peer_node": "nodeA", "peer_pub_hex": my_pub})["peer_pub_hex"]
    key = fed.initiator_ecdh_finish(ephemeral, peer_pub, "nodeA", "nodeB")
    enc = fed.encrypt_request(key, fed.session_id_for("nodeA", "nodeB"),
                              {"task": task, "auth_proof": signed, "forwarding_metadata": {}})
    with pytest.raises(ValueError, match="replay"):
        second.forward({"from_node": "nodeA", "encrypted_request": enc})


def test_a_proof_cannot_be_made_to_last():
    receiver = fed.FederationReceiver("nodeB", "shared-secret")
    params, _ = _forward_params(receiver, {"type": "x"}, expires_at=time.time() + 10 * 365 * 86400)
    with pytest.raises(ValueError, match="too far"):
        receiver.forward(params)
    # the nonce of a proof still valid is never forgotten to make room
    v = fed.ProofValidator("nodeB", "sekret")
    v._max_nonces = 2
    v._seen_nonces.update({"a": time.time() + 100, "b": time.time() - 1})

    def signed(nonce):
        proof = {"client_jwt": "C", "forwarded_by": "nodeA", "forwarded_at": time.time(),
                 "task_hash": "abc", "forwarded_to": "nodeB", "nonce": nonce,
                 "expires_at": time.time() + 300}
        return {"proof": proof, "sig_alg": "HS256",
                "signature": fed.hmac_sign_proof("sekret", fed.canonical_proof(proof))}
    assert v.verify(signed("c"), "nodeA") and "a" in v._seen_nonces and "b" not in v._seen_nonces
    with pytest.raises(ValueError, match="too many"):
        v.verify(signed("d"), "nodeA")


def test_federation_needs_a_node_id_and_a_secret_of_its_own():
    import smcp as rixi_smcp
    with pytest.raises(RuntimeError, match="node_id"):
        rixi_smcp.SMCPToolServer({}, secret_key="s" * 32, api_key="k", federation_enabled=True)
    server = rixi_smcp.SMCPToolServer({}, secret_key="s" * 32, api_key="k", node_id="nodeB",
                                      jwt_secret="session-token-key", federation_enabled=True)
    assert server._fed_hmac_secret != "session-token-key"       # peers cannot sign session tokens
