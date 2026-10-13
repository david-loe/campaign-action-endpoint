import io
import re
from xml.etree import ElementTree

import pytest
from PIL import Image

from campaign_action.branding import MAX_LOGO_BYTES, branding_css, luminance
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


@pytest.mark.parametrize("bad_logo", [b"", b"not an image", b'<svg xmlns="http://wrong.namespace/"/>',
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


SVG_LOGO = b'''<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="160" height="48" viewBox="0 0 160 48">
<defs><linearGradient id="brand"><stop offset="0" stop-color="#2359dc"/><stop offset="1" stop-color="#663399"/></linearGradient><path id="mark" d="M0 0L10 10"/></defs>
<style>.brand{fill:url("#brand")}.label{fill:#2359dc}</style><rect class="brand" width="160" height="48"/><use xlink:href="#mark"/><text class="label" x="10" y="30">Company</text></svg>'''


@pytest.mark.parametrize("data", [SVG_LOGO, SVG_LOGO.decode().replace('<?xml version="1.0"?>', '').encode("utf-16"),
                                  b'<svg width="32" height="32" viewBox="0 0 32 32"><path d="M0 0L32 32"/></svg>'])
def test_svg_uploads_have_correct_delivery_and_preserve_local_styles(app, client, factory, data):
    item = factory()
    assert save_global(client, logo=data).status_code == 302
    response = app.test_client().get(assets(item) + "/logo")
    assert response.mimetype == "image/svg+xml"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert ElementTree.fromstring(response.data).tag == "{http://www.w3.org/2000/svg}svg"
    if b"Company" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        assert b'url("#brand")' in response.data and b"linearGradient" in response.data
        assert b'href="#mark"' in response.data
    assert save_campaign(app, client, item, branding_mode="custom", logo=data).status_code == 302
    assert app.test_client().get(assets(item) + "/logo").mimetype == "image/svg+xml"
    page = app.test_client().get(item["path"])
    assert "style-src 'self'" in page.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in page.headers["Content-Security-Policy"]


def test_svg_active_content_and_external_references_are_removed(app, client, factory):
    item = factory()
    logo = br'''<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" onload="alert(1)" xml:base="https://evil.example/">
    <script>alert(2)</script><foreignObject><div xmlns="http://www.w3.org/1999/xhtml">bad</div></foreignObject>
    <style>@import "https://evil.example/style.css";</style><image href="https://evil.example/pixel"/>
    <rect width="32" height="32" onclick="alert(3)" style='fill:u\72l("https://evil.example/fill")'/>
    <a href="javascript:alert(4)"><circle r="5"/></a><set attributeName="href" to="https://evil.example/change"/>
    </svg>'''
    assert save_global(client, logo=logo).status_code == 302
    response = app.test_client().get(assets(item) + "/logo")
    assert b"alert" not in response.data and b"evil.example" not in response.data
    root = ElementTree.fromstring(response.data)
    tags = [element.tag.rsplit("}", 1)[-1] for element in root.iter()]
    assert "script" not in tags and "foreignObject" not in tags and "set" not in tags
    assert "rect" in tags and "circle" in tags
    assert "img-src data:" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("logo", [b'<svg xmlns="http://www.w3.org/2000/svg">', b"<html>not SVG</html>",
                                  b'<!DOCTYPE svg [<!ENTITY value "expanded">]><svg xmlns="http://www.w3.org/2000/svg">&value;</svg>',
                                  b'<!DOCTYPE svg [<!ENTITY value SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&value;</svg>',
                                  b'<svg xmlns="http://www.w3.org/2000/svg">' + b'<g>' * 1500 + b'</g>' * 1500 + b'</svg>'])
def test_invalid_svg_preserves_previous_logo_and_color(app, client, factory, logo):
    item = factory()
    original = logo_bytes()
    assert save_global(client, logo=original).status_code == 302
    assert save_global(client, color="#abcdef", logo=logo).status_code == 400
    assert save_campaign(app, client, item, branding_mode="custom", logo=logo).status_code == 400
    with app.app_context():
        global_branding = db.session.get(BrandingConfig, 1)
        assert global_branding.logo_data == original and global_branding.accent_color == "#663399"
        assert db.session.get(Campaign, item["campaign_id"]).branding_mode == "global"


@pytest.mark.parametrize("color", ["#000000", "#ffffff", "#2359dc", "#ffcc00", "#663399", "#00ff00"])
def test_branding_text_colors_have_contrast_in_both_schemes(color):
    css = branding_css(color)
    readable_colors = re.findall(r"--accent-readable:(#[0-9a-f]{6})", css)
    assert len(readable_colors) == 2 and f"--blue:{color}" in css
    for foreground, background in zip(readable_colors, ((255, 255, 255), (25, 44, 71))):
        rgb = tuple(int(foreground[index:index + 2], 16) for index in (1, 3, 5))
        light, dark = sorted((luminance(rgb), luminance(background)), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5
