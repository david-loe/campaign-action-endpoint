"""Optional Chromium check using a disposable local server and SQLite database."""
import argparse
import csv
import io
import secrets
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

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
                page.get_by_role("link", name="+ Neue Campaign").click()
                page.get_by_label("Campaign-Name").fill("Herbstkampagne 2026")
                page.get_by_label("Formular aktivieren", exact=True).check()
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
                if args.screenshots:
                    page.screenshot(path=str(args.screenshots / "admin.png"), full_page=True)
                mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                recipient = mobile.new_page()
                recipient.on("pageerror", lambda error: errors.append(str(error)))
                recipient.goto(rows[0]["link"])
                assert recipient.get_by_text("Vielen Dank für Ihre Teilnahme!", exact=True).is_visible()
                assert recipient.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
                if args.screenshots:
                    recipient.screenshot(path=str(args.screenshots / "public.png"), full_page=True)
                recipient.get_by_label("Ihr Name").fill("Anna Beispiel")
                recipient.get_by_label("Kontakt-E-Mail").fill("anna@example.com")
                recipient.get_by_label("Wunschtermin").fill("2026-11-01")
                recipient.get_by_label("Anzahl Gäste").fill("2")
                recipient.get_by_role("button", name="Jetzt bestätigen").click()
                assert recipient.get_by_text("Ihre Angaben wurden gespeichert. Die Abgabe ist abgeschlossen.").is_visible()
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
                print("Browser smoke passed: admin, CSV/forms, mobile layout, Listmonk settings, API import and export with browser-driven steps.")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
