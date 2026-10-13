"""OS-backed key generation and separate keys for storage and public links."""
import base64
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app


def storage_cipher():
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"campaign-action:storage:v1").derive(current_app.config["ENCYPTION_SECRET"].encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(value):
    return storage_cipher().encrypt(value.encode()).decode()


def decrypt_value(value):
    try:
        return storage_cipher().decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise ValueError("ENCYPTION_SECRET passt nicht zu den gespeicherten Schlüsseln.") from error


def new_campaign_secret():
    # No seed, PRNG from random, UUID, clock or identity is used as key material.
    entropy = secrets.token_bytes(32)
    salt = current_app.config.get("SALT", "").encode() or None
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
               info=b"campaign-action:campaign:v2").derive(entropy)
    return encrypt_value(base64.urlsafe_b64encode(key).decode())


def campaign_cipher(campaign):
    return Fernet(decrypt_value(campaign.secret).encode())
