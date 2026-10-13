# Campaign Action Endpoint

Web-App für Campaign-Bestätigungen und Formulare mit Python, SQLite und Docker.

## Start

```bash
cp .env.example .env
openssl rand -hex 32
```

Den erzeugten Wert als `ENCYPTION_SECRET` in `.env` eintragen und `ADMIN_PASSWORD` setzen. Danach das veröffentlichte Image starten:

```bash
docker compose pull
docker compose up -d --no-build
```

Alternativ lokal bauen und starten: `docker compose up -d --build`.

Verwaltung: **http://localhost:8080/admin/campaigns**. Daten bleiben im Docker-Volume erhalten; Migrationen laufen automatisch beim Start.

## Konfiguration

| Variable | Bedeutung |
| --- | --- |
| `ENCYPTION_SECRET` | Erforderlich, mindestens 32 Zeichen. Schützt Sessions und Listmonk-Zugangsdaten; dauerhaft unverändert lassen. |
| `ADMIN_USERNAME` | Admin-Benutzername, standardmäßig `admin`. |
| `ADMIN_PASSWORD` | Erforderlich, mindestens 12 Zeichen. |
| `PUBLIC_BASE_URL` | Öffentlicher HTTP(S)-Origin ohne Unterpfad, standardmäßig `http://localhost:8080`. |
| `PORT` | Docker-Port auf dem Host, standardmäßig `8080`. |
| `DATABASE_PATH` | Bei direktem Python-Start konfigurierbar; Docker verwendet `/data/app.sqlite3`. |

Für öffentlichen Betrieb einen HTTPS-Reverse-Proxy verwenden und `PUBLIC_BASE_URL` entsprechend setzen. HTTPS aktiviert sichere Session-Cookies.

Compose nutzt `ghcr.io/david-loe/campaign-action-endpoint:main`. Für eine feste Version den Image-Tag direkt in `compose.yaml` ändern.

**Persönliche Links:** Bestätigungslinks verwenden `/r/` mit einem zufälligen, URL-sicheren Code aus 22 Zeichen (128 Bit Zufall). Sie enthalten keine Empfängerdaten und bleiben bei erneutem Export unverändert. Migration `0004` ersetzt vorhandene lokale Links durch kurze Codes; die alten Links werden ungültig. Empfänger, Bestätigungszeitpunkte und Formularantworten bleiben erhalten. Neu exportierte Links können anschließend wie gewohnt an Listmonk übertragen werden.

**Listmonk:** Unter **Admin-Einstellungen** Instanz-URL, API-Benutzername und API-Token eintragen, Verbindung aktivieren und testen. Der API-Benutzer benötigt Leserechte für Listen und Subscribers sowie für Exporte `subscribers:manage` und Zugriff auf die gewählte Liste. Die Instanz muss Subscriber-Updates per `PATCH` unterstützen.

**Branding:** Unter **Admin-Einstellungen → Globales Branding** ein Logo hochladen und die Akzentfarbe festlegen. Logos unterstützen PNG, JPEG, WebP und SVG bis 2 MiB; Rasterbilder dürfen höchstens 4096 × 4096 Pixel groß sein. Logos werden mit der Datenbank gesichert. Jede Campaign übernimmt standardmäßig das globale Branding oder verwendet unter **Einstellungen → Branding** eine vollständig eigene Kombination. Ohne eigenes Logo bleibt die Seite im eigenen Branding-Modus ohne Logo. Beim Wechsel zurück zum globalen Branding bleiben die eigenen Einstellungen erhalten. Der Campaign-Name ist standardmäßig sichtbar und kann pro Campaign ausgeblendet werden. Branding und Namensanzeige bleiben nach eingegangenen Bestätigungen bearbeitbar.

**Darstellung:** Verwaltung, Vorschau und Bestätigungsseiten verwenden automatisch den hellen oder dunklen Modus des Client-Systems und reagieren auf Änderungen der Systemeinstellung. Branding-Farben bleiben erhalten; Link- und Fokusfarben werden für den jeweiligen Hintergrund lesbar angepasst.

**Bestätigungstexte:** Unter **Campaign-Einstellungen → Bestätigung** lässt sich die Überschrift unter dem grünen Haken ändern (Standard: „Bestätigung erfolgreich“). Überschrift und Bestätigungstext bleiben nach der ersten Bestätigung bearbeitbar. Die Überschrift ist Klartext; im Bestätigungstext sind Empfänger-Platzhalter möglich:

```text
Vielen Dank, {{ name | default("Teilnehmer") }}!
Ihre Organisation: {{ attributes.firma | default("Ihre Organisation") }}
Kundennummer: {{ source.kundennummer }}
```

Verfügbar sind `name`, `email`, `identity`, `attributes` (Custom-Attribute aus CSV oder Listmonk) und `source` (importierte CSV-/Listmonk-Daten). Verschachtelte Werte lassen sich beispielsweise mit `{{ attributes.kontakte[0].name }}` lesen; Schlüssel mit Sonderzeichen über `{{ attributes["firma-name"] }}` oder `{{ source["Customer ID"] }}`. Fehlende, leere und `null`-Werte verwenden den angegebenen Ersatztext oder bleiben leer. `0` und `false` bleiben erhalten. Bedingungen, Schleifen, Funktionsaufrufe und andere Filter werden beim Speichern abgelehnt. Die Ausgabe ist Klartext; HTML wird nicht ausgeführt.

In der Campaign-Vorschau können Sie unter **Personalisierte Vorschau** nach einem Empfänger suchen und ihn auswählen. Die Vorschau speichert weder Bestätigungen noch Formularabgaben und erzeugt keine Links. Ohne Auswahl greifen Ersatzwerte oder leere Stellen. Die Option **Bestätigungstext bereits über dem offenen Formular anzeigen** steuert weiterhin die vorzeitige Anzeige von Text und Haken.
