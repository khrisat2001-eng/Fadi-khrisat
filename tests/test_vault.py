import os

import pytest
from cryptography.exceptions import InvalidTag

from app.exchanges.base import Credentials
from app.logging_utils import redact
from app.security.vault import CredentialVault, LocalKeyProvider

CREDS = Credentials("key-abcd1234", "s3cret-value", "pass-phrase")


def vault():
    return CredentialVault(LocalKeyProvider(os.urandom(32)))


def test_roundtrip_and_hint():
    v = vault()
    enc = v.encrypt("c1", "okx", CREDS)
    assert enc.key_hint == "1234"
    assert b"s3cret" not in enc.ciphertext
    assert v.decrypt("c1", "okx", enc) == CREDS


def test_ciphertext_bound_to_connection():
    v = vault()
    enc = v.encrypt("c1", "okx", CREDS)
    with pytest.raises(InvalidTag):
        v.decrypt("c2", "okx", enc)
    with pytest.raises(InvalidTag):
        v.decrypt("c1", "kucoin", enc)


def test_wrong_master_key_cannot_decrypt():
    enc = vault().encrypt("c1", "okx", CREDS)
    with pytest.raises(InvalidTag):
        vault().decrypt("c1", "okx", enc)


def test_master_key_must_be_32_bytes():
    with pytest.raises(ValueError):
        LocalKeyProvider(b"short")


@pytest.mark.parametrize("line", [
    'payload {"api_secret": "s3cret-value", "x": 1}',
    "OK-ACCESS-PASSPHRASE: s3cret-value",
    "passphrase=s3cret-value&next=1",
    "KC-API-SIGN='s3cret-value'",
])
def test_redaction(line):
    assert "s3cret-value" not in redact(line)


def test_bad_master_key_explains_itself(monkeypatch):
    import base64
    good = base64.b64encode(os.urandom(32)).decode()
    for bad in ["my-secret-password", good.rstrip("="), base64.b64encode(os.urandom(16)).decode()]:
        monkeypatch.setenv("CREDENTIAL_MASTER_KEY", bad)
        with pytest.raises(ValueError, match="44 characters"):
            LocalKeyProvider.from_env()
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", f" {good}\n")
    assert LocalKeyProvider.from_env().key_id == "local-v1"
