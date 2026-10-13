import io

import pytest
from PIL import Image

from campaign_action.branding import MAX_LOGO_BYTES
from campaign_action.models import BrandingConfig, Campaign, ListmonkConfig, Recipient, db
from conftest import csrf, settings_data


def logo_bytes(kind="PNG", size=(80, 24), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format=kind)
    return output.getvalue()


def save_global(client, color="#663399", logo=None, **changes):
    data = {"csrf_token": csrf(client.get("/admin/settings")), "accent_color": color, **changes}
    if logo is not None:
        data["logo"] = (io.BytesIO(logo), "logo.png")
    return client.post("/admin/settings/branding", data=data)


def save_campaign(app, client, item, logo=None, **changes):
    path = f'/admin/campaigns/{item["campaign_id"]}/settings'
    data = settings_data(app, item["campaign_id"], **changes)
    data["csrf_token"] = csrf(client.get(path))
    if logo is not None:
        data["logo"] = (io.BytesIO(logo), "logo.png")
    return client.post(path, data=data)


def assets(item):
    return f'/branding/{item["campaign_uuid"]}'


@pytest.mark.parametrize("mode", ["visit", "action"])
@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("show_name", [False, True])
def test_public_page_cleanup_in_all_states(app, client, factory, mode, form, show_name):
    item = factory(mode=mode, form=form)
    assert save_campaign(app, client, item, show_campaign_name="1" if show_name else "0").status_code == 302
    public = app.test_client()
    preview = client.get(f'/admin/campaigns/{item["campaign_id"]}/preview')
    initial = public.get(item["path"])
    pages = [preview, initial]
    if form:
        invalid = public.post(item["path"], data={"csrf_token": csrf(initial)})
        assert invalid.status_code == 422
        pages.append(invalid)
    if form or mode == "action":
        data = {"csrf_token": csrf(initial)}
        if form:
            data[f'field_{item["field_ids"][0]}'] = "Ada"
        pages.append(public.post(item["path"], data=data, follow_redirects=True))
    for page in pages:
        assert "Campaign Action" not in page.text
        assert "Ihre Rückmeldung" not in page.text
        assert "Bitte ergänzen Sie Ihre Angaben im Formular." not in page.text
        assert ("Test Campaign" in page.text) == show_name
        assert '<title>Bestätigung</title>' in page.text if not show_name else '<title>Test Campaign</title>' in page.text
    with app.app_context():
        assert db.session.get(Recipient, item["recipient_id"]).confirmed_at is not None


@pytest.mark.parametrize("kind,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_global_logo_validation_and_public_delivery(app, client, factory, kind, mime):
    item = factory(mode="visit")
    blob = logo_bytes(kind)
    assert save_global(client, logo=blob).status_code == 302
    public = app.test_client()
    response = public.get(assets(item) + "/logo")
    assert response.data == blob
    assert response.mimetype == mime
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "img-src 'self'" in response.headers["Content-Security-Policy"]
    assert 'class="public-logo"' in public.get(item["path"]).text
    assert "--blue:#663399" in public.get(assets(item) + "/style.css").text
    with app.app_context():
        assert db.session.get(BrandingConfig, 1).logo_data == blob
    assert save_global(client, color="#229955").status_code == 302
    assert public.get(assets(item) + "/logo").data == blob
    assert "--blue:#229955" in public.get(assets(item) + "/style.css").text
    assert save_global(client, remove_logo="1").status_code == 302
    assert public.get(assets(item) + "/logo").status_code == 404
    assert 'class="public-header"' not in public.get(item["path"]).text


@pytest.mark.parametrize("color", ["red", "#fff", "#123456;", "#000000}</style><script>", "", "#12345g"])
def test_invalid_colors_are_atomic(app, client, factory, color):
    blob = logo_bytes()
    assert save_global(client, logo=blob).status_code == 302
    invalid = save_global(client, color=color, remove_logo="1")
    assert invalid.status_code == 400
    assert '<form method="post" action="/admin/settings"' in invalid.text
    item = factory()
    assert save_campaign(app, client, item, branding_mode="custom", accent_color=color, logo=blob).status_code == 400
    with app.app_context():
        branding = db.session.get(BrandingConfig, 1)
        campaign = db.session.get(Campaign, item["campaign_id"])
        assert branding.accent_color == "#663399" and branding.logo_data == blob
        assert campaign.branding_mode == "global" and campaign.logo_data is None


@pytest.mark.parametrize("bad_logo", [b"", b"not an image", b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                                       b"x" * (MAX_LOGO_BYTES + 1), logo_bytes(size=(4097, 1)),
                                       logo_bytes("JPEG")[:100], logo_bytes("PNG")[:40]])
def test_invalid_uploads_preserve_saved_settings(app, client, factory, bad_logo):
    original = logo_bytes()
    assert save_global(client, logo=original).status_code == 302
    assert save_global(client, color="#abcdef", logo=bad_logo).status_code == 400
    item = factory()
    assert save_campaign(app, client, item, branding_mode="custom", accent_color="#abcdef", logo=bad_logo).status_code == 400
    with app.app_context():
        branding = db.session.get(BrandingConfig, 1)
        assert branding.logo_data == original and branding.accent_color == "#663399"
        campaign = db.session.get(Campaign, item["campaign_id"])
        assert campaign.branding_mode == "global" and campaign.logo_data is None


def test_campaign_branding_is_a_complete_override_and_survives_mode_switches(app, client, factory):
    item, other = factory(), factory()
    global_logo, own_logo = logo_bytes(), logo_bytes(color="blue")
    assert save_global(client, logo=global_logo).status_code == 302
    assert save_campaign(app, client, item, branding_mode="custom", accent_color="#ffff00").status_code == 302
    public = app.test_client()
    assert 'class="public-logo"' not in public.get(item["path"]).text
    assert public.get(assets(item) + "/logo").status_code == 404
    css = public.get(assets(item) + "/style.css")
    assert css.mimetype == "text/css" and "--blue:#ffff00" in css.text and "--accent-text:#000000" in css.text
    assert public.get(assets(other) + "/logo").data == global_logo
    assert save_campaign(app, client, item, branding_mode="custom", accent_color="#AA22CC", logo=own_logo).status_code == 302
    assert save_global(client, color="#112233").status_code == 302
    assert "--blue:#aa22cc" in public.get(assets(item) + "/style.css").text
    assert "--blue:#112233" in public.get(assets(other) + "/style.css").text
    assert public.get(assets(item) + "/logo").data == own_logo
    assert save_campaign(app, client, item, branding_mode="global", accent_color="ignored").status_code == 302
    assert public.get(assets(item) + "/logo").data == global_logo
    with app.app_context():
        assert db.session.get(Campaign, item["campaign_id"]).accent_color == "#aa22cc"
    assert save_campaign(app, client, item, branding_mode="custom").status_code == 302
    assert public.get(assets(item) + "/logo").data == own_logo
    assert save_campaign(app, client, item, remove_logo="1").status_code == 302
    assert public.get(assets(item) + "/logo").status_code == 404


def test_locked_campaign_branding_and_duplicate_are_independent(app, client, factory):
    item = factory(mode="visit", form=True)
    app.test_client().get(item["path"])
    blob = logo_bytes()
    assert save_campaign(app, client, item, branding_mode="custom", accent_color="#123456", logo=blob,
                         show_campaign_name="0").status_code == 302
    path = f'/admin/campaigns/{item["campaign_id"]}'
    assert client.post(path + "/duplicate", data={"csrf_token": csrf(client.get(path))}).status_code == 302
    with app.app_context():
        copy = db.session.scalar(db.select(Campaign).where(Campaign.id != item["campaign_id"]))
        assert copy.branding_mode == "custom" and copy.accent_color == "#123456"
        assert copy.logo_data == blob and not copy.show_campaign_name and copy.locked_at is None
        copy_uuid = copy.uuid
    assert save_campaign(app, client, item, logo=logo_bytes(color="blue")).status_code == 302
    assert app.test_client().get(f"/branding/{copy_uuid}/logo").data == blob


def test_branding_auth_csrf_and_assets_do_not_confirm(app, client, factory):
    item = factory(mode="visit", active=False)
    public = app.test_client()
    assert public.post("/admin/settings/branding").status_code == 400
    token = csrf(public.get("/admin/login"))
    assert public.post("/admin/settings/branding", data={"csrf_token": token}).status_code == 302
    assert public.get("/admin/settings/branding/logo").status_code == 302
    assert client.post("/admin/settings/branding", data={"accent_color": "#123456"}).status_code == 400
    assert save_global(client, logo=logo_bytes()).status_code == 302
    for suffix in ("/logo", "/style.css"):
        assert public.get(assets(item) + suffix).status_code == 404
        assert client.get(assets(item) + suffix).status_code == 200
        assert client.head(assets(item) + suffix).status_code == 200
    with app.app_context():
        recipient = db.session.get(Recipient, item["recipient_id"])
        assert recipient.confirmed_at is None and recipient.campaign.locked_at is None


def test_branding_does_not_modify_listmonk_configuration(app, client):
    with app.app_context():
        db.session.add(ListmonkConfig(id=1, base_url="https://example.com", username="user", token="encrypted",
                                     enabled=True, revision=7, lease_owner="owner", lease_job_id="job"))
        db.session.commit()
    assert save_global(client, logo=logo_bytes()).status_code == 302
    with app.app_context():
        config = db.session.get(ListmonkConfig, 1)
        assert config.revision == 7 and config.enabled and config.token == "encrypted"
        assert config.lease_owner == "owner" and config.lease_job_id == "job"


def test_default_branding_and_conflicting_logo_actions(app, client, factory):
    item = factory()
    with app.app_context():
        campaign = db.session.get(Campaign, item["campaign_id"])
        assert campaign.show_campaign_name and campaign.branding_mode == "global"
    assert "--blue:#2359dc" in app.test_client().get(assets(item) + "/style.css").text
    assert save_global(client, logo=logo_bytes(), remove_logo="1").status_code == 400
    assert save_campaign(app, client, item, branding_mode="custom", logo=logo_bytes(), remove_logo="1").status_code == 400
    assert save_campaign(app, client, item, branding_mode="invalid").status_code == 400
