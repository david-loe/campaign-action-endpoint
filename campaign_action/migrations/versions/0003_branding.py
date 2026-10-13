"""Public page branding and optional campaign names."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("branding_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("accent_color", sa.String(7), nullable=False, server_default="#2359dc"),
        sa.Column("logo_data", sa.LargeBinary()),
        sa.Column("logo_mime", sa.String(30)))
    op.add_column("campaign", sa.Column("show_campaign_name", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("campaign", sa.Column("branding_mode", sa.String(10), nullable=False, server_default="global"))
    op.add_column("campaign", sa.Column("accent_color", sa.String(7), nullable=False, server_default="#2359dc"))
    op.add_column("campaign", sa.Column("logo_data", sa.LargeBinary()))
    op.add_column("campaign", sa.Column("logo_mime", sa.String(30)))


def downgrade():
    with op.batch_alter_table("campaign") as batch:
        for column in ("logo_mime", "logo_data", "accent_color", "branding_mode", "show_campaign_name"):
            batch.drop_column(column)
    op.drop_table("branding_config")
