"""Optional Chromium check using a disposable local server and SQLite database."""
import argparse
import csv
import io
import re
import secrets
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from campaign_action import create_app
from campaign_action.__main__ import migrate
from listmonk_test_server import ListmonkTestServer


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path, help="Optional screenshot directory")
    parser.add_argument("--confirmation-mode", choices=("action", "visit"), default="action")
    parser.add_argument("--hide-text-with-form", action="store_true")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as directory, ListmonkTestServer() as api:
        password = secrets.token_urlsafe(24)
        app = create_app({"ENCYPTION_SECRET": secrets.token_urlsafe(48),
                          "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": password, "PUBLIC_BASE_URL": "http://localhost:8080",
                          "DATABASE_PATH": directory + "/app.sqlite3"})
        migrate(app)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        origin = f"http://127.0.0.1:{server.server_port}"
        app.config["PUBLIC_BASE_URL"] = origin
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1050})
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin + "/admin/login")
                page.get_by_label("Benutzername").fill("admin")
                page.get_by_label("Passwort").fill(password)
                page.get_by_role("button", name="Anmelden", exact=True).click()
                page.wait_for_url("**/admin/campaigns")
                page.get_by_role("link", name="Admin-Einstellungen", exact=True).click()
                logo = io.BytesIO()
                Image.new("RGB", (160, 48), color="#4b267d").save(logo, format="PNG")
                page.get_by_label("Akzentfarbe (Hex)").fill("#4b267d")
                assert page.get_by_label("Akzentfarbe auswählen", exact=True).input_value() == "#4b267d"
                page.get_by_label("Logo hochladen").set_input_files({"name": "logo.png", "mimeType": "image/png",
                                                                               "buffer": logo.getvalue()})
                page.get_by_role("button", name="Branding speichern", exact=True).click()
                assert page.locator(".branding-logo-preview").evaluate("image => image.complete && image.naturalWidth === 160")
                page.get_by_role("link", name="Campaigns", exact=True).click()
                page.get_by_role("link", name="+ Neue Campaign").click()
                page.get_by_label("Campaign-Name", exact=True).fill("Herbstkampagne 2026")
                page.locator('select[name="confirmation_mode"]').select_option(args.confirmation_mode)
                page.get_by_label("Formular aktivieren", exact=True).check()
                if not args.hide_text_with_form:
                    page.get_by_label("Bestätigungstext bereits über dem offenen Formular anzeigen").check()
                page.locator('textarea[name="confirmation_text"]').fill("Vielen Dank für Ihre Teilnahme!")
                for label, kind, required in [("Ihr Name", "text", "1"), ("Kontakt-E-Mail", "email", "1"),
                                               ("Anzahl Gäste", "number", "0"), ("Wunschtermin", "date", "0")]:
                    page.locator("#add-field").click()
                    row = page.locator(".field-editor").last
                    row.get_by_label("Bezeichnung").fill(label)
                    row.locator('select[name="field_type"]').select_option(kind)
                    row.locator('select[name="field_required"]').select_option(required)
                    row.get_by_label("Beschreibung").fill("Bitte ergänzen Sie Ihre Angaben.")
                page.locator('[data-move="up"]').last.click()
                page.get_by_role("button", name="Campaign speichern").click()
                page.wait_for_url("**/admin/campaigns/*")
                campaign_url = page.url
                page.get_by_role("link", name="CSV importieren", exact=True).click()
                page.locator('input[name="file"]').set_input_files({"name": "subscribers.csv", "mimeType": "text/csv",
                    "buffer": b"email,name\nanna@example.com,Anna\nben@example.com,Ben\n"})
                page.get_by_role("button", name="Importvorschau anzeigen").click()
                page.get_by_role("button", name="2 Empfänger importieren").click()
                page.wait_for_url(campaign_url)
                with page.expect_download() as download:
                    page.get_by_role("button", name="E-Mail + Link exportieren").click()
                rows = list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text(encoding="utf-8-sig"))))
                assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", row["link"].rsplit("/r/", 1)[1]) for row in rows)
                if args.screenshots:
                    page.screenshot(path=str(args.screenshots / "admin.png"), full_page=True)
                mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                mobile.add_init_script("""window.cspViolations = [];
                    document.addEventListener('securitypolicyviolation', event => window.cspViolations.push(event.violatedDirective));""")
                recipient = mobile.new_page()
                recipient.on("pageerror", lambda error: errors.append(str(error)))
                recipient.goto(rows[0]["link"])
                assert recipient.locator(".public-logo").evaluate("image => image.complete && image.naturalWidth === 160")
                assert recipient.locator(".primary").evaluate("button => getComputedStyle(button).backgroundColor") == "rgb(75, 38, 125)"
                assert recipient.get_by_text("Herbstkampagne 2026", exact=True).is_visible()
                assert "Campaign Action" not in recipient.locator("body").inner_text()
                assert "Ihre Rückmeldung" not in recipient.locator("body").inner_text()
                assert recipient.get_by_text("Bestätigung erfolgreich", exact=True).count() == (1 if args.confirmation_mode == "visit" and not args.hide_text_with_form else 0)
                assert recipient.evaluate("window.cspViolations.length === 0"), "Public page CSP violation"
                assert recipient.get_by_text("Vielen Dank für Ihre Teilnahme!", exact=True).count() == (0 if args.hide_text_with_form else 1)
                assert recipient.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
                if args.screenshots:
                    recipient.screenshot(path=str(args.screenshots / "public.png"), full_page=True)
                recipient.get_by_label("Ihr Name").fill("Anna Beispiel")
                recipient.get_by_label("Kontakt-E-Mail").fill("anna@example.com")
                recipient.get_by_label("Wunschtermin").fill("2026-11-01")
                recipient.get_by_label("Anzahl Gäste").fill("2")
                recipient.get_by_role("button", name="Angaben absenden" if args.confirmation_mode == "visit" else "Jetzt bestätigen").click()
                assert recipient.get_by_text("Ihre Angaben wurden gespeichert. Die Abgabe ist abgeschlossen.").is_visible()
                assert recipient.get_by_text("Bestätigung erfolgreich", exact=True).is_visible()
                assert recipient.locator(".completion-mark svg path").evaluate("path => getComputedStyle(path).stroke") == "rgb(21, 117, 82)"
                if args.screenshots:
                    recipient.screenshot(path=str(args.screenshots / "public-confirmed.png"), full_page=True)
                recipient.reload()
                assert recipient.locator("form").count() == 0
                page.goto(campaign_url)
                with page.expect_download() as download:
                    page.get_by_role("link", name="Ergebnisse exportieren").click()
                results = list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text(encoding="utf-8-sig"))))
                assert results[0]["confirmed_at"] and not results[1]["confirmed_at"]
                assert "Anna Beispiel" in results[0].values()
                page.get_by_role("link", name="Einstellungen", exact=True).click()
                assert page.locator("#add-field").count() == 0
                assert page.locator('select[name="confirmation_mode"]').count() == 0
                page.get_by_label("Branding verwenden").select_option("custom")
                page.get_by_label("Akzentfarbe (Hex)").fill("#ffcc00")
                page.get_by_label("Campaign-Name auf der Bestätigungsseite anzeigen", exact=True).uncheck()
                page.get_by_role("button", name="Campaign speichern", exact=True).click()
                preview = context.new_page()
                preview.goto(campaign_url + "/preview")
                assert preview.get_by_text("Bestätigung erfolgreich", exact=True).count() == (1 if args.confirmation_mode == "visit" and not args.hide_text_with_form else 0)
                assert preview.locator(".public-logo").count() == 0
                assert preview.title() == "Bestätigung"
                assert preview.locator(".primary").evaluate("button => getComputedStyle(button).backgroundColor") == "rgb(255, 204, 0)"
                assert preview.locator(".primary").evaluate("button => getComputedStyle(button).color") == "rgb(0, 0, 0)"
                if args.screenshots:
                    preview.screenshot(path=str(args.screenshots / "public-desktop-custom.png"), full_page=True)
                preview.close()
                recipient.reload()
                assert recipient.locator(".public-logo").count() == 0
                assert recipient.title() == "Bestätigung"
                assert recipient.get_by_text("Herbstkampagne 2026", exact=True).count() == 0
                assert recipient.evaluate("window.cspViolations.length === 0"), "Custom branding CSP violation"
                page.get_by_role("link", name="Admin-Einstellungen", exact=True).click()
                page.locator('input[name="base_url"]').fill(api.url)
                page.locator('input[name="username"]').fill(api.username)
                page.locator('input[name="token"]').fill(api.token)
                page.get_by_label("Listmonk-Verbindung aktivieren").check()
                page.get_by_role("button", name="Einstellungen speichern", exact=True).click()
                page.get_by_role("button", name="Verbindung testen", exact=True).click()
                assert page.get_by_text("Verbindung erfolgreich: 2 aktive Listen sind zugänglich.", exact=True).is_visible()
                api.add_subscriber(21, "charlie@example.com", "Charlie", [1])
                page.goto(campaign_url)
                page.get_by_role("link", name="Aus Listmonk importieren", exact=True).click()
                page.locator('select[name="list_id"]').select_option("1")
                page.get_by_role("button", name="Liste laden und Vorschau erstellen").click()
                page.get_by_role("heading", name="Import prüfen", exact=True).wait_for()
                page.get_by_role("button", name="1 Empfänger importieren", exact=True).click()
                page.wait_for_url(campaign_url)
                page.get_by_role("link", name="Links an Listmonk übertragen", exact=True).click()
                page.locator('select[name="list_id"]').select_option("1")
                page.get_by_role("button", name="Liste laden und Vorschau erstellen").click()
                page.get_by_role("heading", name="Export prüfen", exact=True).wait_for()
                page.get_by_role("button", name="3 Links übertragen", exact=True).click()
                page.get_by_role("heading", name="Abgeschlossen", exact=True).wait_for()
                assert api.subscribers[10]["attribs"]["campaign_action_links"]["previous"] == "https://example.com/old"
                assert len(api.subscribers[10]["attribs"]["campaign_action_links"]) == 2
                assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", link.rsplit("/r/", 1)[1])
                           for campaign, link in api.subscribers[10]["attribs"]["campaign_action_links"].items() if campaign != "previous")
                if args.screenshots:
                    page.screenshot(path=str(args.screenshots / "listmonk.png"), full_page=True)
                mobile_admin = mobile.new_page()
                mobile_admin.goto(origin + "/admin/login")
                mobile_admin.get_by_label("Benutzername").fill("admin")
                mobile_admin.get_by_label("Passwort").fill(password)
                mobile_admin.get_by_role("button", name="Anmelden", exact=True).click()
                mobile_admin.goto(campaign_url)
                assert mobile_admin.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile admin overflow"
                assert not errors, errors
                browser.close()
                print("Browser smoke passed: global/custom branding, logo upload, CSP, CSV/forms, mobile layout, Listmonk import and export.")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
