import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import StatementError

from campaign_action import create_app
from campaign_action.__main__ import migrate
from campaign_action.crypto import campaign_cipher, decrypt_value
from campaign_action.models import Campaign, Recipient, db
from conftest import PASSWORD


def test_independent_encrypted_campaign_secrets(app, factory):
    first, second = factory(), factory()
    with app.app_context():
        one = db.session.get(Campaign, first["campaign_id"])
        two = db.session.get(Campaign, second["campaign_id"])
        assert decrypt_value(one.secret) != decrypt_value(two.secret)
        assert one.secret != decrypt_value(one.secret)
        with pytest.raises(InvalidToken):
            campaign_cipher(two).decrypt(first["path"].split("/r/")[1].encode())


def test_salt_changes_only_new_campaigns(app, factory):
    first = factory(mode="visit")
    with app.app_context():
        before = db.session.get(Campaign, first["campaign_id"]).secret
    app.config["SALT"] = "new-optional-salt"
    second = factory(mode="visit")
    assert app.test_client().get(first["path"]).status_code == 200
    assert app.test_client().get(second["path"]).status_code == 200
    with app.app_context():
        assert db.session.get(Campaign, first["campaign_id"]).secret == before


def test_duplicate_generates_new_key(app, client, factory):
    from conftest import csrf
    item = factory()
    path = f'/admin/campaigns/{item["campaign_id"]}'
    client.post(path + "/duplicate", data={"csrf_token": csrf(client.get(path))})
    with app.app_context():
        campaigns = db.session.scalars(db.select(Campaign)).all()
        assert len({decrypt_value(campaign.secret) for campaign in campaigns}) == 2


def test_entropy_failure_does_not_create_campaign(app, monkeypatch):
    def fail(_):
        raise OSError("OS entropy unavailable")
    monkeypatch.setattr("campaign_action.crypto.secrets.token_bytes", fail)
    with app.app_context():
        db.session.add(Campaign(name="Must not exist"))
        with pytest.raises(StatementError):
            db.session.commit()
        db.session.rollback()
        assert db.session.scalar(db.select(db.func.count()).select_from(Campaign)) == 0


def test_wrong_encryption_secret_is_rejected_on_restart(app, factory):
    factory()
    with pytest.raises(ValueError, match="ENCYPTION_SECRET"):
        create_app({"ENCYPTION_SECRET": "wrong-secret-" + "z" * 40, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
                    "PUBLIC_BASE_URL": "http://localhost", "DATABASE_PATH": app.config["DATABASE_PATH"]})


def test_migration_preserves_legacy_links_recipients_and_answers(tmp_path):
    app = create_app({"TESTING": True, "ENCYPTION_SECRET": "x" * 48, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
                      "PUBLIC_BASE_URL": "http://localhost", "DATABASE_PATH": str(tmp_path / "legacy.sqlite3")})
    old_token = Fernet(Fernet.generate_key()).encrypt(json.dumps({"v": 1, "campaign": "legacy-campaign", "recipient": "legacy-recipient", "identity": "legacy@example.com"}).encode()).decode()
    with app.app_context():
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "campaign_action" / "migrations"))
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001")
            connection.execute(sa.text("INSERT INTO campaign (id,uuid,name,identity_type,key_column,confirmation_mode,confirmation_text,form_enabled,show_text_with_form,active,created_at) VALUES (1,'legacy-campaign','Legacy','email','key','visit','Danke',1,0,1,'2026-01-01')"))
            connection.execute(sa.text("INSERT INTO form_field (id,campaign_id,label,field_type,required,description,position) VALUES (1,1,'Name','text',1,'',0)"))
            connection.execute(sa.text("INSERT INTO recipient (id,uuid,campaign_id,identity,email,name,source,attributes,token,created_at) VALUES (1,'legacy-recipient',1,'legacy@example.com','legacy@example.com','Legacy','{}','{}',:token,'2026-01-01')"), {"token": old_token})
            connection.execute(sa.text("INSERT INTO submission (id,recipient_id,answers,submitted_at) VALUES (1,1,:answers,'2026-01-02')"), {"answers": json.dumps({"1": "Preserved answer"})})
    migrate(app)
    migrate(app)
    assert app.test_client().get("/r/" + old_token).status_code == 200
    with app.app_context():
        recipient = db.session.get(Recipient, 1)
        assert recipient.token_version == 1 and recipient.token == old_token
        assert recipient.submission.answers == {"1": "Preserved answer"}
        assert recipient.campaign.fields[0].label == "Name"
        assert recipient.campaign.secret
        assert recipient.campaign.show_campaign_name
        assert recipient.campaign.branding_mode == "global"
        assert recipient.campaign.accent_color == "#2359dc"
        assert recipient.campaign.logo_data is None
        assert db.session.execute(sa.text("PRAGMA foreign_key_check")).fetchall() == []
        db.session.remove()
        db.engine.dispose()
