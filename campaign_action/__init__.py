import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

from .models import db

csrf = CSRFProtect()


@event.listens_for(Engine, "connect")
def sqlite_settings(connection, _):
    if connection.__class__.__module__ == "sqlite3":
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


def create_app(config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(
        ENCYPTION_SECRET=os.environ.get("ENCYPTION_SECRET", ""),
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", ""),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        DATABASE_PATH=os.environ.get("DATABASE_PATH", "data/app.sqlite3"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=3600 * 8,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15}},
    )
    if config:
        app.config.update(config)
    for name in ("ENCYPTION_SECRET", "ADMIN_USERNAME", "ADMIN_PASSWORD", "PUBLIC_BASE_URL"):
        if not app.config[name]:
            raise ValueError(f"Missing configuration: {name}")
    if len(app.config["ENCYPTION_SECRET"]) < 32 or len(app.config["ADMIN_PASSWORD"]) < 12:
        raise ValueError("ENCYPTION_SECRET requires 32 characters; ADMIN_PASSWORD requires 12 characters")
    app.config["SECRET_KEY"] = app.config["ENCYPTION_SECRET"]
    base_url = app.config["PUBLIC_BASE_URL"].rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("PUBLIC_BASE_URL must be an http(s) origin without a path or credentials")
    app.config["PUBLIC_BASE_URL"] = base_url
    app.config["SESSION_COOKIE_SECURE"] = parts.scheme == "https"
    database = Path(app.config["DATABASE_PATH"]).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(database)
    app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(app.config["ADMIN_PASSWORD"])
    del app.config["ADMIN_PASSWORD"]
    db.init_app(app)
    csrf.init_app(app)

    from .views import bp
    from .services import iso_date

    app.register_blueprint(bp)
    from .listmonk_views import bp as listmonk_bp
    app.register_blueprint(listmonk_bp)
    app.jinja_env.filters["timestamp"] = iso_date

    # Reject a wrong encryption environment before accessing stored credentials.
    from .crypto import decrypt_value
    with app.app_context(), db.engine.connect() as connection:
        schema = inspect(connection)
        if schema.has_table("listmonk_config"):
            encrypted = connection.execute(text("SELECT token FROM listmonk_config WHERE token != '' LIMIT 1")).scalar()
            if encrypted:
                decrypt_value(encrypted)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(_):
        return render_template("error.html", message="Diese Sitzung ist abgelaufen. Bitte laden Sie die Seite erneut und versuchen Sie es noch einmal."), 400

    @app.errorhandler(413)
    def too_large(_):
        return render_template("error.html", message="Die Anfrage ist zu groß. Dateien dürfen insgesamt höchstens 10 MiB umfassen."), 413

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", message="Diese Seite wurde nicht gefunden."), 404

    @app.errorhandler(OperationalError)
    def database_error(_):
        db.session.rollback()
        app.logger.warning("Database request failed; returning a retryable response")
        response = app.make_response((render_template("error.html", message="Die Datenbank ist vorübergehend nicht verfügbar. Bitte versuchen Sie es in wenigen Sekunden erneut."), 503))
        response.headers["Retry-After"] = "5"
        return response

    return app
