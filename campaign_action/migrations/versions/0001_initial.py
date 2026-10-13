"""Initial campaign, recipient, form and administration schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("campaign",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("identity_type", sa.String(10), nullable=False),
        sa.Column("key_column", sa.String(200), nullable=False),
        sa.Column("confirmation_mode", sa.String(10), nullable=False),
        sa.Column("confirmation_text", sa.Text(), nullable=False),
        sa.Column("form_enabled", sa.Boolean(), nullable=False),
        sa.Column("show_text_with_form", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("form_field",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("field_type", sa.String(10), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False))
    op.create_index("ix_form_field_campaign_id", "form_field", ["campaign_id"])
    op.create_table("recipient",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False),
        sa.Column("identity", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("token", sa.Text()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "identity", name="uq_recipient_campaign_identity"))
    op.create_index("ix_recipient_campaign_id", "recipient", ["campaign_id"])
    op.create_index("ix_recipient_confirmed_at", "recipient", ["confirmed_at"])
    op.create_table("submission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recipient_id", sa.Integer(), sa.ForeignKey("recipient.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("import_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("identity_type", sa.String(10), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_import_job_campaign_id", "import_job", ["campaign_id"])
    op.create_table("login_attempt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address_hash", sa.String(64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_login_attempt_address_hash", "login_attempt", ["address_hash"])
    op.create_index("ix_login_attempt_attempted_at", "login_attempt", ["attempted_at"])


def downgrade():
    for name in ("login_attempt", "import_job", "submission", "recipient", "form_field", "campaign"):
        op.drop_table(name)
