# aesgcm.py

import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12

def aes_encrypt(key, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, data, None)

def aes_decrypt(key, blob):
    return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], None)
