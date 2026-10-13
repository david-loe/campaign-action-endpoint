import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from campaign_action import create_app
from campaign_action.__main__ import migrate
from campaign_action.crypto import decrypt_value, encrypt_value
from campaign_action.models import Campaign, FormField, ListmonkConfig, Recipient, db
from campaign_action.services import begin_write, confirmation_link
from conftest import PASSWORD, csrf


def test_random_codes_are_stable_and_independent_of_identity(app, factory):
    first, second = factory(), factory(identity_type="key")
    with app.app_context():
        for item in (first, second):
            recipient = db.session.get(Recipient, item["recipient_id"])
            assert re.fullmatch(r"[A-Za-z0-9_-]{22}", recipient.token)
            assert confirmation_link(recipient) == item["link"]
            assert recipient.confirmed_at is None and recipient.campaign.locked_at is None
        assert first["path"] != second["path"]


def test_collision_retry_keeps_existing_links(app, client, factory, monkeypatch):
    first, second = factory(), factory()
    with app.app_context():
        existing = db.session.get(Recipient, first["recipient_id"]).token
        db.session.get(Recipient, second["recipient_id"]).token = None
        db.session.commit()
    candidates = iter([existing, "c" * 22])
    requested_bytes = []

    def generate(size):
        requested_bytes.append(size)
        return next(candidates)

    path = f'/admin/campaigns/{second["campaign_id"]}/recipients/{second["recipient_id"]}'
    token = csrf(client.get(path))
    monkeypatch.setattr("campaign_action.services.secrets.token_urlsafe", generate)
    assert client.post(path, data={"csrf_token": token}).status_code == 302
    assert requested_bytes == [16, 16]
    with app.app_context():
        assert db.session.get(Recipient, first["recipient_id"]).token == existing
        assert db.session.get(Recipient, second["recipient_id"]).token == "c" * 22


@pytest.mark.parametrize("failure", ["collision", "entropy"])
def test_generation_failure_can_be_rolled_back_without_partial_changes(app, factory, monkeypatch, failure):
    item = factory()
    with app.app_context():
        existing = db.session.get(Recipient, item["recipient_id"]).token
    attempts = []

    def generate(size):
        attempts.append(size)
        if failure == "entropy":
            raise OSError("OS entropy unavailable")
        return existing

    monkeypatch.setattr("campaign_action.services.secrets.token_urlsafe", generate)
    with app.app_context():
        begin_write()
        recipient = Recipient(campaign_id=item["campaign_id"], identity="new@example.com")
        db.session.add(recipient)
        db.session.flush()
        with pytest.raises(RuntimeError if failure == "collision" else OSError):
            confirmation_link(recipient)
        db.session.rollback()
        assert db.session.scalar(db.select(sa.func.count()).select_from(Recipient)) == 1
        assert db.session.get(Recipient, item["recipient_id"]).token == existing
    assert len(attempts) == (5 if failure == "collision" else 1)


def test_concurrent_issuance_returns_one_stable_code(app, factory):
    item = factory()
    with app.app_context():
        db.session.get(Recipient, item["recipient_id"]).token = None
        db.session.commit()
    barrier = Barrier(4)

    def issue(_):
        with app.app_context():
            barrier.wait(timeout=10)
            begin_write()
            recipient = db.session.get(Recipient, item["recipient_id"])
            link = confirmation_link(recipient)
            db.session.commit()
            return link

    with ThreadPoolExecutor(max_workers=4) as pool:
        links = list(pool.map(issue, range(4)))
    assert len(set(links)) == 1
    assert re.fullmatch(r"http://localhost/r/[A-Za-z0-9_-]{22}", links[0])
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None and recipient.campaign.locked_at is None


def test_codes_are_case_sensitive(app, factory):
    item = factory(mode="visit")
    with app.app_context():
        db.session.get(Recipient, item["recipient_id"]).token = "A" * 22
        db.session.commit()
    assert app.test_client().get("/r/" + "a" * 22).status_code == 404
    assert app.test_client().get("/r/" + "A" * 22).status_code == 200


@pytest.mark.parametrize("token", ["a" * 21, "a" * 23, "a" * 22, "!" * 22, "a" * 21 + "=", "😀" * 22,
                                   "gAAAAA" + "a" * 300])
def test_invalid_or_unknown_codes_never_confirm(app, factory, token):
    item = factory(mode="visit")
    assert app.test_client().get("/r/" + token).status_code == 404
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None and recipient.campaign.locked_at is None


@pytest.fixture
def old_database(tmp_path):
    app = create_app({"TESTING": True, "ENCYPTION_SECRET": "x" * 48, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
                      "PUBLIC_BASE_URL": "http://localhost", "DATABASE_PATH": str(tmp_path / "before-short-links.sqlite3")})
    with app.app_context():
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "campaign_action" / "migrations"))
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0003")
            connection.execute(sa.text("INSERT INTO campaign (id,uuid,name,secret,identity_type,key_column,confirmation_mode,confirmation_text,form_enabled,show_text_with_form,active,locked_at,created_at) VALUES (1,'existing-campaign','Existing','obsolete-key','email','key','visit','Danke',1,0,1,'2026-01-02','2026-01-01')"))
            connection.execute(sa.text("INSERT INTO form_field (id,campaign_id,label,field_type,required,description,position) VALUES (1,1,'Name','text',1,'',0)"))
            for index, old_token in enumerate(("gAAAAA" + "a" * 300, "gAAAAA" + "b" * 300, None, ""), 1):
                connection.execute(sa.text("INSERT INTO recipient (id,uuid,campaign_id,identity,email,name,source,attributes,token,token_version,confirmed_at,created_at) VALUES (:id,:uuid,1,:identity,:identity,'Person','{}','{}',:token,:version,:confirmed,'2026-01-01')"),
                                   {"id": index, "uuid": f"recipient-{index}", "identity": f"person{index}@example.com",
                                    "token": old_token, "version": 1 if index == 1 else 2,
                                    "confirmed": "2026-01-02" if index == 1 else None})
            connection.execute(sa.text("INSERT INTO submission (id,recipient_id,answers,submitted_at) VALUES (1,1,:answers,'2026-01-02')"), {"answers": json.dumps({"1": "Preserved answer"})})
            connection.execute(sa.text("INSERT INTO branding_config (id,accent_color,logo_data,logo_mime) VALUES (1,'#123456',:logo,'image/png')"), {"logo": b"preserved-logo"})
            connection.execute(sa.text("INSERT INTO listmonk_config (id,base_url,username,token,enabled,revision) VALUES (1,'https://example.com','api',:token,1,3)"), {"token": encrypt_value("preserved-api-token")})
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def test_migration_replaces_links_preserves_data_and_is_idempotent(old_database):
    app = old_database
    migrate(app)
    with app.app_context():
        recipients = db.session.scalars(db.select(Recipient).order_by(Recipient.id)).all()
        codes = [recipient.token for recipient in recipients]
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", code) for code in codes[:2])
        assert codes[0] != codes[1] and codes[2:] == [None, None]
        assert recipients[0].confirmed_at.isoformat().startswith("2026-01-02")
        assert all(recipient.confirmed_at is None for recipient in recipients[1:])
        assert recipients[0].submission.answers == {"1": "Preserved answer"}
        assert recipients[0].submission.submitted_at.isoformat().startswith("2026-01-02")
        campaign = db.session.get(Campaign, 1)
        assert campaign.locked_at.isoformat().startswith("2026-01-02")
        assert campaign.show_campaign_name and campaign.branding_mode == "global"
        assert db.session.get(FormField, 1).label == "Name"
        assert decrypt_value(db.session.get(ListmonkConfig, 1).token) == "preserved-api-token"
        assert db.session.execute(sa.text("SELECT logo_data FROM branding_config")).scalar() == b"preserved-logo"
        assert "secret" not in {column["name"] for column in sa.inspect(db.engine).get_columns("campaign")}
        columns = {column["name"]: column for column in sa.inspect(db.engine).get_columns("recipient")}
        assert "token_version" not in columns and columns["token"]["type"].length == 22
        assert any(index["unique"] and index["column_names"] == ["token"] for index in sa.inspect(db.engine).get_indexes("recipient"))
        assert db.session.execute(sa.text("PRAGMA foreign_key_check")).fetchall() == []
    migrate(app)
    with app.app_context():
        assert list(db.session.scalars(db.select(Recipient.token).order_by(Recipient.id))) == codes
    assert app.test_client().get("/r/gAAAAA" + "a" * 300).status_code == 404
    assert app.test_client().get("/r/" + codes[0]).status_code == 200


def test_migration_retries_collisions(old_database, monkeypatch):
    candidates = iter(["a" * 22, "a" * 22, "b" * 22])
    monkeypatch.setattr("secrets.token_urlsafe", lambda size: next(candidates))
    migrate(old_database)
    with old_database.app_context():
        assert list(db.session.scalars(db.select(Recipient.token).order_by(Recipient.id))) == ["a" * 22, "b" * 22, None, None]


def test_populated_initial_schema_upgrades_through_frozen_key_migration(tmp_path):
    app = create_app({"TESTING": True, "ENCYPTION_SECRET": "x" * 48, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
                      "PUBLIC_BASE_URL": "http://localhost", "DATABASE_PATH": str(tmp_path / "initial.sqlite3")})
    try:
        with app.app_context():
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parents[1] / "campaign_action" / "migrations"))
            with db.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "0001")
                connection.execute(sa.text("INSERT INTO campaign (id,uuid,name,identity_type,key_column,confirmation_mode,confirmation_text,form_enabled,show_text_with_form,active,created_at) VALUES (1,'initial-campaign','Initial','email','key','action','Danke',0,0,1,'2026-01-01')"))
                connection.execute(sa.text("INSERT INTO recipient (id,uuid,campaign_id,identity,email,name,source,attributes,token,created_at) VALUES (1,'initial-recipient',1,'person@example.com','person@example.com','','{}','{}','gAAAAA-local-old-link','2026-01-01')"))
        migrate(app)
        with app.app_context():
            recipient = db.session.get(Recipient, 1)
            assert re.fullmatch(r"[A-Za-z0-9_-]{22}", recipient.token)
            assert recipient.confirmed_at is None and recipient.campaign.locked_at is None
            assert recipient.identity == "person@example.com"
            assert db.session.execute(sa.text("PRAGMA foreign_key_check")).fetchall() == []
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.mark.parametrize("failure", ["collision", "entropy"])
def test_failed_migration_rolls_back_links_and_schema(old_database, monkeypatch, failure):
    attempts = []

    def generate(size):
        attempts.append(size)
        if len(attempts) > 1 and failure == "entropy":
            raise OSError("OS entropy unavailable")
        return "a" * 22

    with monkeypatch.context() as patch:
        patch.setattr("secrets.token_urlsafe", generate)
        with pytest.raises(RuntimeError if failure == "collision" else OSError):
            migrate(old_database)
    with old_database.app_context():
        assert db.session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "0003"
        tokens = list(db.session.execute(sa.text("SELECT token FROM recipient ORDER BY id")).scalars())
        assert tokens == ["gAAAAA" + "a" * 300, "gAAAAA" + "b" * 300, None, ""]
        assert "secret" in {column["name"] for column in sa.inspect(db.engine).get_columns("campaign")}
        assert "token_version" in {column["name"] for column in sa.inspect(db.engine).get_columns("recipient")}
    assert len(attempts) == (6 if failure == "collision" else 2)
    migrate(old_database)
