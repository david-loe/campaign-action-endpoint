import os
import sqlite3
import subprocess
import sys

import pytest
from werkzeug.datastructures import MultiDict

from campaign_action import create_app
from campaign_action.__main__ import migrate
from campaign_action.models import Campaign, FormField, Recipient, Submission, db
from conftest import PASSWORD, csrf, settings_data


def test_admin_authentication_csrf_and_logout(app, client):
    anonymous = app.test_client()
    assert anonymous.get("/admin/campaigns").status_code == 302
    assert anonymous.post("/admin/campaigns/new").status_code == 400
    assert client.get("/admin/campaigns").status_code == 200
    assert client.post("/admin/logout", data={"csrf_token": csrf(client.get("/admin/campaigns"))}).status_code == 302
    assert client.get("/admin/campaigns").status_code == 302


def test_persistent_login_rate_limit(app):
    client = app.test_client()
    token = csrf(client.get("/admin/login"))
    for _ in range(5):
        assert client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "wrong"}).status_code == 401
    blocked = client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": PASSWORD})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "900"


def test_create_campaign_with_form_and_render_admin_pages(app, client):
    path = "/admin/campaigns/new"
    data = MultiDict({"csrf_token": csrf(client.get(path)), "name": "Überprüfung", "identity_type": "key", "key_column": "customer_id",
                      "confirmation_mode": "action", "confirmation_text": "Danke!", "active": "1", "form_enabled": "1",
                      "field_id": "", "field_label": "Datum", "field_type": "date", "field_required": "1", "field_description": "Bitte auswählen"})
    response = client.post(path, data=data)
    assert response.status_code == 302
    detail = response.headers["Location"]
    assert client.get(detail).status_code == 200
    assert client.get(detail + "/settings").status_code == 200
    assert client.get(detail + "/preview").status_code == 200
    assert client.get(detail + "/import").status_code == 200
    assert client.get("/admin/campaigns").status_code == 200
    with app.app_context():
        campaign = db.session.scalar(db.select(Campaign))
        assert campaign.fields[0].required and campaign.fields[0].field_type == "date"


def test_campaign_duplicate_and_deletion(app, client, factory):
    item = factory(mode="visit", form=True)
    app.test_client().get(item["path"])
    path = f'/admin/campaigns/{item["campaign_id"]}'
    response = client.post(path + "/duplicate", data={"csrf_token": csrf(client.get(path))})
    assert response.status_code == 302
    with app.app_context():
        copy = db.session.scalar(db.select(Campaign).where(Campaign.id != item["campaign_id"]))
        assert copy.locked_at is None and copy.uuid != item["campaign_uuid"]
        assert not copy.recipients and len(copy.fields) == 1
    deletion = client.get(path + "/delete")
    assert deletion.status_code == 200
    client.post(path + "/delete", data={"csrf_token": csrf(deletion)})
    assert app.test_client().get(item["path"]).status_code == 404
    with app.app_context():
        assert db.session.get(Recipient, item["recipient_id"]) is None


def test_identity_is_fixed_after_import_and_field_ids_are_validated(app, client, factory):
    first, second = factory(form=True), factory(form=True)
    path = f'/admin/campaigns/{first["campaign_id"]}/settings'
    data = settings_data(app, first["campaign_id"], identity_type="key")
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 400
    data = settings_data(app, first["campaign_id"], field_id=[str(second["field_ids"][0])])
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 400


def test_search_status_filter_and_recipient_isolation(app, client, factory):
    first, second = factory(mode="visit"), factory()
    app.test_client().get(first["path"])
    path = f'/admin/campaigns/{first["campaign_id"]}'
    assert "Keine passenden Empfänger" in client.get(path + "?search=missing").text
    assert "Keine passenden Empfänger" in client.get(path + "?status=pending").text
    assert "person@example.com" in client.get(path + "?status=confirmed").text
    assert client.get(path + f'/recipients/{second["recipient_id"]}').status_code == 404
    assert client.get(path + f'/recipients/{first["recipient_id"]}').status_code == 200


def test_labels_are_html_escaped(app, client, factory):
    item = factory(form=True)
    with app.app_context():
        field = db.session.get(FormField, item["field_ids"][0])
        field.label = '<script>alert("unsafe")</script>'
        db.session.commit()
    page = app.test_client().get(item["path"])
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;" in page.text


def test_migrations_are_idempotent_and_backup_contains_wal(app, factory, tmp_path):
    item = factory(mode="visit")
    app.test_client().get(item["path"])
    migrate(app)
    destination = tmp_path / "backup.sqlite3"
    environment = dict(os.environ, ENCYPTION_SECRET=app.config["SECRET_KEY"],
                       ADMIN_USERNAME="admin", ADMIN_PASSWORD=PASSWORD, PUBLIC_BASE_URL="http://localhost",
                       DATABASE_PATH=app.config["DATABASE_PATH"])
    subprocess.run([sys.executable, "-m", "campaign_action", "backup", str(destination)], check=True, capture_output=True, env=environment)
    with sqlite3.connect(destination) as target:
        assert target.execute("SELECT confirmed_at FROM recipient").fetchone()[0]
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert target.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002"


def test_backup_rejects_uninitialized_database(app, tmp_path):
    destination = tmp_path / "must-not-create.sqlite3"
    environment = dict(os.environ, ENCYPTION_SECRET=app.config["ENCYPTION_SECRET"], ADMIN_USERNAME="admin",
                       ADMIN_PASSWORD=PASSWORD, PUBLIC_BASE_URL="http://localhost", DATABASE_PATH=str(tmp_path / "empty.sqlite3"))
    result = subprocess.run([sys.executable, "-m", "campaign_action", "backup", str(destination)], capture_output=True, env=environment)
    assert result.returncode != 0 and not destination.exists()
    assert b"not an initialized" in result.stderr


@pytest.mark.parametrize("change", [
    {"ENCYPTION_SECRET": ""}, {"ENCYPTION_SECRET": "short"}, {"ADMIN_PASSWORD": "short"},
    {"PUBLIC_BASE_URL": "https://example.com/path"}, {"PUBLIC_BASE_URL": "https://user:pass@example.com"},
    {"ADMIN_USERNAME": ""}, {"PUBLIC_BASE_URL": ""},
])
def test_configuration_validation(tmp_path, change):
    config = {"ENCYPTION_SECRET": "x" * 48, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
              "PUBLIC_BASE_URL": "https://example.com", "DATABASE_PATH": str(tmp_path / "test.sqlite3")}
    config.update(change)
    with pytest.raises(ValueError):
        create_app(config)


def test_secure_session_cookie_when_public_origin_is_https(app):
    # Actual session cookies are secure in HTTPS deployments, HTTP for local use.
    config = {"ENCYPTION_SECRET": app.config["SECRET_KEY"], "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD,
              "PUBLIC_BASE_URL": "https://example.com", "DATABASE_PATH": app.config["DATABASE_PATH"]}
    https_app = create_app(config)
    cookie = https_app.test_client().get("/admin/login", base_url="https://example.com").headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
