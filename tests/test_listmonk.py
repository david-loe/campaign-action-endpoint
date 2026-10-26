import copy
import re
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from campaign_action.crypto import decrypt_value
from campaign_action.listmonk import ListmonkClient
from campaign_action.models import Campaign, ListmonkConfig, Recipient, TransferItem, TransferJob, db, utcnow
from campaign_action.transfers import counts
from scripts.listmonk_test_server import ListmonkTestServer
from conftest import csrf, login


@pytest.fixture
def api():
    with ListmonkTestServer() as api:
        yield api


def configure(client, api, **changes):
    data = {"base_url": api.url, "username": api.username, "token": api.token, "enabled": "1"}
    data.update(changes)
    data["csrf_token"] = csrf(client.get("/admin/settings"))
    return client.post("/admin/settings", data=data)


def launch(client, item, direction="import", **params):
    path = f'/admin/campaigns/{item["campaign_id"]}/listmonk/{direction}'
    response = client.post(path, data={"csrf_token": csrf(client.get(path)), "list_id": "1", **params})
    assert response.status_code == 302, response.text
    return response.headers["Location"].split("?")[0]


def step(client, path):
    response = client.post(path + "/step", data={"csrf_token": csrf(client.get(path))}, headers={"Accept": "application/json"})
    assert response.status_code == 200, response.text
    return response.json


def action(client, path, action):
    return client.post(path + "/" + action, data={"csrf_token": csrf(client.get(path))})


def export_campaign(app, factory, count=2):
    item = factory(mode="visit")
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        recipient.identity = recipient.email = "anna@example.com"
        recipient.token = None
        if count >= 2:
            db.session.add(Recipient(campaign_id=item["campaign_id"], identity="ben@example.com", email="ben@example.com", name="Ben"))
        db.session.add(Recipient(campaign_id=item["campaign_id"], identity="missing@example.com", email="missing@example.com", name="Missing"))
        db.session.commit()
    return item


def test_settings_encryption_test_and_token_retention(app, client, api):
    assert configure(client, api).status_code == 302
    page = client.get("/admin/settings")
    assert api.token not in page.text
    with app.app_context():
        config = db.session.get(ListmonkConfig, 1)
        assert config.token != api.token and decrypt_value(config.token) == api.token
    assert configure(client, api, token="").status_code == 302
    tested = client.post("/admin/settings/listmonk/test", data={"csrf_token": csrf(client.get("/admin/settings"))}, follow_redirects=True)
    assert "2 aktive Listen" in tested.text
    assert configure(client, api, base_url="http://localhost:9999", token="").status_code == 400
    assert configure(client, api, base_url="https://user:pass@example.com").status_code == 400
    assert configure(client, api, base_url="https://example.com:bad").status_code == 400


@pytest.mark.parametrize("total", [0, 2, 103])
def test_list_discovery_uses_paginated_response_instead_of_minimal_array(api, total):
    api.lists = {index: {"id": index, "name": f"List {index}", "status": "active"} for index in range(1, total + 1)}
    api.lists[total + 1] = {"id": total + 1, "name": "Archived", "status": "archived"}
    with ListmonkClient(api.url, api.username, api.token) as connection:
        # Mirror the response shape observed from the running Listmonk instance.
        minimal = connection.request("GET", "api/lists", params={"status": "active", "minimal": "true"})
        assert isinstance(minimal, list) and len(minimal) == total
        rows = connection.lists()
    assert [row["id"] for row in rows] == list(range(1, total + 1))
    requests = [parse_qs(urlsplit(path).query) for method, path, _ in api.requests if method == "GET" and "minimal" not in parse_qs(urlsplit(path).query)]
    expected_pages = [["1"], ["2"]] if total > 100 else [["1"]]
    assert [query["page"] for query in requests] == expected_pages


def test_listmonk_options_and_authentication(client, app, factory, api):
    item = factory()
    path = f'/admin/campaigns/{item["campaign_id"]}'
    assert "Aus Listmonk importieren" not in client.get(path).text
    assert app.test_client().get("/admin/settings").status_code == 302
    configure(client, api)
    assert "Aus Listmonk importieren" in client.get(path).text
    selected = client.get(path + "/listmonk/import")
    assert "Newsletter" in selected.text and "Andere Liste" in selected.text and ">Archiv" not in selected.text


def test_import_preview_and_atomic_commit(app, client, factory, api):
    configure(client, api)
    item = factory()
    path = launch(client, item)
    assert step(client, path)["status"] == "preview"
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1
    assert "2 Empfänger importieren" in client.get(path).text
    assert action(client, path, "commit").status_code == 302
    with app.app_context():
        recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == item["campaign_id"])).all()
        assert {r.email for r in recipients} == {"person@example.com", "anna@example.com", "ben@example.com"}
        anna = next(r for r in recipients if r.email == "anna@example.com")
        assert anna.attributes["city"] == "Köln" and anna.source["status"] == "blocklisted"
        assert anna.confirmed_at is None
    assert not any(method == "PATCH" for method, _, _ in api.requests)


@pytest.mark.parametrize("total", [0, 100, 101, 1719])
def test_import_loads_every_subscriber_with_identical_timestamps(app, client, factory, api, total):
    configure(client, api)
    api.subscribers.clear()
    api.unstable_subscriber_ties = True
    for index in reversed(range(1, total + 1)):
        api.add_subscriber(index, f"person{index:04d}@example.com", "Person", [1])
    item = factory()
    path = launch(client, item)
    pages = max(1, (total + 99) // 100)
    for page in range(1, pages + 1):
        result = step(client, path)
        assert result["status"] == ("preview" if page == pages else "loading")
        assert result["total"] == total
    with app.app_context():
        ids = list(db.session.scalars(db.select(TransferItem.subscriber_id).where(TransferItem.job_id == path.rsplit("/", 1)[1])))
        assert len(ids) == total and set(ids) == set(api.subscribers)
    preview = client.get(path).text
    assert f"{total} von {total} Subscribern geladen." in preview
    assert not re.search(r'<button[^>]*disabled[^>]*>\d+ Empfänger importieren</button>', preview)
    assert action(client, path, "commit").status_code == 302
    with app.app_context():
        recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == item["campaign_id"])).all()
        assert len(recipients) == total + 1
        assert {int(r.source["listmonk_id"]) for r in recipients if "listmonk_id" in r.source} == set(api.subscribers)


def test_timestamp_sort_can_repeat_subscribers_across_pages(api):
    api.unstable_subscriber_ties = True
    for index in range(100, 199):
        api.add_subscriber(index, f"person{index}@example.com", "Person", [1])
    with ListmonkClient(api.url, api.username, api.token) as connection:
        pages = [connection.page("api/subscribers", list_id=1, page=page, per_page=100,
                                 order_by="created_at", order="ASC")[0] for page in (1, 2)]
    assert sum(map(len, pages)) == 101
    assert len({row["id"] for page in pages for row in page}) == 100


@pytest.mark.parametrize("direction", ["import", "export"])
@pytest.mark.parametrize("broken_page", ["overlap", "empty"])
def test_incomplete_pagination_stops_before_preview(app, client, factory, api, direction, broken_page):
    configure(client, api)
    for index in range(100, 199):
        api.add_subscriber(index, f"person{index}@example.com", "Person", [1])
    api.subscriber_pages[(1, 2)] = [api.subscribers[10]] if broken_page == "overlap" else []
    item = factory()
    path = launch(client, item, direction)
    assert step(client, path)["status"] == "loading"
    result = step(client, path)
    assert result["status"] == "stopped"
    assert "Unvollständig geladen: 100 von 101 Subscribern" in result["message"]
    assert sum(result["counts"].values()) == 100
    assert "Importvorschau" not in client.get(path).text
    assert "Exportvorschau" not in client.get(path).text
    assert action(client, path, "retry").status_code == 400
    assert action(client, path, "commit").status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1
        assert db.session.get(ListmonkConfig, 1).lease_owner is None
    assert not any(method == "PATCH" for method, _, _ in api.requests)


@pytest.mark.parametrize("direction", ["import", "export"])
@pytest.mark.parametrize("change", ["add", "remove"])
def test_changed_subscriber_total_requires_new_transfer(app, client, factory, api, direction, change):
    configure(client, api)
    for index in range(100, 199):
        api.add_subscriber(index, f"person{index}@example.com", "Person", [1])
    path = launch(client, factory(), direction)
    assert step(client, path)["status"] == "loading"
    if change == "add":
        api.add_subscriber(999, "added@example.com", "Added", [1])
        expected = 102
    else:
        del api.subscribers[198]
        expected = 100
    result = step(client, path)
    assert result["status"] == "stopped"
    assert f"Subscriber-Anzahl hat sich während des Ladens geändert (101 auf {expected})" in result["message"]
    assert result["total"] == 101
    assert result["counts"] == {"staged": 100}
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1
    assert not any(method == "PATCH" for method, _, _ in api.requests)


def test_incomplete_preview_disables_import_and_rejects_direct_commit(app, client, factory, api):
    configure(client, api)
    path = launch(client, factory())
    assert step(client, path)["status"] == "preview"
    with app.app_context():
        job = db.session.get(TransferJob, path.rsplit("/", 1)[1])
        job.total = 3
        db.session.commit()
    preview = client.get(path).text
    assert "2 von 3 Subscribern geladen." in preview
    assert "Unvollständig geladen: 2 von 3 Subscribern" in preview
    assert re.search(r'<button[^>]*disabled[^>]*>2 Empfänger importieren</button>', preview)
    assert action(client, path, "commit").status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1
        assert db.session.get(TransferJob, path.rsplit("/", 1)[1]).status == "preview"


def test_existing_campaign_recipient_does_not_make_load_incomplete(app, client, factory, api):
    configure(client, api)
    item = factory()
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        recipient.identity = recipient.email = "anna@example.com"
        db.session.commit()
    path = launch(client, item)
    assert step(client, path)["status"] == "preview"
    preview = client.get(path).text
    assert "2 von 2 Subscribern geladen." in preview
    assert "1 Dubletten" in preview and "1 Empfänger importieren" in preview
    assert action(client, path, "commit").status_code == 302
    with app.app_context():
        recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == item["campaign_id"])).all()
        assert {recipient.email for recipient in recipients} == {"anna@example.com", "ben@example.com"}


def test_export_checks_full_list_before_matching_campaign_recipients(app, client, factory, api):
    configure(client, api)
    for index in range(100, 199):
        api.add_subscriber(index, f"person{index}@example.com", "Person", [1])
    path = launch(client, export_campaign(app, factory), "export")
    first = step(client, path)
    assert first["status"] == "loading" and first["total"] == 101
    result = step(client, path)
    assert result["status"] == "ready" and result["total"] == 3
    assert result["counts"] == {"pending": 2, "skipped": 1}
    assert action(client, path, "commit").status_code == 302
    assert step(client, path)["status"] == "done"
    assert len([method for method, _, _ in api.requests if method == "PATCH"]) == 2


@pytest.mark.parametrize("source,attribute,expected", [("uuid", "", "00000000-0000-0000-0000-000000000010"), ("id", "", "10"), ("attribute", "customer.id", "A-10")])
def test_key_sources(app, client, factory, api, source, attribute, expected):
    configure(client, api)
    item = factory(identity_type="key")
    path = launch(client, item, key_source=source, key_attribute=attribute)
    assert step(client, path)["status"] == "preview"
    action(client, path, "commit")
    with app.app_context():
        assert db.session.scalar(db.select(Recipient).where(Recipient.identity == expected)) is not None


def test_invalid_key_blocks_entire_import(app, client, factory, api):
    configure(client, api)
    item = factory(identity_type="key")
    del api.subscribers[11]["attribs"]["customer"]
    for index in range(100, 200):
        api.add_subscriber(index, f"valid{index}@example.com", "Valid", [1], {"customer": {"id": f"key-{index}"}})
    api.add_subscriber(999, "broken@example.com", "Missing key", [1])
    path = launch(client, item, key_source="attribute", key_attribute="customer.id")
    assert step(client, path)["status"] == "loading"
    assert step(client, path)["status"] == "preview"
    assert "Fehlerhafte Subscribers" in client.get(path).text
    assert "broken@example.com" in client.get(path).text
    action(client, path, "commit")
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 1
        assert counts(path.rsplit("/", 1)[1])["invalid"] == 2


def test_pagination_failure_resume_and_relogin(app, client, factory, api):
    configure(client, api)
    for index in range(100, 201):
        api.add_subscriber(index, f"person{index}@example.com", "Person", [1])
    item = factory()
    path = launch(client, item)
    assert step(client, path)["status"] == "loading"
    api.fail_pages.add((1, 2))
    result = step(client, path)
    assert result["status"] == "failed" and api.token not in result["message"]
    with app.app_context():
        assert sum(counts(path.rsplit("/", 1)[1]).values()) == 100
    api.fail_pages.clear()
    resumed = app.test_client()
    login(resumed)
    assert action(resumed, path, "retry").status_code == 302
    assert step(resumed, path)["status"] == "preview"
    action(resumed, path, "commit")
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Recipient)) == 104


def test_export_scope_attribute_preservation_and_stable_links(app, client, factory, api):
    configure(client, api)
    item = export_campaign(app, factory)
    before = copy.deepcopy(api.subscribers)
    path = launch(client, item, "export")
    assert step(client, path)["status"] == "ready"
    assert not any(method == "PATCH" for method, _, _ in api.requests)
    action(client, path, "commit")
    assert step(client, path)["status"] == "done"
    anna = api.subscribers[10]
    assert anna["attribs"]["city"] == "Köln"
    assert anna["attribs"]["campaign_action_links"]["previous"] == "https://example.com/old"
    link = anna["attribs"]["campaign_action_links"][item["campaign_uuid"]]
    assert anna["status"] == before[10]["status"] and anna["lists"] == before[10]["lists"]
    assert api.subscribers[12] == before[12]
    patches = [(path, payload) for method, path, payload in api.requests if method == "PATCH"]
    assert len(patches) == 2 and all(set(payload) == {"attribs"} for _, payload in patches)
    with app.app_context():
        assert counts(path.rsplit("/", 1)[1]) == {"done": 2, "skipped": 1}
    assert app.test_client().get(link.removeprefix("http://localhost")).status_code == 200


def test_partial_export_retry_skips_completed_records(app, client, factory, api):
    configure(client, api)
    item = export_campaign(app, factory)
    path = launch(client, item, "export")
    step(client, path)
    action(client, path, "commit")
    api.patch_failures[11] = 1
    assert step(client, path)["status"] == "partial"
    first_link = api.subscribers[10]["attribs"]["campaign_action_links"][item["campaign_uuid"]]
    action(client, path, "retry")
    assert step(client, path)["status"] == "done"
    assert api.subscribers[10]["attribs"]["campaign_action_links"][item["campaign_uuid"]] == first_link
    assert len([1 for method, endpoint, _ in api.requests if method == "PATCH" and endpoint.endswith("/10")]) == 1


def test_membership_is_rechecked_before_patch(app, client, factory, api):
    configure(client, api)
    item = export_campaign(app, factory)
    path = launch(client, item, "export")
    step(client, path)
    action(client, path, "commit")
    api.subscribers[10]["lists"] = [{"id": 2}]
    assert step(client, path)["status"] == "done"
    assert item["campaign_uuid"] not in api.subscribers[10]["attribs"]["campaign_action_links"]


def test_config_changes_stop_jobs_and_removal_keeps_revision(app, client, factory, api):
    configure(client, api)
    path = launch(client, factory())
    configure(client, api)
    assert step(client, path)["status"] == "stopped"
    client.post("/admin/settings", data={"csrf_token": csrf(client.get("/admin/settings")), "action": "remove"})
    configure(client, api)
    assert step(client, path)["status"] == "stopped"
    with app.app_context():
        assert db.session.get(ListmonkConfig, 1).revision == 4


def test_unchanged_settings_keep_paused_job_valid(client, factory, api):
    configure(client, api)
    path = launch(client, factory())
    assert configure(client, api, token="").status_code == 302
    assert step(client, path)["status"] == "preview"


def test_expired_lease_can_be_resumed(app, client, factory, api):
    configure(client, api)
    path = launch(client, factory())
    with app.app_context():
        config = db.session.get(ListmonkConfig, 1)
        config.lease_owner, config.lease_job_id = "abandoned", path.rsplit("/", 1)[1]
        config.lease_until = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert step(client, path)["status"] == "preview"
    with app.app_context():
        assert db.session.get(ListmonkConfig, 1).lease_owner is None


def test_preflight_rechecks_late_duplicate_campaign_emails(app, client, factory, api):
    configure(client, api)
    item = factory(identity_type="key")
    path = launch(client, item, "export")
    with app.app_context():
        db.session.add(Recipient(campaign_id=item["campaign_id"], identity="duplicate-key", email="person@example.com", name="Duplicate"))
        db.session.commit()
    result = step(client, path)
    assert result["status"] == "failed" and "eindeutig" in result["message"]
    assert not any(method == "PATCH" for method, _, _ in api.requests)


def test_timeout_and_wrong_credentials_never_expose_tokens(app, client, factory, api, monkeypatch):
    configure(client, api, token="wrong-token")
    response = client.post("/admin/settings/listmonk/test", data={"csrf_token": csrf(client.get("/admin/settings"))}, follow_redirects=True)
    assert "abgelehnt" in response.text and api.token not in response.text
    configure(client, api)
    path = launch(client, factory())
    original = httpx.Client.__init__
    def quick_timeout(self, *args, **kwargs):
        kwargs["timeout"] = .05
        original(self, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, "__init__", quick_timeout)
    api.delay = .2
    result = step(client, path)
    assert result["status"] == "failed" and "Zeitüberschreitung" in result["message"]


def test_concurrent_steps_lease_and_public_requests(app, client, factory, api):
    configure(client, api)
    item = export_campaign(app, factory)
    path = launch(client, item, "export")
    step(client, path)
    action(client, path, "commit")
    worker = app.test_client()
    login(worker)
    api.patch_release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(step, worker, path)
        assert api.patch_entered.wait(timeout=5)
        try:
            assert step(client, path)["status"] == "busy"
            assert configure(client, api).status_code == 400
            assert app.test_client().get("/healthz").status_code == 200
        finally:
            api.patch_release.set()
        assert running.result(timeout=10)["status"] == "done"
