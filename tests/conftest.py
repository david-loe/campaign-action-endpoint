import io
import re

import pytest

from campaign_action import create_app
from campaign_action.__main__ import migrate
from campaign_action.models import Campaign, FormField, Recipient, db
from campaign_action.services import confirmation_link

PASSWORD = "test-admin-password-123"


def csrf(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data.decode()[:500]
    return match.group(1).decode()


def login(client):
    return client.post("/admin/login", data={"username": "admin", "password": PASSWORD,
                       "csrf_token": csrf(client.get("/admin/login"))})


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "ENCYPTION_SECRET": "test-session-secret-" + "x" * 40,
                      "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": PASSWORD, "PUBLIC_BASE_URL": "http://localhost",
                      "DATABASE_PATH": str(tmp_path / "app.sqlite3")})
    migrate(app)
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    client = app.test_client()
    assert login(client).status_code == 302
    return client


@pytest.fixture
def factory(app):
    def make(mode="action", form=False, identity_type="email", fields=None, show_text=False, active=True):
        with app.app_context():
            campaign = Campaign(name="Test Campaign", confirmation_mode=mode, form_enabled=form,
                                confirmation_text="Eigener Bestätigungstext", show_text_with_form=show_text,
                                identity_type=identity_type, active=active)
            if form:
                campaign.fields = [FormField(label=label, field_type=kind, required=required, description="Beschreibung", position=index)
                                   for index, (label, kind, required) in enumerate(fields or [("Name", "text", True)])]
            recipient = Recipient(campaign=campaign, identity="person@example.com" if identity_type == "email" else "customer-01",
                                  email="person@example.com", name="Person")
            db.session.add(campaign)
            db.session.commit()
            link = confirmation_link(recipient)
            db.session.commit()
            return {"campaign_id": campaign.id, "recipient_id": recipient.id, "campaign_uuid": campaign.uuid,
                    "recipient_uuid": recipient.uuid, "field_ids": [f.id for f in campaign.fields], "path": "/r/" + recipient.token, "link": link}
    return make


def settings_data(app, campaign_id, **changes):
    with app.app_context():
        campaign = db.session.get(Campaign, campaign_id)
        data = {"name": campaign.name, "identity_type": campaign.identity_type, "key_column": campaign.key_column,
                "confirmation_mode": campaign.confirmation_mode, "confirmation_text": campaign.confirmation_text,
                "field_id": [str(f.id) for f in campaign.fields], "field_label": [f.label for f in campaign.fields],
                "field_type": [f.field_type for f in campaign.fields], "field_required": ["1" if f.required else "0" for f in campaign.fields],
                "field_description": [f.description for f in campaign.fields]}
        for key in ("active", "form_enabled", "show_text_with_form"):
            if getattr(campaign, key):
                data[key] = "1"
    data.update(changes)
    return data


def upload(client, campaign_id, content, delimiter=",", identity_column="key"):
    path = f"/admin/campaigns/{campaign_id}/import"
    response = client.post(path, data={"csrf_token": csrf(client.get(path)), "file": (io.BytesIO(content), "subscribers.csv"),
                                      "delimiter": delimiter, "identity_column": identity_column})
    assert response.status_code == 302
    return response.headers["Location"]


def commit_import(client, preview_path):
    return client.post(preview_path, data={"csrf_token": csrf(client.get(preview_path))})
