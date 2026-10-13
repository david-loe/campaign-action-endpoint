"""Replace local links with random 128-bit codes; remove obsolete link keys."""
import secrets

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE recipient SET token=NULL WHERE token=''"))
    recipient_ids = connection.execute(sa.text("SELECT id FROM recipient WHERE token IS NOT NULL ORDER BY id")).scalars().all()
    for recipient_id in recipient_ids:
        for _ in range(5):
            token = secrets.token_urlsafe(16)
            existing = connection.execute(sa.text("SELECT id FROM recipient WHERE token=:token LIMIT 1"),
                                          {"token": token}).scalar()
            if existing is None:
                connection.execute(sa.text("UPDATE recipient SET token=:token WHERE id=:id"),
                                   {"token": token, "id": recipient_id})
                break
        else:
            raise RuntimeError("Ein eindeutiger Bestätigungslink konnte nicht erzeugt werden.")
    with op.batch_alter_table("recipient") as batch:
        batch.alter_column("token", existing_type=sa.Text(), type_=sa.String(22), existing_nullable=True)
        batch.drop_column("token_version")
    with op.batch_alter_table("campaign") as batch:
        batch.drop_column("secret")


def downgrade():
    raise RuntimeError("Die ersetzten Links können nicht wiederhergestellt werden. Verwenden Sie eine Datenbanksicherung.")
