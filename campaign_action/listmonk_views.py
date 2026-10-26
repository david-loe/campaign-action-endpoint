from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import case

from .branding import DEFAULT_ACCENT, global_branding
from .crypto import encrypt_value
from .listmonk import ListmonkError, validate_base_url
from .models import ListmonkConfig, Recipient, TransferItem, TransferJob, db
from .services import annotate_import, begin_write
from .transfers import active_lease, client_for, counts, incomplete_load_message, process_step
from .views import admin_required, campaign_or_404

bp = Blueprint("listmonk", __name__)
STATUS_LABELS = {"loading": "Subscribers laden", "preview": "Import prüfen", "ready": "Export prüfen", "running": "Links übertragen",
                 "done": "Abgeschlossen", "partial": "Teilweise abgeschlossen", "failed": "Unterbrochen", "stopped": "Gestoppt"}


def render_admin_settings(config=None, values=None, error=None, branding_values=None, branding_error=None):
    if config is None:
        config = db.session.get(ListmonkConfig, 1)
    if values is None:
        values = {"base_url": config.base_url if config else "", "username": config.username if config else "",
                  "enabled": bool(config and config.enabled)}
    branding = global_branding()
    return render_template("admin_settings.html", config=config, values=values, error=error, branding_config=branding,
                           branding_values=branding_values or {"accent_color": branding.accent_color if branding else DEFAULT_ACCENT},
                           branding_error=branding_error)


def configured():
    config = db.session.get(ListmonkConfig, 1)
    if not config or not config.enabled:
        abort(404)
    return config


def revision_matches(job):
    config = db.session.get(ListmonkConfig, 1)
    return bool(config and config.enabled and config.revision == job.config_revision and job.campaign.identity_type == job.identity_type)


@bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        begin_write()
    config = db.session.get(ListmonkConfig, 1)
    error = None
    if request.method == "POST":
        try:
            if active_lease(config):
                raise ValueError("Eine Listmonk-Charge läuft gerade. Bitte warten Sie bis zum Abschluss der Charge und pausieren Sie den Vorgang.")
            if config is None:
                config = ListmonkConfig(id=1, revision=0, enabled=False, base_url="", username="", token="")
                db.session.add(config)
            old_values = (config.base_url, config.username, config.token, config.enabled)
            if request.form.get("action") == "remove":
                config.base_url = config.username = config.token = ""
                config.enabled = False
            else:
                base_url = validate_base_url(request.form.get("base_url", ""))
                username = request.form.get("username", "").strip()
                token = request.form.get("token", "")
                if not username or len(username) > 200 or ":" in username or len(token) > 4096:
                    raise ValueError("Bitte geben Sie einen gültigen API-Benutzernamen und Token ein.")
                if not token and (not config.token or config.base_url != base_url or config.username != username):
                    raise ValueError("Für eine neue URL oder einen neuen Benutzernamen benötigen Sie einen neuen API-Token.")
                if token:
                    config.token = encrypt_value(token)
                config.base_url, config.username = base_url, username
                config.enabled = request.form.get("enabled") == "1"
            changed = config.revision == 0 or old_values != (config.base_url, config.username, config.token, config.enabled)
            if changed:
                config.revision += 1
            # Keep the monotonically increasing revision even after removal.
            # Old jobs must never resume against a newly configured instance.
            config.lease_owner = config.lease_job_id = config.lease_until = None
            if changed:
                db.session.execute(db.update(TransferJob).where(TransferJob.status.not_in(("done", "stopped"))).values(
                    status="stopped", error="Die Listmonk-Konfiguration wurde geändert. Bitte starten Sie einen neuen Vorgang."))
            db.session.commit()
            flash("Die Listmonk-Einstellungen wurden gespeichert.", "success")
            return redirect(url_for("listmonk.settings"))
        except ValueError as exc:
            db.session.rollback()
            error = str(exc)
            config = db.session.get(ListmonkConfig, 1)
    values = {"base_url": config.base_url if config else "", "username": config.username if config else "", "enabled": bool(config and config.enabled)}
    if request.method == "POST" and error:
        values.update(base_url=request.form.get("base_url", ""), username=request.form.get("username", ""), enabled=request.form.get("enabled") == "1")
    return render_admin_settings(config=config, values=values, error=error), 400 if error else 200


@bp.post("/admin/settings/listmonk/test")
@admin_required
def test_connection():
    config = configured()
    client = client_for(config)
    db.session.rollback()
    try:
        with client:
            lists = client.lists()
        flash(f"Verbindung erfolgreich: {len(lists)} aktive Listen sind zugänglich.", "success")
    except ListmonkError as exc:
        flash(str(exc), "error")
    return redirect(url_for("listmonk.settings"))


@bp.route("/admin/campaigns/<int:campaign_id>/listmonk/<direction>", methods=["GET", "POST"])
@admin_required
def transfer_select(campaign_id, direction):
    if direction not in ("import", "export"):
        abort(404)
    campaign = campaign_or_404(campaign_id)
    config = configured()
    revision = config.revision
    client = client_for(config)
    db.session.rollback()
    error, lists = None, []
    try:
        with client:
            if request.method == "GET":
                lists = client.lists()
            else:
                list_id = request.form.get("list_id", type=int)
                if not list_id or list_id < 1:
                    raise ValueError("Bitte wählen Sie eine Subscriber-Liste aus.")
                selected = client.active_list(list_id)
        if request.method == "POST":
            key_source = request.form.get("key_source", "uuid")
            attribute = request.form.get("key_attribute", "").strip()
            if key_source not in ("uuid", "id", "attribute") or len(attribute) > 200:
                raise ValueError("Ungültige Identitätsquelle.")
            begin_write()
            campaign = campaign_or_404(campaign_id)
            config = configured()
            if config.revision != revision:
                raise ValueError("Die Listmonk-Konfiguration wurde geändert. Bitte laden Sie die Seite neu.")
            if direction == "import" and campaign.identity_type == "key" and key_source == "attribute" and not attribute:
                raise ValueError("Bitte geben Sie den Namen des Subscriber-Attributs ein.")
            if direction == "export":
                emails = list(db.session.scalars(db.select(Recipient.email).where(Recipient.campaign_id == campaign.id, Recipient.email.is_not(None))))
                if len(emails) != len(set(emails)):
                    raise ValueError("Für den Listmonk-Export müssen E-Mail-Adressen innerhalb der Campaign eindeutig sein.")
            job = TransferJob(campaign_id=campaign.id, config_revision=revision, direction=direction,
                              list_id=list_id, list_name=str(selected.get("name", ""))[:200], identity_type=campaign.identity_type,
                              key_source=key_source, key_attribute=attribute)
            db.session.add(job)
            db.session.commit()
            return redirect(url_for("listmonk.job_detail", job_id=job.id, run=1))
    except (ListmonkError, ValueError) as exc:
        db.session.rollback()
        error = str(exc)
        # Retain the selected list when a local preflight check failed.
        if request.method == "POST" and "selected" in locals():
            lists = [selected]
    return render_template("listmonk_select.html", campaign=campaign, direction=direction, lists=lists, error=error), 400 if error else 200


@bp.get("/admin/listmonk/jobs/<job_id>")
@admin_required
def job_detail(job_id):
    job = db.get_or_404(TransferJob, job_id)
    summary = counts(job.id)
    items = db.session.scalars(db.select(TransferItem).where(TransferItem.job_id == job.id).order_by(
        case((TransferItem.status.in_(("invalid", "error")), 0), else_=1), TransferItem.id).limit(100)).all()
    rows, duplicates, load_error = [], 0, ""
    if job.direction == "import" and job.status == "preview":
        load_error = incomplete_load_message(sum(summary.values()), job.total)
        payloads = list(db.session.scalars(db.select(TransferItem.payload).where(TransferItem.job_id == job.id, TransferItem.status == "staged").order_by(TransferItem.id)))
        annotated = annotate_import(payloads, job.campaign_id)
        duplicates = sum(row["duplicate"] for row in annotated)
        rows = annotated[:100]
    return render_template("listmonk_job.html", job=job, summary=summary, items=items, rows=rows, duplicates=duplicates,
                           status_labels=STATUS_LABELS, current=revision_matches(job), autorun=request.args.get("run") == "1",
                           load_error=load_error)


@bp.post("/admin/listmonk/jobs/<job_id>/step")
@admin_required
def job_step(job_id):
    db.get_or_404(TransferJob, job_id)
    db.session.rollback()
    result = process_step(job_id)
    if request.accept_mimetypes.best == "application/json":
        return jsonify(result)
    if result.get("message"):
        flash(result["message"], "error" if result["status"] not in ("busy", "done") else "success")
    return redirect(url_for("listmonk.job_detail", job_id=job_id))


@bp.post("/admin/listmonk/jobs/<job_id>/commit")
@admin_required
def job_commit(job_id):
    begin_write()
    job = db.get_or_404(TransferJob, job_id)
    if not revision_matches(job) or job.status not in ("preview", "ready"):
        db.session.rollback()
        flash("Dieser Vorgang kann nicht übernommen werden. Bitte starten Sie ihn erneut.", "error")
        return redirect(url_for("listmonk.job_detail", job_id=job_id))
    if job.direction == "export":
        job.status = "running"
        db.session.commit()
        return redirect(url_for("listmonk.job_detail", job_id=job.id, run=1))
    summary = counts(job.id)
    load_error = incomplete_load_message(sum(summary.values()), job.total)
    if load_error:
        db.session.rollback()
        flash(load_error, "error")
        return redirect(url_for("listmonk.job_detail", job_id=job_id))
    if summary.get("invalid"):
        db.session.rollback()
        flash("Der Import enthält fehlerhafte Subscribers und wird nicht übernommen.", "error")
        return redirect(url_for("listmonk.job_detail", job_id=job_id))
    payloads = list(db.session.scalars(db.select(TransferItem.payload).where(TransferItem.job_id == job.id).order_by(TransferItem.id)))
    rows = annotate_import(payloads, job.campaign_id)
    imported = 0
    for row in rows:
        if not row["duplicate"]:
            db.session.add(Recipient(campaign_id=job.campaign_id, identity=row["identity"], email=row["email"],
                                     name=row["name"], source=row["source"], attributes=row["attributes"]))
            imported += 1
    job.status = "done"
    db.session.commit()
    flash(f"{imported} Empfänger aus Listmonk importiert; {len(rows) - imported} Dubletten übersprungen.", "success")
    return redirect(url_for("web.campaign_detail", campaign_id=job.campaign_id))


@bp.post("/admin/listmonk/jobs/<job_id>/retry")
@admin_required
def job_retry(job_id):
    begin_write()
    job = db.get_or_404(TransferJob, job_id)
    if not revision_matches(job) or job.status not in ("failed", "partial"):
        db.session.rollback()
        abort(400)
    summary = counts(job.id)
    job.status = "running" if job.direction == "export" and any(summary.get(key) for key in ("pending", "done", "error", "skipped")) else "loading"
    db.session.execute(db.update(TransferItem).where(TransferItem.job_id == job.id, TransferItem.status == "error").values(status="pending", message=""))
    job.error = ""
    db.session.commit()
    return redirect(url_for("listmonk.job_detail", job_id=job.id, run=1))
