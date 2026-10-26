"""Browser-driven transfer steps with a database lease and per-item checkpoints."""
import secrets
from datetime import timedelta, timezone

from sqlalchemy import func

from .crypto import decrypt_value
from .listmonk import ListmonkClient, ListmonkError, MAX_SUBSCRIBERS, PAGE_SIZE, subscriber_row
from .models import ListmonkConfig, Recipient, TransferItem, TransferJob, db, utcnow
from .services import begin_write, confirmation_link, normalized_email


class ListmonkPaginationError(ListmonkError):
    """An inconsistent result set requires a fresh transfer, not a page retry."""


def incomplete_load_message(loaded, total):
    if loaded != total:
        return f"Unvollständig geladen: {loaded} von {total} Subscribern. Bitte starten Sie einen neuen Vorgang."
    return ""


def active_lease(config):
    return bool(config and config.lease_owner and config.lease_until and config.lease_until.replace(tzinfo=timezone.utc) > utcnow())


def client_for(config):
    return ListmonkClient(config.base_url, config.username, decrypt_value(config.token))


def counts(job_id):
    return dict(db.session.execute(db.select(TransferItem.status, func.count()).where(TransferItem.job_id == job_id).group_by(TransferItem.status)).all())


def claim(job_id):
    begin_write()
    job = db.session.get(TransferJob, job_id)
    config = db.session.get(ListmonkConfig, 1)
    if not job:
        db.session.rollback()
        return None, {"status": "missing", "message": "Der Vorgang wurde entfernt."}
    if not config or not config.enabled or config.revision != job.config_revision:
        job.status, job.error = "stopped", "Die Listmonk-Konfiguration wurde geändert. Bitte starten Sie einen neuen Vorgang."
        db.session.commit()
        return None, {"status": "stopped", "message": job.error}
    if job.campaign.identity_type != job.identity_type:
        job.status, job.error = "stopped", "Die Identifikationsart der Campaign wurde geändert. Bitte starten Sie einen neuen Vorgang."
        db.session.commit()
        return None, {"status": "stopped", "message": job.error}
    if job.status not in ("loading", "running"):
        db.session.rollback()
        return None, {"status": job.status, "message": job.error}
    if active_lease(config):
        db.session.rollback()
        return None, {"status": "busy", "message": "Eine andere Charge wird gerade verarbeitet.", "retry_after": 3}
    owner = secrets.token_hex(24)
    config.lease_owner, config.lease_job_id, config.lease_until = owner, job.id, utcnow() + timedelta(seconds=120)
    snapshot = {key: getattr(job, key) for key in ("id", "direction", "status", "page", "list_id", "identity_type", "key_source", "key_attribute", "campaign_id")}
    snapshot.update(owner=owner, base_url=config.base_url, username=config.username, token=decrypt_value(config.token))
    if job.status == "running":
        tasks = db.session.scalars(db.select(TransferItem).where(TransferItem.job_id == job.id, TransferItem.status == "pending").order_by(TransferItem.id).limit(3)).all()
        snapshot["tasks"] = []
        for item in tasks:
            recipient = db.session.get(Recipient, item.recipient_id) if item.recipient_id else None
            snapshot["tasks"].append({"id": item.id, "subscriber_id": item.subscriber_id,
                                      "uuid": item.payload.get("uuid"), "email": item.payload.get("email"),
                                      "link": confirmation_link(recipient) if recipient else None})
        snapshot["campaign_uuid"] = job.campaign.uuid
    db.session.commit()
    # The snapshot contains everything required for remote I/O; no SQLite write
    # transaction is held while waiting for Listmonk.
    return snapshot, None


def owned_job(snapshot):
    config = db.session.get(ListmonkConfig, 1)
    job = db.session.get(TransferJob, snapshot["id"])
    if (not job or not config or config.lease_owner != snapshot["owner"]
            or config.revision != job.config_revision):
        raise ListmonkError("Die Charge ist nicht mehr gültig. Bitte laden Sie den Vorgang neu.")
    config.lease_until = utcnow() + timedelta(seconds=120)
    return job, config


def finish_loading(job):
    error = incomplete_load_message(sum(counts(job.id).values()), job.total)
    if error:
        raise ListmonkPaginationError(error)
    if job.direction == "import":
        job.status = "preview"
        return
    # E-mail identities in Listmonk are unique. Reject an ambiguous response
    # instead of choosing whichever record happened to arrive first.
    items = db.session.scalars(db.select(TransferItem).where(TransferItem.job_id == job.id)).all()
    by_email = {}
    for item in items:
        email = item.payload.get("email")
        if email:
            if email in by_email:
                raise ListmonkError("Die Listmonk-Liste enthält mehrdeutige E-Mail-Adressen.")
            by_email[email] = item
    recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == job.campaign_id).order_by(Recipient.id)).all()
    emails = [recipient.email for recipient in recipients if recipient.email]
    if len(emails) != len(set(emails)):
        raise ListmonkError("Für den Listmonk-Export müssen E-Mail-Adressen innerhalb der Campaign eindeutig sein.")
    used = set()
    for recipient in recipients:
        item = by_email.get(recipient.email) if recipient.email else None
        if item:
            used.add(item.id)
            item.recipient_id, item.status = recipient.id, "pending"
            item.payload = {**item.payload, "identity": recipient.identity}
        else:
            db.session.add(TransferItem(job_id=job.id, recipient_id=recipient.id,
                           payload={"identity": recipient.identity, "email": recipient.email}, status="skipped",
                           message="Keine E-Mail-Adresse." if not recipient.email else "Nicht in der ausgewählten Liste vorhanden."))
    for item in items:
        if item.id not in used:
            db.session.delete(item)
    job.total, job.status = len(recipients), "ready"


def load_page(snapshot, client):
    rows, total = client.subscribers(snapshot["list_id"], snapshot["page"])
    normalized = []
    for subscriber in rows:
        if snapshot["direction"] == "import":
            payload, error = subscriber_row(subscriber, snapshot["identity_type"], snapshot["key_source"], snapshot["key_attribute"])
            normalized.append((subscriber["id"], payload or {"identity": str(subscriber.get("email") or subscriber["id"]), "line": subscriber["id"]}, error))
        else:
            try:
                email = normalized_email(subscriber.get("email", ""))
            except (ValueError, AttributeError):
                raise ListmonkError("Die Listmonk-Liste enthält eine ungültige E-Mail-Adresse.") from None
            normalized.append((subscriber["id"], {"email": email, "uuid": subscriber.get("uuid")}, ""))
    begin_write()
    job, _ = owned_job(snapshot)
    if snapshot["page"] > 1 and total != job.total:
        raise ListmonkPaginationError(
            f"Die Subscriber-Anzahl hat sich während des Ladens geändert ({job.total} auf {total}). "
            "Bitte starten Sie einen neuen Vorgang.")
    existing = set(db.session.scalars(db.select(TransferItem.subscriber_id).where(TransferItem.job_id == job.id)))
    for subscriber_id, payload, error in normalized:
        if subscriber_id not in existing:
            existing.add(subscriber_id)
            db.session.add(TransferItem(job_id=job.id, subscriber_id=subscriber_id, payload=payload,
                                       status="invalid" if error else "staged", message=error))
    if len(existing) > MAX_SUBSCRIBERS:
        raise ListmonkError("Es sind höchstens 100.000 Subscribers je Transfer erlaubt.")
    job.total = total
    job.page += 1
    db.session.flush()
    if not rows or snapshot["page"] * PAGE_SIZE >= total:
        finish_loading(job)
    db.session.commit()


def export_tasks(snapshot, client):
    client.active_list(snapshot["list_id"])
    for task in snapshot["tasks"]:
        status, message = "done", "Link ergänzt."
        try:
            if not task["link"]:
                status, message = "skipped", "Der Campaign-Empfänger wurde entfernt."
            else:
                subscriber = client.subscriber(task["subscriber_id"])
                memberships = subscriber.get("lists", [])
                email = normalized_email(subscriber.get("email", ""))
                if (email != task["email"] or subscriber.get("uuid") != task["uuid"]
                        or not any(isinstance(row, dict) and row.get("id") == snapshot["list_id"] for row in memberships)):
                    status, message = "skipped", "Subscriber gehört nicht mehr zur ausgewählten Liste oder seine Identität wurde geändert."
                else:
                    attributes = subscriber.get("attribs", {})
                    links = attributes.get("campaign_action_links", {}) if isinstance(attributes, dict) else None
                    if not isinstance(links, dict):
                        raise ListmonkError("Das Subscriber-Attribut campaign_action_links ist kein JSON-Objekt.")
                    links = {**links, snapshot["campaign_uuid"]: task["link"]}
                    client.patch_links(task["subscriber_id"], links)
        except (ListmonkError, ValueError) as error:
            status, message = "error", "Das Subscriber-Update ist fehlgeschlagen."
            # Safe error messages from our client contain no remote response data.
            if isinstance(error, ListmonkError):
                message = str(error)
        begin_write()
        _, _ = owned_job(snapshot)
        item = db.session.get(TransferItem, task["id"])
        if item:
            item.status, item.message = status, message
        db.session.commit()
    begin_write()
    job, _ = owned_job(snapshot)
    summary = counts(job.id)
    if not summary.get("pending"):
        job.status = "partial" if summary.get("error") else "done"
    db.session.commit()


def process_step(job_id):
    snapshot, result = claim(job_id)
    if snapshot is None:
        return result
    try:
        with ListmonkClient(snapshot["base_url"], snapshot["username"], snapshot["token"]) as client:
            if snapshot["status"] == "loading":
                load_page(snapshot, client)
            else:
                export_tasks(snapshot, client)
    except ListmonkPaginationError as error:
        # Pagination checks run inside the owned loading transaction, before
        # export reconciliation. Keep the loaded rows and their count visible.
        job, _ = owned_job(snapshot)
        job.status, job.error = "stopped", str(error)
        db.session.commit()
    except ListmonkError as error:
        db.session.rollback()
        begin_write()
        job, _ = owned_job(snapshot)
        job.status, job.error = "failed", str(error)
        db.session.commit()
    finally:
        db.session.rollback()
        begin_write()
        config = db.session.get(ListmonkConfig, 1)
        if config and config.lease_owner == snapshot["owner"]:
            config.lease_owner = config.lease_job_id = config.lease_until = None
        db.session.commit()
    job = db.session.get(TransferJob, job_id)
    summary = counts(job.id)
    return {"status": job.status, "message": job.error, "counts": summary, "total": job.total,
            "processed": sum(summary.values()) if job.status == "loading" else sum(value for key, value in summary.items() if key in ("done", "skipped", "error"))}
