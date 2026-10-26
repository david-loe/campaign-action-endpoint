"""Listmonk HTTP client. Errors intentionally never include URLs or credentials."""
import json
import time
from urllib.parse import urlsplit

import httpx

from .services import parse_csv, csv_bytes

PAGE_SIZE = 100
MAX_SUBSCRIBERS = 100000


class ListmonkError(Exception):
    pass


def validate_base_url(value):
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise ValueError("Bitte geben Sie eine gültige Listmonk-URL ein.") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or any(character.isspace() for character in value)
            or parts.query or parts.fragment or len(value) > 2000):
        raise ValueError("Die Listmonk-URL benötigt HTTP(S) und darf keine Zugangsdaten, Query oder Fragment enthalten.")
    return value


class ListmonkClient:
    def __init__(self, base_url, username, token):
        self.client = httpx.Client(base_url=base_url + "/", auth=(username, token), timeout=10,
                                   follow_redirects=False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, path, **kwargs):
        deadline = time.monotonic() + 10
        try:
            # A response cap also prevents a misconfigured endpoint from filling
            # memory with an HTML/download response instead of Listmonk JSON.
            with self.client.stream(method, path, **kwargs) as response:
                if time.monotonic() > deadline:
                    raise ListmonkError("Zeitüberschreitung bei der Verbindung mit Listmonk.")
                if response.status_code != 200:
                    messages = {401: "Listmonk-Zugangsdaten wurden abgelehnt.", 403: "Für diese Aktion fehlen Listmonk-Berechtigungen.",
                                404: "Die Listmonk-Liste oder der Subscriber wurde nicht gefunden.",
                                405: "Diese Listmonk-Version unterstützt die benötigte API-Aktion nicht.",
                                429: "Listmonk begrenzt gerade die Anfragen. Bitte versuchen Sie es später erneut."}
                    raise ListmonkError(messages.get(response.status_code, f"Listmonk-Anfrage fehlgeschlagen (HTTP {response.status_code})."))
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ListmonkError("Zeitüberschreitung bei der Verbindung mit Listmonk.")
                    body.extend(chunk)
                    if len(body) > 10 * 1024 * 1024:
                        raise ListmonkError("Die Listmonk-Antwort überschreitet 10 MiB.")
                result = json.loads(body)
                if not isinstance(result, dict) or "data" not in result:
                    raise ValueError()
                return result["data"]
        except httpx.TimeoutException:
            raise ListmonkError("Zeitüberschreitung bei der Verbindung mit Listmonk.") from None
        except httpx.HTTPError:
            raise ListmonkError("Die Verbindung mit Listmonk ist fehlgeschlagen. Bitte prüfen Sie URL und Erreichbarkeit.") from None
        except (ValueError, UnicodeError):
            raise ListmonkError("Listmonk hat eine ungültige API-Antwort zurückgegeben.") from None

    def page(self, path, **params):
        data = self.request("GET", path, params=params)
        if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
                or not isinstance(data.get("total"), int) or data["total"] < 0
                or not all(isinstance(row, dict) for row in data["results"])):
            raise ListmonkError("Listmonk hat ungültige paginierte Daten zurückgegeben.")
        return data["results"], data["total"]

    def lists(self):
        lists = []
        for page in range(1, 101):
            # minimal=true returns a plain array instead of the paginated
            # results/total envelope used by this client.
            rows, total = self.page("api/lists", status="active", page=page, per_page=PAGE_SIZE)
            lists.extend(row for row in rows if row.get("status", "active") == "active")
            if page * PAGE_SIZE >= total or not rows:
                return lists
        raise ListmonkError("Die Listmonk-Instanz liefert zu viele Listen.")

    def active_list(self, list_id):
        data = self.request("GET", f"api/lists/{list_id}")
        if not isinstance(data, dict) or data.get("id") != list_id or data.get("status", "active") != "active":
            raise ListmonkError("Die ausgewählte Listmonk-Liste ist nicht aktiv oder nicht zugänglich.")
        return data

    def subscribers(self, list_id, page):
        self.active_list(list_id)
        # Bulk imports can share created_at. Unique e-mail addresses keep page
        # boundaries stable instead of returning the same IDs on several pages.
        rows, total = self.page("api/subscribers", list_id=list_id, page=page, per_page=PAGE_SIZE, order_by="email", order="ASC")
        if total > MAX_SUBSCRIBERS or len(rows) > PAGE_SIZE:
            raise ListmonkError("Es sind höchstens 100.000 Subscribers je Transfer erlaubt.")
        if any(not isinstance(row.get("id"), int) or isinstance(row["id"], bool) or row["id"] <= 0 for row in rows):
            raise ListmonkError("Listmonk hat ungültige Subscriber-IDs zurückgegeben.")
        return rows, total

    def subscriber(self, subscriber_id):
        data = self.request("GET", f"api/subscribers/{subscriber_id}")
        if not isinstance(data, dict) or data.get("id") != subscriber_id:
            raise ListmonkError("Listmonk hat ungültige Subscriber-Daten zurückgegeben.")
        return data

    def patch_links(self, subscriber_id, links):
        data = self.request("PATCH", f"api/subscribers/{subscriber_id}", json={"attribs": {"campaign_action_links": links}})
        if not isinstance(data, dict) or data.get("id") != subscriber_id:
            raise ListmonkError("Listmonk hat das Subscriber-Update nicht gültig bestätigt.")
        attributes = data.get("attribs", {})
        stored = attributes.get("campaign_action_links", {}) if isinstance(attributes, dict) else {}
        if not isinstance(stored, dict) or any(stored.get(key) != value for key, value in links.items()):
            raise ListmonkError("Listmonk hat das Link-Attribut nicht bestätigt. Bitte wiederholen Sie den Vorgang.")
        return data


def subscriber_row(subscriber, identity_type, key_source, key_attribute):
    """Use the CSV validation contract for both import paths, without divergence."""
    source = {key: str(subscriber.get(key) or "") for key in ("uuid", "email", "name", "status", "created_at", "updated_at")}
    attributes = subscriber.get("attribs", {})
    source["attributes"] = json.dumps(attributes, ensure_ascii=False)
    source["listmonk_id"] = str(subscriber["id"])
    source["listmonk_lists"] = json.dumps(subscriber.get("lists", []), ensure_ascii=False)
    key = subscriber.get(key_source) if key_source in ("uuid", "id") else attributes
    if key_source == "attribute":
        for segment in key_attribute.split("."):
            key = key.get(segment) if isinstance(key, dict) else None
    if identity_type == "key":
        if not isinstance(key, (str, int)) or isinstance(key, bool):
            return None, "Der ausgewählte Key fehlt oder ist kein Text/Integer."
        source["key"] = str(key)
    encoded = csv_bytes(list(source), [list(source.values())])
    rows, errors = parse_csv(encoded, ",", identity_type, "key")
    if errors:
        return None, " ".join(errors).replace("Zeile 2:", f"Subscriber {subscriber['id']}:")
    rows[0]["line"] = subscriber["id"]
    return rows[0], ""
