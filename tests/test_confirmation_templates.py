from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from campaign_action.__main__ import migrate
from campaign_action.confirmation_text import parse_confirmation_text, render_confirmation_text
from campaign_action.listmonk import subscriber_row
from campaign_action.models import Campaign, Recipient, db
from conftest import commit_import, csrf, settings_data, upload


@pytest.fixture
def recipient_data():
    return SimpleNamespace(name="Anna", email="anna@example.com", identity="customer-01",
                           attributes={"firma": "Müller & Söhne", "firma-name": "Acme", "nested": {"city": "Berlin"},
                                       "contacts": [{"name": "Ben"}], "zero": 0, "false": False, "empty": "", "null": None,
                                       "items": "Custom items", "__class__": "JSON value", "name": "Custom Anna"},
                           source={"kundennummer": "123", "Customer ID": "A-01"})


@pytest.mark.parametrize("source,expected", [
    ("Hallo {{ name }}!\n{{ email }} · {{ identity }}", "Hallo Anna!\nanna@example.com · customer-01"),
    ("{{ attributes.firma }}", "Müller & Söhne"),
    ('{{ attributes["firma-name"] }}', "Acme"),
    ("{{ attributes.nested.city }}", "Berlin"),
    ("{{ attributes.contacts[0].name }}", "Ben"),
    ("{{ source.kundennummer }}", "123"),
    ('{{ source["Customer ID"] }}', "A-01"),
    ("{{ attributes.items }} · {{ attributes.__class__ }}", "Custom items · JSON value"),
    ("{{ name }} · {{ attributes.name }}", "Anna · Custom Anna"),
    ('{{ attributes.zero | default("other") }} · {{ attributes.false | default("other") }}', "0 · false"),
    ('{{ attributes.empty | default("empty") }} · {{ attributes.null | default("null") }}', "empty · null"),
    ('{{ attributes.missing | default("missing") }} · {{ attributes.missing.nested | default("nested") }}', "missing · nested"),
    ('{{ attributes.contacts[99].name | default("nobody") }}', "nobody"),
    ('{{ attributes.contacts["name"] | default("nobody") }}', "nobody"),
    ("{{ attributes.missing }}{{ attributes.null }}", ""),
    ("{{ attributes.contacts }}", '[{"name":"Ben"}]'),
])
def test_supported_placeholders_and_defaults(recipient_data, source, expected):
    assert render_confirmation_text(source, recipient_data) == expected


def test_values_are_not_recursively_interpreted(recipient_data):
    recipient_data.name = '{{ config.SECRET_KEY }}<script>alert("data")</script>'
    source = '{{ name }} · {{ attributes.missing | default("{{ email }}") }}'
    assert render_confirmation_text(source, recipient_data) == recipient_data.name + " · {{ email }}"
    assert render_confirmation_text('Hallo {{ name | default("Teilnehmer") }}! {{ attributes.firma }}') == "Hallo Teilnehmer! "


@pytest.mark.parametrize("source", [
    "{{ name", "{{ config.SECRET_KEY }}", "{{ recipient.token }}", "{{ request }}", "{{ session }}", "{{ self }}",
    "{{ cycler.__init__.__globals__ }}", "{{ name.upper() }}", "{{ attributes.get('firma') }}", "{{ 7 * 7 }}",
    "{{ name ~ email }}", "{{ name or email }}", "{{ attributes[name] }}", "{{ attributes.contacts[-1] }}",
    "{{ attributes.contacts[true] }}", "{{ name | safe }}", "{{ name | upper }}", "{{ name | default(email) }}",
    "{{ name | default('x', true) }}", "{{ name | default(value='x') }}", "{{ name | default('x') | default('y') }}",
    "{% if name %}Hello{% endif %}", "{% for x in attributes %}{{ x }}{% endfor %}", "{% include 'base.html' %}",
    "{# hidden comment #}", "{{ ['a', 'b'] }}",
])
def test_disallowed_expressions_cannot_be_saved(app, client, factory, source):
    with pytest.raises(ValueError):
        parse_confirmation_text(source)
    item = factory()
    path = f'/admin/campaigns/{item["campaign_id"]}/settings'
    data = settings_data(app, item["campaign_id"], confirmation_text=source, confirmation_heading="Changed", name="Changed")
    data["csrf_token"] = csrf(client.get(path))
    response = client.post(path, data=data)
    assert response.status_code == 400
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        assert campaign.confirmation_text == "Eigener Bestätigungstext"
        assert campaign.confirmation_heading == "Bestätigung erfolgreich" and campaign.name == "Test Campaign"


def personalize(app, item):
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.confirmation_heading = "Teilnahme bestätigt"
        campaign.confirmation_text = 'Hallo {{ name | default("Teilnehmer") }}! {{ attributes.firma }} · {{ source.kundennummer }}'
        recipient = db.session.get(Recipient, item["recipient_id"])
        recipient.attributes = {"firma": "Acme"}
        recipient.source = {"kundennummer": "123"}
        db.session.commit()


@pytest.mark.parametrize("mode", ["visit", "action"])
@pytest.mark.parametrize("show_text", [False, True])
def test_personalization_obeys_visibility_and_survives_form_errors(app, factory, mode, show_text):
    item = factory(mode=mode, form=True, show_text=show_text)
    personalize(app, item)
    public = app.test_client()
    initial = public.get(item["path"])
    text = "Hallo Person! Acme · 123"
    assert (text in initial.text) == show_text
    assert ("Teilnahme bestätigt" in initial.text) == (mode == "visit" and show_text)
    invalid = public.post(item["path"], data={"csrf_token": csrf(initial)})
    assert invalid.status_code == 422
    assert (text in invalid.text) == show_text
    assert ("Teilnahme bestätigt" in invalid.text) == (mode == "visit" and show_text)
    complete = public.post(item["path"], data={"csrf_token": csrf(initial), f'field_{item["field_ids"][0]}': "Form name"},
                           follow_redirects=True)
    assert complete.status_code == 200
    assert text in complete.text and "Teilnahme bestätigt" in complete.text and "Vielen Dank!" in complete.text
    assert "Hallo Form name!" not in complete.text  # Only stored recipient data is used.


def test_html_is_escaped_in_heading_template_and_attribute_values(app, factory):
    item = factory(mode="visit")
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.confirmation_heading = '<img src=x onerror="alert(1)">'
        campaign.confirmation_text = '<b>{{ name }}</b>\n{{ attributes.firma }}'
        recipient = db.session.get(Recipient, item["recipient_id"])
        recipient.name = '<script>alert("name")</script>'
        recipient.attributes = {"firma": '<img src=x onerror="alert(2)">'}
        db.session.commit()
    page = app.test_client().get(item["path"])
    assert "<script>" not in page.text and "<img src=x" not in page.text and "<b>" not in page.text
    assert "&lt;script&gt;" in page.text and "&lt;img src=x" in page.text and "&lt;b&gt;" in page.text


@pytest.mark.parametrize("heading", ["", "   ", "x" * 201])
def test_invalid_heading_is_rejected(app, client, factory, heading):
    item = factory()
    path = f'/admin/campaigns/{item["campaign_id"]}/settings'
    data = settings_data(app, item["campaign_id"], confirmation_heading=heading)
    data["csrf_token"] = csrf(client.get(path))
    assert client.post(path, data=data).status_code == 400
    with app.app_context():
        assert db.session.get(Campaign, item["campaign_id"]).confirmation_heading == "Bestätigung erfolgreich"


def test_heading_and_template_can_be_edited_after_confirmation_and_duplicated(app, client, factory):
    item = factory(mode="visit")
    app.test_client().get(item["path"])
    path = f'/admin/campaigns/{item["campaign_id"]}'
    data = settings_data(app, item["campaign_id"], confirmation_heading=" Danke, {{ name }}! ",
                         confirmation_text='Hallo {{ name | default("Teilnehmer") }}')
    data["csrf_token"] = csrf(client.get(path + "/settings"))
    assert client.post(path + "/settings", data=data).status_code == 302
    assert "Danke, {{ name }}!" in app.test_client().get(item["path"]).text  # Heading is plain text.
    assert client.post(path + "/duplicate", data={"csrf_token": csrf(client.get(path))}).status_code == 302
    with app.app_context():
        copy = db.session.scalar(db.select(Campaign).where(Campaign.id != item["campaign_id"]))
        assert copy.confirmation_heading == "Danke, {{ name }}!"
        assert copy.confirmation_text == 'Hallo {{ name | default("Teilnehmer") }}'


def test_preview_uses_selected_recipient_without_writes(app, client, factory):
    item = factory(mode="visit", form=True, show_text=True)
    personalize(app, item)
    with app.app_context():
        db.session.get(Recipient, item["recipient_id"]).token = None
        db.session.commit()
    path = f'/admin/campaigns/{item["campaign_id"]}/preview'
    assert "Hallo Teilnehmer!  · " in client.get(path).text
    page = client.get(path + f'?recipient_id={item["recipient_id"]}')
    assert "Hallo Person! Acme · 123" in page.text and "Teilnahme bestätigt" in page.text
    assert client.head(path + f'?recipient_id={item["recipient_id"]}').status_code == 200
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.token is None and recipient.confirmed_at is None and recipient.submission is None
        assert recipient.campaign.locked_at is None
    assert app.test_client().get(path + f'?recipient_id={item["recipient_id"]}').status_code == 302


def test_preview_rejects_foreign_and_invalid_recipient_ids(app, client, factory):
    first, second = factory(), factory()
    path = f'/admin/campaigns/{first["campaign_id"]}/preview'
    for recipient_id in (second["recipient_id"], 9999, "invalid", -1):
        assert client.get(path, query_string={"recipient_id": recipient_id}).status_code == 404
    with app.app_context():
        assert db.session.get(Recipient, second["recipient_id"]).confirmed_at is None


def test_preview_search_is_bounded_and_preserves_selected_recipient(app, client, factory):
    item = factory(mode="visit")
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.confirmation_text = "Hallo {{ name }}"
        for index in range(55):
            db.session.add(Recipient(campaign=campaign, identity=f"new-{index:02d}@example.com", email=f"new-{index:02d}@example.com",
                                     name=f"Name {index:02d}"))
        db.session.commit()
    path = f'/admin/campaigns/{item["campaign_id"]}/preview'
    page = client.get(path)
    assert "Die ersten 50 Treffer" in page.text
    assert page.text.count('<option value="') == 51
    selected = client.get(path, query_string={"search": "Name 00", "recipient_id": item["recipient_id"]})
    assert "Hallo Person" in selected.text and 'value="' + str(item["recipient_id"]) + '" selected' in selected.text
    assert "new-00@example.com" in selected.text and "new-01@example.com" not in selected.text
    assert "Keine passenden Empfänger gefunden." in client.get(path, query_string={"search": "%"}).text
    by_email = client.get(path, query_string={"search": "new-54@example.com"})
    assert "Name 54" in by_email.text and "Name 53" not in by_email.text


def test_csv_imported_custom_attributes_and_source_fields_are_personalized(app, client, factory):
    item = factory(mode="visit")
    preview = upload(client, item["campaign_id"], b'email,name,attributes,kundennummer\nanna@example.com,Anna,"{""firma"":""Acme""}",A-01\n')
    commit_import(client, preview)
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.confirmation_text = "Hallo {{ name }}! {{ attributes.firma }} · {{ source.kundennummer }}"
        recipient = db.session.scalar(db.select(Recipient).where(Recipient.identity == "anna@example.com"))
        recipient_id = recipient.id
        db.session.commit()
    page = client.get(f'/admin/campaigns/{item["campaign_id"]}/preview?recipient_id={recipient_id}')
    assert "Hallo Anna! Acme · A-01" in page.text


def test_listmonk_custom_attributes_use_the_same_rendering(app, client, factory):
    item = factory(mode="visit")
    row, error = subscriber_row({"id": 77, "uuid": "listmonk-person", "email": "listmonk@example.com", "name": "Listmonk Person",
                                 "attribs": {"firma": "Listmonk Company", "nested": {"city": "Berlin"}}}, "email", "uuid", "")
    assert error == ""
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        campaign.confirmation_text = "{{ name }} · {{ attributes.firma }} · {{ attributes.nested.city }} · {{ source.listmonk_id }}"
        recipient = Recipient(campaign=campaign, identity=row["identity"], email=row["email"], name=row["name"],
                              attributes=row["attributes"], source=row["source"])
        db.session.add(recipient)
        db.session.commit()
        recipient_id = recipient.id
    page = client.get(f'/admin/campaigns/{item["campaign_id"]}/preview?recipient_id={recipient_id}')
    assert "Listmonk Person · Listmonk Company · Berlin · 77" in page.text


def test_migration_sets_default_heading_without_changing_existing_data(app, factory):
    item = factory(mode="visit")
    app.test_client().get(item["path"])
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        before = (recipient.token, recipient.confirmed_at, recipient.campaign.confirmation_text)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE campaign DROP COLUMN confirmation_heading")
            connection.exec_driver_sql("UPDATE alembic_version SET version_num='0004'")
    migrate(app)
    migrate(app)
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.campaign.confirmation_heading == "Bestätigung erfolgreich"
        assert (recipient.token, recipient.confirmed_at, recipient.campaign.confirmation_text) == before
        assert "confirmation_heading" in {column["name"] for column in inspect(db.engine).get_columns("campaign")}
