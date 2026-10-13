"""Independent campaign keys and resumable listmonk transfers."""
from alembic import op
import sqlalchemy as sa

from campaign_action.crypto import new_campaign_secret

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("campaign", sa.Column("secret", sa.Text(), nullable=True))
    connection = op.get_bind()
    for campaign_id in connection.execute(sa.text("SELECT id FROM campaign")).scalars().all():
        connection.execute(sa.text("UPDATE campaign SET secret=:secret WHERE id=:id"),
                           {"id": campaign_id, "secret": new_campaign_secret()})
    with op.batch_alter_table("campaign") as batch:
        batch.alter_column("secret", existing_type=sa.Text(), nullable=False)
    op.add_column("recipient", sa.Column("token_version", sa.Integer(), nullable=False, server_default="2"))
    connection.execute(sa.text("UPDATE recipient SET token_version=1 WHERE token IS NOT NULL"))
    op.create_index("ix_recipient_token", "recipient", ["token"], unique=True)
    op.create_table("listmonk_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("username", sa.String(200), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_job_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_table("transfer_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("list_name", sa.String(200), nullable=False),
        sa.Column("identity_type", sa.String(10), nullable=False),
        sa.Column("key_source", sa.String(20), nullable=False),
        sa.Column("key_attribute", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_transfer_job_campaign_id", "transfer_job", ["campaign_id"])
    op.create_table("transfer_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("transfer_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscriber_id", sa.Integer()),
        sa.Column("recipient_id", sa.Integer(), sa.ForeignKey("recipient.id", ondelete="SET NULL")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.UniqueConstraint("job_id", "subscriber_id", name="uq_transfer_subscriber"))
    op.create_index("ix_transfer_item_job_id", "transfer_item", ["job_id"])


def downgrade():
    for table in ("transfer_item", "transfer_job", "listmonk_config"):
        op.drop_table(table)
    op.drop_index("ix_recipient_token", table_name="recipient")
    with op.batch_alter_table("recipient") as batch:
        batch.drop_column("token_version")
    with op.batch_alter_table("campaign") as batch:
        batch.drop_column("secret")
