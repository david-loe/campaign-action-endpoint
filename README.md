# Campaign Action Endpoint

Eine kleine, selbst gehostete Web-App für Campaign-Bestätigungen und optionale Formulare. Python 3.13, Flask, SQLite und Docker; deutsche, responsive Oberfläche ohne Frontend-Build.

## Schnellstart mit Docker

```bash
cp .env.example .env
openssl rand -hex 32
```

Den ausgegebenen Wert als `ENCYPTION_SECRET` in `.env` eintragen. Außerdem `ADMIN_PASSWORD` setzen (mindestens 12 Zeichen). `SALT` darf leer bleiben. Danach lokal bauen und starten:

```bash
docker compose up -d --build
```

Die Verwaltung ist unter **http://localhost:8080/admin/campaigns** erreichbar. Benutzername: der Wert von `ADMIN_USERNAME` (standardmäßig `admin`). SQLite liegt im persistenten Volume `campaign-data`; Migrationen laufen vor jedem Serverstart.

`ENCYPTION_SECRET` einmal erzeugen und dauerhaft zusammen mit der Datenbank sichern. Es schützt Sessions, gespeicherte Campaign-Schlüssel und den Listmonk-API-Token. Eine Änderung beendet Sitzungen und verhindert das Entschlüsseln der bisherigen Schlüssel und Zugangsdaten. Eine falsche Einstellung wird beim Start abgewiesen. `.env` ist vom Git-Repository und Docker-Build ausgeschlossen.

Jede neue oder duplizierte Campaign erhält automatisch 256 Bit Schlüsselmaterial aus `secrets.token_bytes(32)`, der kryptografischen Zufallsquelle des Betriebssystems. Es wird weder ein manuell gesetzter Seed noch das Python-Modul `random` verwendet. HKDF-SHA256 leitet daraus einen eigenen Fernet-Schlüssel ab; `SALT` ist ein optionaler zusätzlicher Parameter, kein Ersatz für Zufall. Der fertige Campaign-Schlüssel wird unter einem separat aus `ENCYPTION_SECRET` abgeleiteten Speicherschlüssel verschlüsselt in SQLite abgelegt. Änderungen an `SALT` betreffen nur künftig angelegte Campaigns. Bei Bedarf einen Salt ebenfalls mit `openssl rand -hex 32` erzeugen.

Beim Update vorhandener Installationen den bisherigen Sitzungsschlüssel unter dem neuen Namen `ENCYPTION_SECRET` konfigurieren. Das globale Link-Geheimnis entfällt. Die Migration erzeugt eigene Campaign-Schlüssel; bereits gespeicherte persönliche Links bleiben durch exakte Token-Zuordnung gültig. Neue Links werden mit dem jeweiligen Campaign-Schlüssel verschlüsselt. Ein Fehler der Zufallsquelle bricht das Anlegen bzw. die Migration ab.

## Campaign-Ablauf

1. Campaign erstellen: Namen, E-Mail oder eigenen Key, Bestätigungsmodus und Texte festlegen.
2. Optional ein Formular aktivieren. Felder hinzufügen, entfernen und sortieren; Typ, Pflichtfeld und Beschreibung einstellen.
3. Empfänger-CSV hochladen oder eine Liste aus der konfigurierten Listmonk-Instanz auswählen. Die Vorschau zeigt neue Einträge, Dubletten und Fehler. Erst die Übernahme schreibt Empfänger.
4. Persönliche Links als `email,link` / `key,link` oder als Listmonk-Subscriber-CSV exportieren oder direkt in die Attribute vorhandener Listmonk-Subscribers übertragen. Über das bestehende Campaign-System verschicken.
5. Bestätigungen und Formularabgaben in der Verwaltung verfolgen und Ergebnisse als CSV exportieren.

**Beim Linkaufruf:** Der erste gültige GET speichert die Bestätigung sofort. Ein aktiviertes Formular wird separat ausgefüllt. Dieser Modus zählt auch automatische GET-Abrufe, etwa durch E-Mail-Linkprüfungen; es gibt keine zuverlässige Bot-Erkennung.

**Bewusste Bestätigung:** Ohne Formular bestätigt ein Button. Mit Formular bestätigt erst die erste gültige Abgabe. Dieser Modus ist der Standard.

Das erste Bestätigungsdatum bleibt erhalten. Formularabgaben sind final und werden bei erneuten Aufrufen oder parallelen Abgaben nicht überschrieben. Pflichtfelder und alle Feldtypen werden serverseitig geprüft. `number` akzeptiert Dezimalzahlen mit Punkt und Exponentialnotation, `date` erwartet `YYYY-MM-DD`.

Die Einstellung „Bestätigungstext bereits über dem offenen Formular anzeigen“ betrifft nur die Darstellung. Sie verschiebt den Bestätigungszeitpunkt nicht. Nach erfolgreicher Abgabe erscheint der Text immer. Die Admin-Vorschau und HEAD-Anfragen speichern keine Bestätigung.

Bestätigungsmodus und Formularstruktur dürfen bis zur ersten Bestätigung **oder** Abgabe geändert werden, auch wenn Links bereits verschickt sind. Anschließend sind Aktivierung des Formulars, Typen, Pflichtfelder, Reihenfolge und Feldbestand gesperrt. Texte und Feldbeschreibungen bleiben bearbeitbar. Für einen neuen Ablauf kann die Campaign dupliziert werden; die Kopie enthält Einstellungen, aber keine Empfänger oder Ergebnisse. Die Identifikationsart ist nach Aufnahme von Empfängern festgeschrieben.

Links sind mit Fernet authentifiziert verschlüsselt und an Campaign sowie Empfänger gebunden. Sie enthalten keinen lesbaren Identifikator. Es gibt kein zeitliches Ablaufdatum. Deaktivieren einer Campaign sperrt alle ihre Links; Reaktivieren macht sie wieder erreichbar. Löschen einer Campaign oder eines Empfängers entfernt die zugehörigen Ergebnisse und macht dessen Links ungültig. Wer einen persönlichen Link besitzt, kann die damit verbundene Aktion ausführen: Links nicht öffentlich teilen.

## CSV und Listmonk

### Direkte Listmonk-API

Unter **Admin-Einstellungen → Listmonk** eine Instanz mit Basis-URL, API-Benutzername und API-Token konfigurieren, aktivieren und die Verbindung testen. Die Basis-URL kann einen Unterpfad enthalten; `/api` wird von der App ergänzt. HTTP ist für interne Docker-Adressen möglich, HTTPS wird mit Zertifikatsprüfung verwendet. Redirects werden nicht verfolgt. Der API-Token wird verschlüsselt gespeichert und nie erneut angezeigt. Leeres Token-Feld behält ihn; bei Änderung von URL oder Benutzername muss ein neuer Token eingegeben werden.

Benötigt werden Leserechte für Listen und Subscribers sowie für Exporte `subscribers:manage` und Zugriff auf die betreffende Liste. Die Listmonk-Version muss `PATCH /api/subscribers/{id}` unterstützen. Der Verbindungstest prüft lesenden Zugriff auf die Listen, ohne Subscriber zu ändern. Sobald die Verbindung aktiviert ist, zeigt jede Campaign neben den CSV-Aktionen **Aus Listmonk importieren** und **Links an Listmonk übertragen**.

- **Import:** Aktive, zugängliche Subscriber-Liste auswählen. E-Mail-Campaigns verwenden die E-Mail-Adresse; Key-Campaigns wahlweise UUID, ID oder ein Subscriber-Attribut (auch verschachtelt, z. B. `customer.id`). Alle Statuswerte der Liste werden eingelesen. Die gemeinsame CSV-/API-Validierung und Dublettenbehandlung greifen vor der atomaren Übernahme in die Campaign. Bestehende Bestätigungen und Antworten bleiben erhalten.
- **Export:** Die Vorschau gleicht Campaign-Empfänger anhand ihrer normalisierten E-Mail-Adressen mit der ausgewählten Liste ab. Nur bereits vorhandene Subscribers erhalten den persönlichen Link in `attribs.campaign_action_links[Campaign-UUID]`. Fehlende Empfänger oder fehlende E-Mails werden übersprungen und ausgewiesen; doppelte Campaign-E-Mail-Adressen verhindern den Start. Vor jedem Update werden aktuelle Attribute, Identität und Listenmitgliedschaft erneut gelesen. Es wird ausschließlich `attribs` per PATCH gesendet; Status und Mitgliedschaften bleiben erhalten, es werden keine Subscriber angelegt und keine Opt-in-Aktionen ausgelöst.

Die Verarbeitung läuft in Browser-Chargen: pro Import-Schritt eine Seite mit bis zu 100 Subscribers, pro Export-Schritt bis zu drei Updates. Fortschritt und Einzelergebnisse werden in SQLite gespeichert. **Pausieren** wirkt nach der laufenden Charge; beim Schließen des Browsers endet die Verarbeitung spätestens nach dieser Charge. Vorgänge lassen sich über die Campaign-Seite nach erneutem Login fortsetzen. Ohne JavaScript verarbeitet jeder Klick auf **Verarbeitung fortsetzen** eine Charge.

Die App serialisiert ihre Transfers mit einer zeitlich begrenzten Datenbanksperre, ohne während API-Anfragen SQLite-Schreibtransaktionen zu halten. Wiederholungen überspringen erfolgreich übertragene Einträge und verwenden dieselben Links; teilweise erfolgreiche Exporte werden nicht als vollständig erfolgreich ausgegeben. Fehlgeschlagene Schritte lassen sich erneut versuchen. Änderungen bzw. Entfernen der Listmonk-Konfiguration stoppen ältere Aufträge. Eine laufende Charge muss vor einer Konfigurationsänderung oder dem Löschen ihrer Campaign/Empfänger beendet werden. Exporte berücksichtigen die Campaign-Empfänger zum Zeitpunkt ihrer Vorschau.

### CSV-Dateien

UTF-8 mit oder ohne BOM, Komma oder Semikolon, erste Zeile als Überschrift. Grenzen: 10 MiB Upload, 100.000 Datensätze pro Import, 255 Zeichen pro Identität und 50 Formularfelder pro Campaign. Namen dürfen 200 Zeichen lang sein. Eine fehlerhafte CSV wird vollständig zurückgewiesen; bestehende Empfänger und Dubletten werden übersprungen. E-Mail-Identitäten werden getrimmt und kleingeschrieben, eigene Keys getrimmt und unter Beachtung der Groß-/Kleinschreibung verglichen.

Minimal:

```csv
email,name
anna@example.com,Anna
ben@example.com,Ben
```

Eigener Key, beim Import z. B. `customer_id` auswählen:

```csv
customer_id,email,name
customer-101,anna@example.com,Anna
customer-102,,Ben
```

Listmonk-Subscriber-Exporte verwenden `uuid,email,name,attributes,status,created_at,updated_at`. `attributes` ist ein JSON-Objekt; vorhandene Attribute und weitere importierte Spalten werden gespeichert. Listmonk-CSV-Exporte benötigen bei allen Empfängern eine gültige, innerhalb der Campaign eindeutige E-Mail-Adresse. Die App exportiert Links auch für importierte Subscriber mit dem Status `blocklisted` und erhält den Status; die Versandentscheidung trifft Listmonk.

Der persönliche Link wird in der exportierten Spalte `attributes` ergänzt:

```json
{
  "city": "Berlin",
  "campaign_action_links": {
    "<campaign-uuid>": "https://confirm.example.com/r/<encrypted-token>"
  }
}
```

Für Listmonk die exportierte Datei mit aktivierter Überschreiboption importieren. Der Verwaltungsschirm zeigt den genauen Template-Ausdruck für jede Campaign:

```gotemplate
<a href="{{ index .Subscriber.Attribs.campaign_action_links "<campaign-uuid>" }}">Jetzt bestätigen</a>
```

Subscriber-Attribute gehören in Listmonk zum Subscriber und werden bei Importen möglicherweise ersetzt. Für weitere Campaigns deshalb einen **aktuellen** Listmonk-Subscriber-Export als Ausgangspunkt verwenden, damit die bereits vorhandenen Campaign-Links mitgeführt werden. Die CSV-Integration synchronisiert nichts automatisch und verändert keine Listmonk-Listenmitgliedschaften.

Der Ergebnisexport enthält alle Empfänger, einschließlich noch offener Einträge: Identität, E-Mail (bei Key-Campaigns), Name, Subscriber-Metadaten, `confirmed_at`, `submitted_at`, zusätzliche Quellspalten mit Präfix `source_` und Formulardaten. Antwortspalten heißen `field_<id>_<bezeichnung>`, sodass gleiche Feldbezeichnungen unterscheidbar bleiben. Zeitstempel sind UTC / ISO 8601, z. B. `2026-10-13T12:30:00Z`. Fehlende Werte sind leer. Ergebnis-CSVs entschärfen Zellen mit möglichen Tabellenkalkulationsformeln mit einem führenden Apostroph; Link-/Listmonk-CSVs erhalten exakte Werte für den maschinellen Import.

Referenzen: [Listmonk-Listen](https://listmonk.app/docs/apis/lists/), [Subscriber-API](https://listmonk.app/docs/apis/subscribers/), [Listmonk-Import](https://listmonk.app/docs/apis/import/), [Subscriber-Export im Listmonk-Quellcode](https://github.com/knadh/listmonk/blob/master/cmd/subscribers.go), [Listmonk-Templates](https://listmonk.app/docs/templating/), [Fernet](https://cryptography.io/en/stable/fernet/), [Python secrets](https://docs.python.org/3/library/secrets.html), [OpenSSL rand](https://docs.openssl.org/master/man1/openssl-rand/).

## Konfiguration und Betrieb

| Variable | Bedeutung |
| --- | --- |
| `ENCYPTION_SECRET` | Pflichtwert für Sessions und verschlüsselte Speicherung, mindestens 32 Zeichen; `openssl rand -hex 32` |
| `SALT` | Optionaler HKDF-Parameter für neu erzeugte Campaign-Schlüssel; standardmäßig leer |
| `ADMIN_USERNAME` | Ein Admin-Konto; Compose-Standard `admin` |
| `ADMIN_PASSWORD` | Mindestens 12 Zeichen; beim Start für Passwortprüfung gehasht |
| `PUBLIC_BASE_URL` | Öffentlicher HTTP(S)-Origin ohne Unterpfad, Query oder Zugangsdaten |
| `DATABASE_PATH` | Lokal standardmäßig `data/app.sqlite3`; Compose verwendet `/data/app.sqlite3` |
| `PORT` | Compose-Port auf dem Host, standardmäßig `8080` |

Für den öffentlichen Betrieb `PUBLIC_BASE_URL=https://confirm.example.com` setzen und einen HTTPS-Reverse-Proxy vorschalten. Die App wird am Domain-Root betrieben. HTTPS als öffentliche URL aktiviert sichere Session-Cookies; über unverschlüsseltes HTTP ist dann kein Admin-Login möglich. Alle generierten Links verwenden `PUBLIC_BASE_URL`, nicht den Host-Header einer Anfrage.

Gunicorn startet mit zwei Workern und je vier Threads. SQLite verwendet WAL, Fremdschlüssel und 15 Sekunden Busy-Timeout. Kritische Änderungen werden mit `BEGIN IMMEDIATE` serialisiert. Eine App-Instanz auf einem Host mit lokalem Volume betreiben. Sitzungen laufen nach acht Stunden ab. Fünf fehlgeschlagene Loginversuche pro direkter Client-IP sperren weitere Versuche bis zum Ablauf des 15-Minuten-Fensters. Hinter einem Reverse-Proxy gilt dessen IP; Forwarded-Header werden nicht ungeprüft vertraut.

Der Container läuft als UID 10001. Bei einem eigenen Bind-Mount muss diese UID schreiben dürfen. `/healthz` prüft App und Datenbank. Gunicorn-Access-Logs sind nicht aktiviert; auch im Reverse-Proxy personenbezogene Bestätigungs-URLs von Zugriffslogs ausnehmen.

## GitHub Actions / GHCR

Nach Push des Repositorys zu GitHub:

- Pull Requests: Tests sowie Docker-Build und Container-Smoke-Test, keine Veröffentlichung.
- Push auf `main`: Tests, Containerprüfung, Veröffentlichung als `ghcr.io/david-loe/campaign-action-endpoint:main` und `:sha-<commit>`.
- Jeder Git-Tag: eigenes Image-Tag plus Commit-Tag. Semver-Tags erhalten zusätzlich Versionsaliase. Stabile Tags wie `v1.2.3` setzen außerdem `:latest`; Vorabversionen und andere Tags ändern `latest` nicht.

Der Workflow veröffentlicht für `linux/amd64` und `linux/arm64`. Er verwendet `GITHUB_TOKEN` mit `packages: write`, offizielle Docker-Actions und festgeschriebene Action-Commit-SHAs. Weitere Registry-Secrets sind nicht erforderlich. Falls die GitHub-Organisation die Veröffentlichung von Packages einschränkt, dort die Berechtigung freigeben. Die Sichtbarkeit des GHCR-Packages lässt sich in GitHub einstellen; der Workflow stellt Packages nicht automatisch auf öffentlich.

Compose verwendet fest `ghcr.io/david-loe/campaign-action-endpoint:main`. Das veröffentlichte Image verwenden:

```bash
docker compose pull
docker compose up -d --no-build
```

Der GitHub-Workflow veröffentlicht Images; ein laufender Server wird dadurch nicht automatisch aktualisiert. Für Updates zuerst ein Backup erstellen, danach das neue Image ziehen und den Container ersetzen. Migrationen laufen beim Start. Ein Downgrade des Images ist nach einer Schemaänderung nur mit einem dazu passenden Datenbankbackup sicher.

Für eine feste Release-Version den Tag direkt in der `image:`-Zeile von `compose.yaml` setzen, z. B. `ghcr.io/david-loe/campaign-action-endpoint:1.2.3`.

## Backup und Wiederherstellung

Die SQLite-Backup-API erstellt konsistente Backups einschließlich bereits bestätigter WAL-Schreibvorgänge, auch während des Betriebs:

```bash
docker compose exec app python -m campaign_action backup /data/backup-2026-10-13.sqlite3
docker compose cp app:/data/backup-2026-10-13.sqlite3 ./backup-2026-10-13.sqlite3
```

Ein existierendes Backup wird nicht überschrieben. Backups enthalten Empfängerdaten, persönliche Tokens, Antworten, verschlüsselte Campaign-Schlüssel und gegebenenfalls Listmonk-Zugangsdaten. **`ENCYPTION_SECRET` separat und dauerhaft zum Backup sichern.** Ohne diesen Wert lassen sich die gespeicherten Schlüssel nicht entschlüsseln. Der bei Erstellung verwendete `SALT` wird für bestehende Schlüssel nicht erneut benötigt. Zum Wiederherstellen App stoppen, die Datenbank aus einem Backup ersetzen und etwaige alte `app.sqlite3-wal`-/`app.sqlite3-shm`-Dateien entfernen. Die Datei muss UID 10001 gehören. Danach die zum Backup passende App-Version starten. Ein laufendes SQLite-Volume nicht durch einfaches Kopieren einzelner Dateien sichern.

## Lokale Entwicklung und Tests

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
cp .env.example .env
openssl rand -hex 32
```

`.env` wie oben ausfüllen und lokal `DATABASE_PATH=data/app.sqlite3` setzen. Der lokale Entwicklungsserver lädt `.env` und migriert beim Start:

```bash
.venv/bin/python -m campaign_action run
.venv/bin/python -m pytest
.venv/bin/python scripts/docker_smoke.py --build --image campaign-action:test
```

Der Docker-Smoke-Test erzeugt eigene temporäre Container und ein Volume, führt Login, CSV-Import, Linkexport und Bestätigung über echtes HTTP aus und ersetzt danach den Container. Er prüft Datenerhalt, stabile Links und den Nicht-Root-Benutzer und entfernt seine Testressourcen anschließend.

Optionaler Browser-Test mit Chromium (eigener temporärer Server und eigene Datenbank):

```bash
.venv/bin/python -m pip install -e '.[browser]'
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/browser_smoke.py
```

Er prüft Feldeditor und Sortierung, Import, Downloads, mobile Formulare, finale Abgabe und Struktursperre sowie Listmonk-Einstellungen, API-Import und API-Export mit einer lokalen Test-API. Mit `--screenshots /tmp/campaign-action-preview` können Vorschau-Bilder gespeichert werden.

Produktive Abhängigkeiten sind in `requirements.txt` festgeschrieben, Testabhängigkeiten in `requirements-dev.txt`. Nach beabsichtigten Dependency-Updates beide mit `uv pip compile pyproject.toml -o requirements.txt` beziehungsweise `uv pip compile pyproject.toml --extra dev -o requirements-dev.txt` erneuern.

Die erste Version versendet keine E-Mails, besitzt keine zusätzliche Benutzerverwaltung und benötigt weder eine Listmonk-Verbindung noch einen externen Datenbankserver.
