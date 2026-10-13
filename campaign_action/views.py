import hashlib
import hmac
import json
import secrets
from datetime import timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, text
from werkzeug.security import check_password_hash

from .models import Campaign, FormField, ImportJob, LoginAttempt, ListmonkConfig, Recipient, Submission, TransferJob, db, utcnow
from .services import (FIELD_TYPES, LISTMONK_COLUMNS, annotate_import, begin_write, confirm,
                       confirmation_link, csv_bytes, decode_token, iso_date, listmonk_values,
                       parse_csv, token_recipient, validate_answers)

bp = Blueprint("web", __name__)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin") != current_app.config["ADMIN_USERNAME"]:
            return redirect(url_for("web.login"))
        return view(*args, **kwargs)
    return wrapped


def campaign_or_404(campaign_id):
    return db.get_or_404(Campaign, campaign_id)


def transfer_is_running(campaign_id):
    from .transfers import active_lease
    config = db.session.get(ListmonkConfig, 1)
    if active_lease(config):
        job = db.session.get(TransferJob, config.lease_job_id)
        return bool(job and job.campaign_id == campaign_id)
    return False


def public_error():
    db.session.rollback()
    return render_template("error.html", message="Dieser Bestätigungslink ist ungültig oder die Campaign ist nicht mehr aktiv."), 404


@bp.get("/healthz")
def health():
    db.session.execute(text("SELECT id FROM campaign LIMIT 1"))
    return {"status": "ok"}


@bp.get("/")
def index():
    return redirect(url_for("web.campaigns"))


@bp.route("/admin/login", methods=["GET", "POST"])
def login():
    if session.get("admin") == current_app.config["ADMIN_USERNAME"]:
        return redirect(url_for("web.campaigns"))
    error, status = None, 200
    if request.method == "POST":
        address_hash = hmac.new(current_app.config["SECRET_KEY"].encode(), (request.remote_addr or "unknown").encode(), hashlib.sha256).hexdigest()
        cutoff = utcnow() - timedelta(minutes=15)
        begin_write()
        db.session.execute(db.delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff))
        failures = db.session.scalar(db.select(func.count()).select_from(LoginAttempt).where(LoginAttempt.address_hash == address_hash))
        if failures >= 5:
            error, status = "Zu viele Loginversuche. Bitte versuchen Sie es in 15 Minuten erneut.", 429
        else:
            password_ok = check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], request.form.get("password", ""))
            username_ok = hmac.compare_digest(request.form.get("username", "").encode(), current_app.config["ADMIN_USERNAME"].encode())
            if username_ok and password_ok:
                db.session.execute(db.delete(LoginAttempt).where(LoginAttempt.address_hash == address_hash))
                db.session.commit()
                session.clear()
                session["admin"] = current_app.config["ADMIN_USERNAME"]
                session["import_owner"] = secrets.token_hex(24)
                session.permanent = True
                return redirect(url_for("web.campaigns"))
            db.session.add(LoginAttempt(address_hash=address_hash))
            error, status = "Benutzername oder Passwort ist falsch.", 401
        db.session.commit()
    response = Response(render_template("login.html", error=error), status=status)
    if status == 429:
        response.headers["Retry-After"] = "900"
    return response


@bp.post("/admin/logout")
@admin_required
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@bp.get("/admin/campaigns")
@admin_required
def campaigns():
    counts = db.select(
        Recipient.campaign_id.label("campaign_id"), func.count(Recipient.id).label("total"),
        func.count(Recipient.confirmed_at).label("confirmed"), func.count(Submission.id).label("submitted"),
    ).outerjoin(Submission).group_by(Recipient.campaign_id).subquery()
    rows = db.session.execute(db.select(Campaign, func.coalesce(counts.c.total, 0),
        func.coalesce(counts.c.confirmed, 0), func.coalesce(counts.c.submitted, 0)
    ).outerjoin(counts, Campaign.id == counts.c.campaign_id).order_by(Campaign.created_at.desc())).all()
    return render_template("campaigns.html", rows=rows)


def settings_values(campaign=None, form=None):
    if form is not None:
        columns = [form.getlist(key) for key in ("field_id", "field_label", "field_type", "field_required", "field_description")]
        fields = [dict(zip(("id", "label", "field_type", "required", "description"), row)) for row in zip(*columns)]
        return {"name": form.get("name", ""), "identity_type": form.get("identity_type", "email"),
                "key_column": form.get("key_column", "key"), "confirmation_mode": form.get("confirmation_mode", "action"),
                "confirmation_text": form.get("confirmation_text", ""), "form_enabled": form.get("form_enabled") == "1",
                "show_text_with_form": form.get("show_text_with_form") == "1", "active": form.get("active") == "1", "fields": fields}
    if campaign:
        return {key: getattr(campaign, key) for key in ("name", "identity_type", "key_column", "confirmation_mode",
                "confirmation_text", "form_enabled", "show_text_with_form", "active")} | {
                "fields": [{"id": str(f.id), "label": f.label, "field_type": f.field_type,
                            "required": "1" if f.required else "0", "description": f.description} for f in campaign.fields]}
    return {"name": "", "identity_type": "email", "key_column": "key", "confirmation_mode": "action",
            "confirmation_text": "Vielen Dank für Ihre Bestätigung.", "form_enabled": False,
            "show_text_with_form": False, "active": True, "fields": []}


def apply_settings(campaign, values, form):
    if not 1 <= len(values["name"].strip()) <= 200:
        raise ValueError("Der Campaign-Name muss 1 bis 200 Zeichen enthalten.")
    if values["identity_type"] not in ("email", "key") or values["confirmation_mode"] not in ("visit", "action"):
        raise ValueError("Ungültige Campaign-Einstellungen.")
    if not 1 <= len(values["confirmation_text"].strip()) <= 10000:
        raise ValueError("Der Bestätigungstext muss 1 bis 10.000 Zeichen enthalten.")
    if not 1 <= len(values["key_column"].strip()) <= 200:
        raise ValueError("Bitte geben Sie den Namen der Key-Spalte an.")
    columns = [form.getlist(key) for key in ("field_id", "field_label", "field_type", "field_required", "field_description")]
    if len({len(column) for column in columns}) != 1 or len(values["fields"]) > 50:
        raise ValueError("Ungültige Formularstruktur; höchstens 50 Felder sind erlaubt.")
    if values["form_enabled"] and not values["fields"]:
        raise ValueError("Ein aktiviertes Formular benötigt mindestens ein Feld.")
    if campaign.id and values["identity_type"] != campaign.identity_type:
        has_recipients = db.session.scalar(db.select(Recipient.id).where(Recipient.campaign_id == campaign.id).limit(1))
        if has_recipients is not None or campaign.locked_at:
            raise ValueError("Die Identifikationsart ist nach Aufnahme von Empfängern festgeschrieben.")
    existing = {str(field.id): field for field in campaign.fields}
    ids = [field["id"] for field in values["fields"] if field["id"]]
    if len(ids) != len(set(ids)) or any(field_id not in existing for field_id in ids):
        raise ValueError("Die Formularfelder sind ungültig. Bitte laden Sie die Einstellungen neu.")
    if campaign.locked_at:
        old_structure = [(str(f.id), f.field_type, "1" if f.required else "0") for f in campaign.fields]
        new_structure = [(f["id"], f["field_type"], f["required"]) for f in values["fields"]]
        if (values["confirmation_mode"] != campaign.confirmation_mode or values["form_enabled"] != campaign.form_enabled
                or old_structure != new_structure):
            raise ValueError("Nach der ersten Bestätigung oder Abgabe sind Modus und Formularstruktur gesperrt.")
    for field in values["fields"]:
        if not 1 <= len(field["label"].strip()) <= 200 or len(field["description"]) > 2000:
            raise ValueError("Felder benötigen eine Bezeichnung mit höchstens 200 Zeichen und eine Beschreibung mit höchstens 2.000 Zeichen.")
        if field["field_type"] not in FIELD_TYPES or field["required"] not in ("0", "1"):
            raise ValueError("Ungültiger Feldtyp oder Pflichtfeld-Einstellung.")
    for key, value in values.items():
        if key != "fields":
            setattr(campaign, key, value.strip() if isinstance(value, str) else value)
    fields = []
    for position, data in enumerate(values["fields"]):
        field = existing.get(data["id"]) or FormField()
        field.label = data["label"].strip()
        field.field_type = data["field_type"]
        field.required = data["required"] == "1"
        field.description = data["description"].strip()
        field.position = position
        fields.append(field)
    campaign.fields = fields


@bp.route("/admin/campaigns/new", methods=["GET", "POST"])
@bp.route("/admin/campaigns/<int:campaign_id>/settings", methods=["GET", "POST"])
@admin_required
def campaign_settings(campaign_id=None):
    if request.method == "POST":
        begin_write()
    campaign = campaign_or_404(campaign_id) if campaign_id else None
    values = settings_values(campaign, request.form if request.method == "POST" else None)
    error = None
    if request.method == "POST":
        target = campaign or Campaign()
        try:
            apply_settings(target, values, request.form)
            db.session.add(target)
            db.session.commit()
            flash("Die Campaign wurde gespeichert.", "success")
            return redirect(url_for("web.campaign_detail", campaign_id=target.id))
        except ValueError as exc:
            db.session.rollback()
            error = str(exc)
    identity_locked = bool(campaign and (campaign.locked_at or db.session.scalar(db.select(Recipient.id).where(Recipient.campaign_id == campaign.id).limit(1)) is not None))
    return render_template("settings.html", campaign=campaign, values=values, error=error, identity_locked=identity_locked), 400 if error else 200


@bp.get("/admin/campaigns/<int:campaign_id>")
@admin_required
def campaign_detail(campaign_id):
    campaign = campaign_or_404(campaign_id)
    query = db.select(Recipient).where(Recipient.campaign_id == campaign.id)
    search = request.args.get("search", "")[:255]
    if search:
        query = query.where(or_(Recipient.identity.contains(search, autoescape=True), Recipient.name.contains(search, autoescape=True), Recipient.email.contains(search, autoescape=True)))
    status = request.args.get("status", "all")
    filters = {"confirmed": Recipient.confirmed_at.is_not(None), "pending": Recipient.confirmed_at.is_(None),
               "submitted": Recipient.submission.has(), "form_pending": ~Recipient.submission.has()}
    if status in filters:
        query = query.where(filters[status])
    page = max(1, request.args.get("page", 1, type=int))
    recipients = db.paginate(query.order_by(Recipient.id), page=page, per_page=50, error_out=False)
    counts = db.session.execute(db.select(func.count(Recipient.id), func.count(Recipient.confirmed_at), func.count(Submission.id)).outerjoin(Submission).where(Recipient.campaign_id == campaign.id)).one()
    snippet = '{{ index .Subscriber.Attribs.campaign_action_links "' + campaign.uuid + '" }}'
    connection = db.session.get(ListmonkConfig, 1)
    jobs = db.session.scalars(db.select(TransferJob).where(TransferJob.campaign_id == campaign.id).order_by(TransferJob.created_at.desc()).limit(10)).all()
    return render_template("campaign_detail.html", campaign=campaign, recipients=recipients, counts=counts, search=search, status=status,
                           snippet=snippet, listmonk_enabled=bool(connection and connection.enabled), transfer_jobs=jobs)


@bp.post("/admin/campaigns/<int:campaign_id>/duplicate")
@admin_required
def campaign_duplicate(campaign_id):
    begin_write()
    original = campaign_or_404(campaign_id)
    copy = Campaign(name=original.name[:190] + " (Kopie)", identity_type=original.identity_type, key_column=original.key_column,
                    confirmation_mode=original.confirmation_mode, confirmation_text=original.confirmation_text,
                    form_enabled=original.form_enabled, show_text_with_form=original.show_text_with_form, active=True)
    copy.fields = [FormField(label=f.label, field_type=f.field_type, required=f.required, description=f.description, position=f.position) for f in original.fields]
    db.session.add(copy)
    db.session.commit()
    flash("Die Einstellungen wurden in eine neue Campaign kopiert.", "success")
    return redirect(url_for("web.campaign_settings", campaign_id=copy.id))


@bp.route("/admin/campaigns/<int:campaign_id>/delete", methods=["GET", "POST"])
@admin_required
def campaign_delete(campaign_id):
    if request.method == "POST":
        begin_write()
    campaign = campaign_or_404(campaign_id)
    if request.method == "POST":
        if transfer_is_running(campaign.id):
            db.session.rollback()
            flash("Eine Listmonk-Charge läuft gerade. Bitte pausieren Sie den Vorgang und warten Sie vor dem Löschen auf den Abschluss der Charge.", "error")
            return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
        db.session.delete(campaign)
        db.session.commit()
        flash("Die Campaign und ihre Daten wurden gelöscht.", "success")
        return redirect(url_for("web.campaigns"))
    return render_template("delete.html", campaign=campaign, recipient=None)


@bp.route("/admin/campaigns/<int:campaign_id>/recipients/<int:recipient_id>", methods=["GET", "POST"])
@admin_required
def recipient_detail(campaign_id, recipient_id):
    # POST only issues a stable personal link, never a confirmation.
    if request.method == "POST":
        begin_write()
    campaign = campaign_or_404(campaign_id)
    recipient = db.get_or_404(Recipient, recipient_id)
    if recipient.campaign_id != campaign.id:
        abort(404)
    if request.method == "POST":
        confirmation_link(recipient)
        db.session.commit()
        return redirect(url_for("web.recipient_detail", campaign_id=campaign.id, recipient_id=recipient.id))
    link = current_app.config["PUBLIC_BASE_URL"] + "/r/" + recipient.token if recipient.token else None
    return render_template("recipient_detail.html", campaign=campaign, recipient=recipient, link=link)


@bp.route("/admin/campaigns/<int:campaign_id>/recipients/<int:recipient_id>/delete", methods=["GET", "POST"])
@admin_required
def recipient_delete(campaign_id, recipient_id):
    if request.method == "POST":
        begin_write()
    campaign = campaign_or_404(campaign_id)
    recipient = db.get_or_404(Recipient, recipient_id)
    if recipient.campaign_id != campaign.id:
        abort(404)
    if request.method == "POST":
        if transfer_is_running(campaign.id):
            db.session.rollback()
            flash("Eine Listmonk-Charge läuft gerade. Bitte pausieren Sie den Vorgang und warten Sie vor dem Löschen auf den Abschluss der Charge.", "error")
            return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
        db.session.delete(recipient)
        db.session.commit()
        flash("Der Empfänger und seine Daten wurden gelöscht.", "success")
        return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
    return render_template("delete.html", campaign=campaign, recipient=recipient)


@bp.route("/admin/campaigns/<int:campaign_id>/import", methods=["GET", "POST"])
@admin_required
def import_csv(campaign_id):
    campaign = campaign_or_404(campaign_id)
    if request.method == "POST":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return render_template("import.html", campaign=campaign, error="Bitte wählen Sie eine CSV-Datei aus."), 400
        rows, errors = parse_csv(upload.read(), request.form.get("delimiter", ","), campaign.identity_type,
                                 request.form.get("identity_column", campaign.key_column))
        identity_type = campaign.identity_type
        db.session.rollback()
        begin_write()
        campaign = campaign_or_404(campaign_id)
        if campaign.identity_type != identity_type:
            errors.append("Die Identifikationsart wurde während des Imports geändert. Bitte laden Sie die Datei erneut hoch.")
        db.session.execute(db.delete(ImportJob).where(ImportJob.expires_at < utcnow()))
        job = ImportJob(campaign_id=campaign.id, owner=session["import_owner"], identity_type=identity_type,
                        rows=rows, errors=errors, expires_at=utcnow() + timedelta(hours=1))
        db.session.add(job)
        db.session.commit()
        return redirect(url_for("web.import_preview", campaign_id=campaign.id, job_id=job.id))
    return render_template("import.html", campaign=campaign)


@bp.route("/admin/campaigns/<int:campaign_id>/import/<job_id>", methods=["GET", "POST"])
@admin_required
def import_preview(campaign_id, job_id):
    if request.method == "POST":
        begin_write()
    campaign = campaign_or_404(campaign_id)
    job = db.get_or_404(ImportJob, job_id)
    if job.campaign_id != campaign.id or job.owner != session.get("import_owner") or job.expires_at.replace(tzinfo=timezone.utc) <= utcnow():
        abort(404)
    rows = annotate_import(job.rows, campaign.id)
    if request.method == "POST":
        if job.errors or job.identity_type != campaign.identity_type:
            db.session.rollback()
            flash("Dieser Import kann nicht übernommen werden. Bitte prüfen Sie die Datei und Einstellungen.", "error")
            return redirect(url_for("web.import_csv", campaign_id=campaign.id))
        imported = 0
        for row in rows:
            if not row["duplicate"]:
                db.session.add(Recipient(campaign_id=campaign.id, identity=row["identity"], email=row["email"],
                                         name=row["name"], source=row["source"], attributes=row["attributes"]))
                imported += 1
        db.session.delete(job)
        db.session.commit()
        flash(f"{imported} Empfänger importiert; {len(rows) - imported} Dubletten übersprungen.", "success")
        return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
    return render_template("import_preview.html", campaign=campaign, job=job, rows=rows[:100],
                           total=len(rows), duplicates=sum(row["duplicate"] for row in rows))


def download_csv(data, filename):
    return Response(data, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.post("/admin/campaigns/<int:campaign_id>/export/links")
@admin_required
def export_links(campaign_id):
    begin_write()
    campaign = campaign_or_404(campaign_id)
    recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == campaign.id).order_by(Recipient.id)).all()
    format_name = request.form.get("format", "simple")
    if format_name not in ("simple", "listmonk"):
        abort(400)
    if format_name == "listmonk":
        if any(not recipient.email for recipient in recipients):
            db.session.rollback()
            flash("Für den Listmonk-Export benötigen alle Empfänger eine gültige E-Mail-Adresse.", "error")
            return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
        if len({recipient.email for recipient in recipients}) != len(recipients):
            db.session.rollback()
            flash("Für den Listmonk-Export müssen die E-Mail-Adressen innerhalb der Campaign eindeutig sein.", "error")
            return redirect(url_for("web.campaign_detail", campaign_id=campaign.id))
        header = LISTMONK_COLUMNS
        rows = [listmonk_values(recipient) for recipient in recipients]
    else:
        header = [campaign.identity_type, "link"]
        rows = [[recipient.identity, confirmation_link(recipient)] for recipient in recipients]
    data = csv_bytes(header, rows)
    db.session.commit()
    return download_csv(data, f"campaign-{campaign.id}-{format_name}-links.csv")


@bp.get("/admin/campaigns/<int:campaign_id>/export/results")
@admin_required
def export_results(campaign_id):
    campaign = campaign_or_404(campaign_id)
    recipients = db.session.scalars(db.select(Recipient).where(Recipient.campaign_id == campaign.id).order_by(Recipient.id)).all()
    source_columns = sorted({key for recipient in recipients for key in recipient.source if key not in LISTMONK_COLUMNS})
    header = [campaign.identity_type] + (["email"] if campaign.identity_type == "key" else [])
    header += ["name", "uuid", "attributes", "status", "created_at", "updated_at", "confirmed_at", "submitted_at"]
    header += [f"source_{key}" for key in source_columns]
    header += [f"field_{field.id}_{field.label}" for field in campaign.fields]
    rows = []
    for recipient in recipients:
        submission = recipient.submission
        row = [recipient.identity] + ([recipient.email] if campaign.identity_type == "key" else [])
        row += [recipient.name, recipient.source.get("uuid", ""), json.dumps(recipient.attributes, ensure_ascii=False),
                recipient.source.get("status", ""), recipient.source.get("created_at") or iso_date(recipient.created_at),
                recipient.source.get("updated_at", ""), iso_date(recipient.confirmed_at), iso_date(submission.submitted_at) if submission else ""]
        row += [recipient.source.get(key, "") for key in source_columns]
        row += [submission.answers.get(str(field.id), "") if submission else "" for field in campaign.fields]
        rows.append(row)
    return download_csv(csv_bytes(header, rows, spreadsheet_safe=True), f"campaign-{campaign.id}-results.csv")


@bp.get("/admin/campaigns/<int:campaign_id>/preview")
@admin_required
def campaign_preview(campaign_id):
    campaign = campaign_or_404(campaign_id)
    return render_template("public.html", campaign=campaign, recipient=None, token=None, preview=True,
                           complete=campaign.confirmation_mode == "visit" and not campaign.form_enabled, errors={}, values={})


@bp.route("/r/<token>", methods=["GET", "POST"])
def public_confirmation(token):
    if request.method != "HEAD":
        begin_write()
    payload = decode_token(token)
    if payload is None:
        return public_error()
    recipient = token_recipient(payload)
    if recipient is None:
        return public_error()
    campaign = recipient.campaign
    errors, values, status = {}, {}, 200
    if request.method == "POST":
        if campaign.form_enabled and recipient.submission is None:
            answers, errors, values = validate_answers(campaign.fields, request.form)
            if not errors:
                confirm(recipient)
                db.session.add(Submission(recipient=recipient, answers=answers))
            else:
                status = 422
        elif not campaign.form_enabled:
            confirm(recipient)
        db.session.commit()
        if not errors:
            return redirect(url_for("web.public_confirmation", token=token), code=303)
    elif request.method == "GET":
        if campaign.confirmation_mode == "visit":
            confirm(recipient)
        db.session.commit()
    complete = recipient.submission is not None if campaign.form_enabled else recipient.confirmed_at is not None
    return render_template("public.html", campaign=campaign, recipient=recipient, token=token, preview=False,
                           complete=complete, errors=errors, values=values), status
