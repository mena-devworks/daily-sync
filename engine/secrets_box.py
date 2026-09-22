"""Decrypt app passwords written by the worker: AES-GCM, key = HKDF-SHA256(SETUP_CODE, salt, info)."""
import base64, os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_key = None


def _k():
    global _key
    if _key is None:
        code = os.environ.get("SETUP_CODE", "")
        if not code:
            raise RuntimeError("SETUP_CODE missing")
        _key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"job-hunter/app-password", info=b"v1").derive(code.encode())
    return _key


def decrypt(blob):
    if not blob:
        return None
    v, iv, ct = blob.split(":")
    if v != "v1":
        raise ValueError("unknown format")
    return AESGCM(_k()).decrypt(base64.b64decode(iv), base64.b64decode(ct), None).decode()


def encrypt(text):  # used by tests only
    iv = os.urandom(12)
    return "v1:" + base64.b64encode(iv).decode() + ":" + base64.b64encode(AESGCM(_k()).encrypt(iv, text.encode(), None)).decode()
