"""Encryption for stored Listmonk credentials."""
import base64

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
        raise ValueError("ENCYPTION_SECRET passt nicht zu den gespeicherten Zugangsdaten.") from error
