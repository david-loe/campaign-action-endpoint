import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from campaign_action.models import Campaign, Recipient, Submission, db
from campaign_action.crypto import campaign_cipher
from conftest import csrf, settings_data


@pytest.mark.parametrize("mode", ["visit", "action"])
@pytest.mark.parametrize("form", [False, True])
def test_confirmation_modes(app, factory, mode, form):
    item = factory(mode=mode, form=form)
    client = app.test_client()
    page = client.get(item["path"])
    assert page.status_code == 200
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert (recipient.confirmed_at is not None) == (mode == "visit")
        assert recipient.submission is None
        assert (recipient.campaign.locked_at is not None) == (mode == "visit")
    data = {"csrf_token": csrf(page)} if mode == "action" or form else None
    if data is not None:
        if form:
            data[f'field_{item["field_ids"][0]}'] = "Ada"
        result = client.post(item["path"], data=data, follow_redirects=True)
        assert result.status_code == 200
        assert "Eigener Bestätigungstext" in result.text
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is not None
        assert (recipient.submission is not None) == form


def test_head_and_admin_preview_do_not_confirm(app, client, factory):
    item = factory(mode="visit", form=True)
    assert app.test_client().head(item["path"]).status_code == 200
    assert client.get(f'/admin/campaigns/{item["campaign_id"]}/preview').status_code == 200
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None
        assert recipient.campaign.locked_at is None


@pytest.mark.parametrize("show_text", [False, True])
def test_text_display_setting(app, factory, show_text):
    item = factory(form=True, show_text=show_text)
    page = app.test_client().get(item["path"])
    assert ("Eigener Bestätigungstext" in page.text) == show_text


@pytest.mark.parametrize("kind,bad,good", [
    ("text", "   ", "Valid name"), ("email", "invalid", "Ada@Example.com"),
    ("number", "NaN", "12.5"), ("number", "Infinity", "-2"), ("number", "1_000", "1e3"),
    ("date", "2026-02-30", "2026-10-13"), ("date", "20261013", "2026-10-13"),
])
def test_server_side_field_validation(app, factory, kind, bad, good):
    item = factory(form=True, fields=[("Pflichtfeld", kind, True), ("Optional", "text", False)])
    client = app.test_client()
    token = csrf(client.get(item["path"]))
    key = f'field_{item["field_ids"][0]}'
    invalid = client.post(item["path"], data={"csrf_token": token, key: bad})
    assert invalid.status_code == 422
    assert 'aria-invalid="true"' in invalid.text
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None and recipient.submission is None
        assert recipient.campaign.locked_at is None
    assert client.post(item["path"], data={"csrf_token": token, key: good}).status_code == 303
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.submission.answers[str(item["field_ids"][1])] == ""


def test_direct_confirmation_survives_invalid_form(app, factory):
    item = factory(mode="visit", form=True)
    client = app.test_client()
    token = csrf(client.get(item["path"]))
    assert client.post(item["path"], data={"csrf_token": token}).status_code == 422
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is not None and recipient.submission is None


def test_submission_is_final_and_confirmation_date_is_stable(app, factory):
    item = factory(form=True)
    client = app.test_client()
    token = csrf(client.get(item["path"]))
    key = f'field_{item["field_ids"][0]}'
    client.post(item["path"], data={"csrf_token": token, key: "First"})
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        first = recipient.confirmed_at
    assert client.post(item["path"], data={"csrf_token": token, key: "Changed"}).status_code == 303
    assert "Abgabe ist abgeschlossen" in client.get(item["path"]).text
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at == first
        assert recipient.submission.answers[str(item["field_ids"][0])] == "First"
        assert db.session.scalar(db.select(db.func.count()).select_from(Submission)) == 1


def test_concurrent_first_submissions(app, factory):
    item = factory(form=True)
    clients = [app.test_client() for _ in range(4)]
    tokens = [csrf(client.get(item["path"])) for client in clients]
    barrier = Barrier(4)
    def submit(index):
        barrier.wait(timeout=10)
        return clients[index].post(item["path"], data={"csrf_token": tokens[index], f'field_{item["field_ids"][0]}': str(index)}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(submit, range(4))) == [303] * 4
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Submission)) == 1
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is not None and recipient.campaign.locked_at is not None


def test_concurrent_direct_gets(app, factory):
    item = factory(mode="visit")
    barrier = Barrier(4)
    def visit(_):
        barrier.wait(timeout=10)
        return app.test_client().get(item["path"]).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(visit, range(4))) == [200] * 4
    with app.app_context():
        first = db.session.get(Recipient, item["recipient_id"]).confirmed_at
    app.test_client().get(item["path"])
    with app.app_context():
        assert db.session.get(Recipient, item["recipient_id"]).confirmed_at == first


def test_invalid_tokens_and_campaign_isolation(app, factory):
    first, second = factory(mode="visit"), factory(mode="visit")
    token = first["path"].split("/r/")[1]
    altered = token[:40] + ("A" if token[40] != "A" else "B") + token[41:]
    assert app.test_client().get("/r/" + altered).status_code == 404
    assert app.test_client().get("/r/not-a-token").status_code == 404
    with app.app_context():
        fernet = campaign_cipher(db.session.get(Campaign, first["campaign_id"]))
        payload = json.loads(fernet.decrypt(token.encode()))
        assert payload["identity"] == "person@example.com"
        assert "person@example.com" not in token
        payload["campaign"] = second["campaign_uuid"]
        mismatch = fernet.encrypt(json.dumps(payload).encode()).decode()
    assert app.test_client().get("/r/" + mismatch).status_code == 404
    app.test_client().get(first["path"])
    with app.app_context():
        assert db.session.get(Recipient, second["recipient_id"]).confirmed_at is None


def test_deactivated_and_deleted_links(app, factory):
    item = factory(mode="visit", active=False)
    assert app.test_client().get(item["path"]).status_code == 404
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.active = True
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None
        db.session.delete(recipient)
        db.session.commit()
    assert app.test_client().get(item["path"]).status_code == 404


def test_public_csrf_and_security_headers(app, factory):
    item = factory()
    client = app.test_client()
    assert client.post(item["path"]).status_code == 400
    response = client.get(item["path"])
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_structure_changes_until_first_confirmation(app, client, factory):
    item = factory(form=True)
    path = f'/admin/campaigns/{item["campaign_id"]}/settings'
    data = settings_data(app, item["campaign_id"], field_type=["email"])
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 302
    visitor = app.test_client()
    visitor.post(item["path"], data={"csrf_token": csrf(visitor.get(item["path"])), f'field_{item["field_ids"][0]}': "person@example.com"})
    data = settings_data(app, item["campaign_id"], field_type=["number"])
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 400
    data = settings_data(app, item["campaign_id"], confirmation_text="Updated", field_label=["Updated label"], field_description=["Updated description"])
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 302
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        assert campaign.fields[0].field_type == "email"
        assert campaign.fields[0].label == "Updated label"
        assert campaign.confirmation_text == "Updated"


def test_direct_confirmation_locks_structure_even_without_form_submission(app, client, factory):
    item = factory(mode="visit", form=True)
    app.test_client().get(item["path"])
    path = f'/admin/campaigns/{item["campaign_id"]}/settings'
    data = settings_data(app, item["campaign_id"], confirmation_mode="action")
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 400
