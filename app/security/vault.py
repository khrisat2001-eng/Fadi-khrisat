"""Envelope encryption for exchange credentials.

Each credential set gets its own random 256-bit data key (DEK). The secrets are
encrypted with AES-256-GCM under the DEK, and the DEK is encrypted ("wrapped")
by a key-encryption key (KEK) held by a KeyProvider. Associated data binds every
ciphertext to its connection, so a row copied onto another connection fails to
decrypt. Deleting the wrapped DEK makes the credentials unrecoverable.

LocalKeyProvider reads the KEK from the environment and is meant for
development and single-server deployments. A cloud KMS provider can implement
the same two methods without touching anything else.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.exchanges.base import Credentials


class KeyProvider(Protocol):
    key_id: str

    def wrap(self, dek: bytes, aad: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes: ...


class LocalKeyProvider:
    def __init__(self, kek: bytes, key_id: str = "local-v1"):
        if len(kek) != 32:
            raise ValueError("Master key must be 32 bytes (base64-encoded in CREDENTIAL_MASTER_KEY)")
        self._aes = AESGCM(kek)
        self.key_id = key_id

    @classmethod
    def from_env(cls) -> "LocalKeyProvider":
        raw = os.environ.get("CREDENTIAL_MASTER_KEY")
        if not raw:
            raise RuntimeError(
                "CREDENTIAL_MASTER_KEY is not set. Generate one with: "
                "python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\""
            )
        try:
            kek = base64.b64decode(raw.strip(), validate=True)
        except ValueError:
            kek = b""
        if len(kek) != 32:
            raise ValueError(
                "CREDENTIAL_MASTER_KEY is not a valid key. It must be 32 random bytes in base64 "
                "(44 characters ending in '='). Generate one with: openssl rand -base64 32"
            )
        return cls(kek)

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        return self._aes.decrypt(wrapped[:12], wrapped[12:], aad)


@dataclass
class EncryptedCredentials:
    key_id: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes
    key_hint: str  # last 4 characters of the API key, for display only


def _aad(connection_id: str, exchange_id: str) -> bytes:
    return f"exchange-credentials|{exchange_id}|{connection_id}".encode()


class CredentialVault:
    def __init__(self, provider: KeyProvider):
        self._provider = provider

    def encrypt(self, connection_id: str, exchange_id: str, creds: Credentials) -> EncryptedCredentials:
        aad = _aad(connection_id, exchange_id)
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        plaintext = json.dumps(
            {"api_key": creds.api_key, "api_secret": creds.api_secret, "passphrase": creds.passphrase}
        ).encode()
        ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return EncryptedCredentials(
            key_id=self._provider.key_id,
            wrapped_dek=self._provider.wrap(dek, aad),
            nonce=nonce,
            ciphertext=ct,
            key_hint=creds.api_key[-4:],
        )

    def decrypt(self, connection_id: str, exchange_id: str, enc: EncryptedCredentials) -> Credentials:
        aad = _aad(connection_id, exchange_id)
        dek = self._provider.unwrap(enc.wrapped_dek, aad)
        data = json.loads(AESGCM(dek).decrypt(enc.nonce, enc.ciphertext, aad))
        return Credentials(**data)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)
