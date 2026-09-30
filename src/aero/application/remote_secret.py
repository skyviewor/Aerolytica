"""Per-request envelope encryption; private keys and plaintext never leave this process."""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def associated_data(agent_id: str, command_id: str, request_id: str) -> bytes:
    return json.dumps([agent_id, command_id, request_id], separators=(",", ":")).encode()


class SecretKey:
    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def public_key(self) -> str:
        return base64.b64encode(
            self._key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode()

    def decrypt(self, envelope: dict, aad: bytes) -> str:
        key = self._key.decrypt(
            base64.b64decode(envelope["key"], validate=True),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        return (
            AESGCM(key)
            .decrypt(
                base64.b64decode(envelope["iv"], validate=True),
                base64.b64decode(envelope["ciphertext"], validate=True),
                aad,
            )
            .decode("utf-8")
        )
