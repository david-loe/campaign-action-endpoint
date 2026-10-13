"""Configurable confirmation heading."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("campaign", sa.Column("confirmation_heading", sa.String(200), nullable=False,
                                     server_default="Bestätigung erfolgreich"))


def downgrade():
    with op.batch_alter_table("campaign") as batch:
        batch.drop_column("confirmation_heading")
