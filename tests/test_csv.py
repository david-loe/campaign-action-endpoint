import csv
import io
import json
from datetime import timedelta

import pytest

from campaign_action.models import Campaign, ImportJob, Recipient, db, utcnow
from campaign_action.services import csv_bytes, parse_csv
from conftest import commit_import, csrf, login, upload


def read_csv(response):
    return list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))


@pytest.mark.parametrize("delimiter", [",", ";"])
def test_listmonk_csv_round_trip(app, client, factory, delimiter):
    item = factory()
    attrs = {"city": "Köln", "nested": {"values": [1, "ä"]}, "campaign_action_links": {"previous": "https://example.com/old"}}
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(["uuid", "email", "name", "attributes", "status", "created_at", "updated_at", "extra"])
    writer.writerow(["external-uuid", "ANNA@example.com", 'Anna, "Änne"', json.dumps(attrs), "blocklisted", "2026-01-01", "2026-02-01", "保留"])
    preview = upload(client, item["campaign_id"], output.getvalue().encode("utf-8-sig"), delimiter)
    assert "Importvorschau" in client.get(preview).text
    assert commit_import(client, preview).status_code == 302
    path = f'/admin/campaigns/{item["campaign_id"]}'
    token = csrf(client.get(path))
    response = client.post(path + "/export/links", data={"csrf_token": token, "format": "listmonk"})
    rows = read_csv(response)
    anna = next(row for row in rows if row["email"] == "anna@example.com")
    assert anna["uuid"] == "external-uuid" and anna["name"] == 'Anna, "Änne"'
    assert anna["status"] == "blocklisted" and anna["created_at"] == "2026-01-01"
    exported_attrs = json.loads(anna["attributes"])
    assert exported_attrs["city"] == "Köln" and exported_attrs["nested"] == attrs["nested"]
    assert exported_attrs["campaign_action_links"]["previous"] == "https://example.com/old"
    link = exported_attrs["campaign_action_links"][item["campaign_uuid"]]
    assert len(link.rsplit("/r/", 1)[1]) == 22
    repeated = read_csv(client.post(path + "/export/links", data={"csrf_token": token, "format": "listmonk"}))
    assert rows == repeated
    results = read_csv(client.get(path + "/export/results"))
    assert next(row for row in results if row["email"] == "anna@example.com")["source_extra"] == "保留"
    with app.app_context():
        recipient = db.session.scalar(db.select(Recipient).where(Recipient.email == "anna@example.com"))
        assert recipient.confirmed_at is None
        assert recipient.campaign.locked_at is None
    assert link.startswith("http://localhost/r/")


def test_duplicates_preserve_existing_confirmation(app, client, factory):
    item = factory(mode="visit")
    app.test_client().get(item["path"])
    preview = upload(client, item["campaign_id"], b"email,name\nPERSON@example.com,Changed\nnew@example.com,New\nNEW@example.com,Duplicate\n")
    assert "2 Dubletten" in client.get(preview).text
    commit_import(client, preview)
    with app.app_context():
        recipients = db.session.scalars(db.select(Recipient).order_by(Recipient.id)).all()
        assert len(recipients) == 2
        assert recipients[0].name == "Person" and recipients[0].confirmed_at is not None
        assert recipients[1].name == "New"


@pytest.mark.parametrize("content", [
    b"email\nnew@example.com\ninvalid\n",
    b'email,attributes\nnew@example.com,"[1,2]"\n',
    b'email,attributes\nnew@example.com,"{bad}"\n',
    b'email,attributes\nnew@example.com,"{""campaign_action_links"":""wrong""}"\n',
    b"email,email\nnew@example.com,new@example.com\n",
    b"email,name\nnew@example.com\n",
    b"wrong\nnew@example.com\n",
    b"email\n\xff\n",
    b"email\n",
    b'email,name\nnew@example.com,"unterminated\n',
])
def test_invalid_import_is_atomic(app, client, factory, content):
    item = factory()
    preview = upload(client, item["campaign_id"], content)
    assert "Bitte korrigieren" in client.get(preview).text
    assert commit_import(client, preview).status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1


def test_custom_key_column_and_missing_email_export(app, client, factory):
    item = factory(identity_type="key")
    preview = upload(client, item["campaign_id"], b"customer_id,name\nA-99,Key only\n", identity_column="customer_id")
    commit_import(client, preview)
    path = f'/admin/campaigns/{item["campaign_id"]}'
    token = csrf(client.get(path))
    rows = read_csv(client.post(path + "/export/links", data={"csrf_token": token, "format": "simple"}))
    assert set(rows[0]) == {"key", "link"}
    assert {row["key"] for row in rows} == {"customer-01", "A-99"}
    assert client.post(path + "/export/links", data={"csrf_token": token, "format": "listmonk"}).status_code == 302
    assert "gültige E-Mail-Adresse" in client.get(path).text


def test_result_export_includes_pending_and_form_answers(app, client, factory):
    item = factory(form=True, fields=[("Kommentar", "text", True), ("Kommentar", "text", False)])
    preview = upload(client, item["campaign_id"], b"email\npending@example.com\n")
    commit_import(client, preview)
    visitor = app.test_client()
    visitor.post(item["path"], data={"csrf_token": csrf(visitor.get(item["path"])), f'field_{item["field_ids"][0]}': "Hallo, Welt\nWeitere Zeile"})
    rows = read_csv(client.get(f'/admin/campaigns/{item["campaign_id"]}/export/results'))
    assert len(rows) == 2
    confirmed = next(row for row in rows if row["email"] == "person@example.com")
    assert confirmed["confirmed_at"].endswith("Z") and confirmed["submitted_at"].endswith("Z")
    assert confirmed[f'field_{item["field_ids"][0]}_Kommentar'] == "Hallo, Welt\nWeitere Zeile"
    assert confirmed[f'field_{item["field_ids"][1]}_Kommentar'] == ""
    pending = next(row for row in rows if row["email"] == "pending@example.com")
    assert pending["confirmed_at"] == pending["submitted_at"] == ""


def test_import_jobs_are_owned_expire_and_cannot_be_replayed(app, client, factory):
    item = factory()
    preview = upload(client, item["campaign_id"], b"email\nnew@example.com\n")
    other = app.test_client()
    login(other)
    assert other.get(preview).status_code == 404
    with app.app_context():
        job = db.session.get(ImportJob, preview.rsplit("/", 1)[1])
        job.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert client.get(preview).status_code == 404
    preview = upload(client, item["campaign_id"], b"email\nnew@example.com\n")
    commit_import(client, preview)
    assert client.get(preview).status_code == 404


def test_import_rechecks_duplicates_at_commit(app, client, factory):
    item = factory()
    first = upload(client, item["campaign_id"], b"email\nnew@example.com\n")
    second = upload(client, item["campaign_id"], b"email\nnew@example.com\n")
    commit_import(client, first)
    commit_import(client, second)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 2


def test_spreadsheet_reports_escape_formulas_machine_csv_preserves_keys():
    assert b"'=SUM" in csv_bytes(["key"], [["=SUM(1,2)"]], spreadsheet_safe=True)
    rows = list(csv.DictReader(io.StringIO(csv_bytes(["key"], [["=SUM(1,2)"]]).decode("utf-8-sig"))))
    assert rows[0]["key"] == "=SUM(1,2)"


def test_parse_csv_rejects_unknown_delimiters():
    rows, errors = parse_csv(b"email\nnew@example.com\n", "|", "email", "email")
    assert not rows and errors


def test_long_unicode_keys_produce_working_links(app, client, factory):
    item = factory(mode="visit", identity_type="key")
    identity = "😀" * 255
    preview = upload(client, item["campaign_id"], ("key\n" + identity + "\n").encode())
    commit_import(client, preview)
    path = f'/admin/campaigns/{item["campaign_id"]}'
    rows = read_csv(client.post(path + "/export/links", data={"csrf_token": csrf(client.get(path)), "format": "simple"}))
    link = next(row["link"] for row in rows if row["key"] == identity)
    assert len(link.rsplit("/r/", 1)[1]) == 22
    assert app.test_client().get(link.removeprefix("http://localhost")).status_code == 200
    with app.app_context():
        recipient = db.session.scalar(db.select(Recipient).where(Recipient.identity == identity))
        assert recipient.confirmed_at is not None


def test_listmonk_rejects_ambiguous_duplicate_emails(client, factory):
    item = factory(identity_type="key")
    preview = upload(client, item["campaign_id"], b"key,email\nother-key,person@example.com\n")
    commit_import(client, preview)
    path = f'/admin/campaigns/{item["campaign_id"]}'
    response = client.post(path + "/export/links", data={"csrf_token": csrf(client.get(path)), "format": "listmonk"}, follow_redirects=True)
    assert "E-Mail-Adressen innerhalb der Campaign eindeutig" in response.text
