import pytest

from campaign_action import create_app
from campaign_action.crypto import decrypt_value, encrypt_value
from campaign_action.models import ListmonkConfig, db
from conftest import PASSWORD


def test_credential_encryption(app):
    with app.app_context():
        encrypted = encrypt_value("private-api-token")
        assert encrypted != "private-api-token"
        assert decrypt_value(encrypted) == "private-api-token"
        assert encrypt_value("private-api-token") != encrypted


def test_wrong_encryption_secret_is_rejected_on_restart(app):
    with app.app_context():
        db.session.add(ListmonkConfig(id=1, token=encrypt_value("private-api-token")))
        db.session.commit()
    with pytest.raises(ValueError, match="ENCYPTION_SECRET"):
        create_app({"ENCYPTION_SECRET": "wrong-secret-" + "z" * 40, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
                    "PUBLIC_BASE_URL": "http://localhost", "DATABASE_PATH": app.config["DATABASE_PATH"]})
