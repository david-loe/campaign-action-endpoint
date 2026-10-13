import csv
import io
import json
import re
import secrets
from datetime import date, timezone
from decimal import Decimal, InvalidOperation

from email_validator import EmailNotValidError, validate_email
from flask import current_app
from sqlalchemy import text

from .models import Campaign, Recipient, db, utcnow

FIELD_TYPES = {"text", "email", "number", "date"}
LISTMONK_COLUMNS = ["uuid", "email", "name", "attributes", "status", "created_at", "updated_at"]


def begin_write():
    # SQLite has no SELECT FOR UPDATE. Serialize changes before reading state.
    db.session.execute(text("BEGIN IMMEDIATE"))


def iso_date(value):
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalized_email(value):
    return validate_email(value.strip(), check_deliverability=False).normalized.lower()


def confirmation_link(recipient):
    if not recipient.token:
        for _ in range(5):
            token = secrets.token_urlsafe(16)
            if db.session.scalar(db.select(Recipient.id).where(Recipient.token == token).limit(1)) is None:
                recipient.token = token
                break
        else:
            raise RuntimeError("Ein eindeutiger Bestätigungslink konnte nicht erzeugt werden.")
    return current_app.config["PUBLIC_BASE_URL"] + "/r/" + recipient.token


def token_recipient(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{22}", token):
        return None
    return db.session.scalar(db.select(Recipient).join(Campaign).where(
        Recipient.token == token, Campaign.active.is_(True),
    ))


def confirm(recipient):
    if recipient.confirmed_at is None:
        recipient.confirmed_at = utcnow()
    if recipient.campaign.locked_at is None:
        recipient.campaign.locked_at = utcnow()


def validate_answers(fields, form):
    answers, errors, values = {}, {}, {}
    for field in fields:
        key = f"field_{field.id}"
        value = form.get(key, "").strip()
        values[key] = value
        if not value:
            if field.required:
                errors[key] = "Bitte füllen Sie dieses Pflichtfeld aus."
            answers[str(field.id)] = ""
            continue
        if len(value) > 10000:
            errors[key] = "Bitte verwenden Sie höchstens 10.000 Zeichen."
            continue
        try:
            if field.field_type == "email":
                value = normalized_email(value)
            elif field.field_type == "number":
                if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
                    raise ValueError()
                number = Decimal(value)
                if not number.is_finite():
                    raise ValueError()
            elif field.field_type == "date":
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError()
        except (EmailNotValidError, InvalidOperation, ValueError):
            errors[key] = {"email": "Bitte geben Sie eine gültige E-Mail-Adresse ein.", "number": "Bitte geben Sie eine gültige Zahl ein.", "date": "Bitte geben Sie ein gültiges Datum ein."}[field.field_type]
        answers[str(field.id)] = value
    return answers, errors, values


def parse_csv(raw, delimiter, identity_type, identity_column):
    rows, errors = [], []
    if delimiter not in (",", ";"):
        return [], ["Ungültiges Trennzeichen."]
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError:
        return [], ["Bitte verwenden Sie eine UTF-8-kodierte CSV-Datei."]
    csv.field_size_limit(2 * 1024 * 1024)
    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=delimiter, strict=True)
        headers = reader.fieldnames
        if not headers or any(not h or not h.strip() for h in headers) or len(headers) != len(set(headers)):
            return [], ["Die CSV benötigt eindeutige, nicht leere Spaltenüberschriften."]
        identity_column = "email" if identity_type == "email" else identity_column
        if identity_column not in headers:
            return [], [f"Die Identitätsspalte „{identity_column}“ fehlt."]
        for index, source in enumerate(reader, start=1):
            line = reader.line_num
            if index > 100000:
                errors.append("Es sind höchstens 100.000 Empfänger je Import erlaubt.")
                break
            if None in source or any(v is None for v in source.values()):
                errors.append(f"Zeile {line}: Die Anzahl der Werte passt nicht zu den Überschriften.")
                continue
            try:
                identity = source[identity_column].strip()
                if not identity or len(identity) > 255:
                    raise ValueError("Die Identität muss 1 bis 255 Zeichen enthalten.")
                email = normalized_email(source["email"]) if source.get("email", "").strip() else None
                if identity_type == "email":
                    identity = normalized_email(identity)
                    email = identity
                name = source.get("name", "").strip()
                if len(name) > 200:
                    raise ValueError("Der Name darf höchstens 200 Zeichen enthalten.")
                attributes = json.loads(source.get("attributes", "").strip() or "{}")
                if not isinstance(attributes, dict):
                    raise ValueError("„attributes“ muss ein JSON-Objekt sein.")
                link_map = attributes.get("campaign_action_links", {})
                if not isinstance(link_map, dict):
                    raise ValueError("„campaign_action_links“ muss ein JSON-Objekt sein.")
                rows.append({"line": line, "identity": identity, "email": email, "name": name, "source": source, "attributes": attributes})
            except (EmailNotValidError, ValueError) as error:
                errors.append(f"Zeile {line}: {error}")
    except csv.Error as error:
        errors.append(f"Ungültige CSV-Datei: {error}")
    if not rows and not errors:
        errors.append("Die CSV-Datei enthält keine Empfänger.")
    return rows, errors


def annotate_import(rows, campaign_id):
    seen = set(db.session.scalars(db.select(Recipient.identity).where(Recipient.campaign_id == campaign_id)))
    result = []
    for row in rows:
        duplicate = row["identity"] in seen
        seen.add(row["identity"])
        result.append({**row, "duplicate": duplicate})
    return result


def listmonk_values(recipient):
    attributes = dict(recipient.attributes)
    links = dict(attributes.get("campaign_action_links", {}))
    links[recipient.campaign.uuid] = confirmation_link(recipient)
    attributes["campaign_action_links"] = links
    return [recipient.source.get("uuid", ""), recipient.email, recipient.name,
            json.dumps(attributes, ensure_ascii=False, separators=(",", ":")),
            recipient.source.get("status") or "enabled",
            recipient.source.get("created_at") or iso_date(recipient.created_at),
            recipient.source.get("updated_at") or iso_date(recipient.created_at)]


def csv_bytes(header, rows, spreadsheet_safe=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    def cell(value):
        value = "" if value is None else str(value)
        # Only reports consumed by spreadsheets are escaped. Machine-import CSV
        # keeps exact values so listmonk/identity round trips remain lossless.
        if spreadsheet_safe and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    writer.writerow([cell(value) for value in header])
    for row in rows:
        writer.writerow([cell(value) for value in row])
    return output.getvalue().encode("utf-8-sig")
