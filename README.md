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
| `ENCYPTION_SECRET` | Erforderlich, mindestens 32 Zeichen. Schützt Sessions, Campaign-Schlüssel und Listmonk-Zugangsdaten; dauerhaft unverändert lassen. |
| `SALT` | Optionaler Salt für neue Campaign-Schlüssel. Darf leer bleiben; Änderungen betreffen nur neue Campaigns. |
| `ADMIN_USERNAME` | Admin-Benutzername, standardmäßig `admin`. |
| `ADMIN_PASSWORD` | Erforderlich, mindestens 12 Zeichen. |
| `PUBLIC_BASE_URL` | Öffentlicher HTTP(S)-Origin ohne Unterpfad, standardmäßig `http://localhost:8080`. |
| `PORT` | Docker-Port auf dem Host, standardmäßig `8080`. |
| `DATABASE_PATH` | Bei direktem Python-Start konfigurierbar; Docker verwendet `/data/app.sqlite3`. |

Für öffentlichen Betrieb einen HTTPS-Reverse-Proxy verwenden und `PUBLIC_BASE_URL` entsprechend setzen. HTTPS aktiviert sichere Session-Cookies.

Compose nutzt `ghcr.io/david-loe/campaign-action-endpoint:main`. Für eine feste Version den Image-Tag direkt in `compose.yaml` ändern.

**Listmonk:** Unter **Admin-Einstellungen** Instanz-URL, API-Benutzername und API-Token eintragen, Verbindung aktivieren und testen. Der API-Benutzer benötigt Leserechte für Listen und Subscribers sowie für Exporte `subscribers:manage` und Zugriff auf die gewählte Liste. Die Instanz muss Subscriber-Updates per `PATCH` unterstützen.
