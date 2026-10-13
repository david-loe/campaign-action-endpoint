import argparse
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def migrate(app):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with app.app_context():
        from .models import db
        with db.engine.connect() as connection:
            # Alembic's SQLite table rebuild must not trigger ON DELETE CASCADE
            # on existing recipients when replacing the campaign table.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Database migration violated foreign keys")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


def main():
    parser = argparse.ArgumentParser(description="Campaign Action Endpoint")
    parser.add_argument("command", choices=["migrate", "run", "backup"])
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    from . import create_app
    app = create_app()
    if args.command == "migrate":
        migrate(app)
    elif args.command == "run":
        migrate(app)
        app.run(host="127.0.0.1", port=8080)
    elif args.command == "backup":
        if not args.destination:
            parser.error("backup requires a destination path")
        destination = Path(args.destination).expanduser().resolve()
        if destination.exists():
            parser.error("backup destination already exists")
        source_path = Path(app.config["DATABASE_PATH"]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's backup API includes committed WAL data during live operation.
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
            if source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone() is None:
                parser.error("backup source is not an initialized application database")
            with sqlite3.connect(destination) as target:
                source.backup(target)
        print(f"Backup saved: {destination}")


if __name__ == "__main__":
    main()
