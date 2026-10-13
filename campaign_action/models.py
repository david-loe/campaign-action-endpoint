import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

from .crypto import new_campaign_secret

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=new_uuid)
    name = db.Column(db.String(200), nullable=False)
    secret = db.Column(db.Text, nullable=False, default=new_campaign_secret)
    identity_type = db.Column(db.String(10), nullable=False, default="email")
    key_column = db.Column(db.String(200), nullable=False, default="key")
    confirmation_mode = db.Column(db.String(10), nullable=False, default="action")
    confirmation_text = db.Column(db.Text, nullable=False, default="Vielen Dank für Ihre Bestätigung.")
    form_enabled = db.Column(db.Boolean, nullable=False, default=False)
    show_text_with_form = db.Column(db.Boolean, nullable=False, default=False)
    show_campaign_name = db.Column(db.Boolean, nullable=False, default=True)
    branding_mode = db.Column(db.String(10), nullable=False, default="global")
    accent_color = db.Column(db.String(7), nullable=False, default="#2359dc")
    logo_data = db.deferred(db.Column(db.LargeBinary))
    logo_mime = db.Column(db.String(30))
    active = db.Column(db.Boolean, nullable=False, default=True)
    locked_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    fields = db.relationship("FormField", order_by="FormField.position", cascade="all, delete-orphan", back_populates="campaign")
    recipients = db.relationship("Recipient", cascade="all, delete-orphan", back_populates="campaign", passive_deletes=True)


class FormField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False, index=True)
    label = db.Column(db.String(200), nullable=False)
    field_type = db.Column(db.String(10), nullable=False)
    required = db.Column(db.Boolean, nullable=False, default=False)
    description = db.Column(db.Text, nullable=False, default="")
    position = db.Column(db.Integer, nullable=False)
    campaign = db.relationship("Campaign", back_populates="fields")


class Recipient(db.Model):
    __table_args__ = (UniqueConstraint("campaign_id", "identity", name="uq_recipient_campaign_identity"),)
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=new_uuid)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False, index=True)
    identity = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    name = db.Column(db.String(200), nullable=False, default="")
    source = db.Column(db.JSON, nullable=False, default=dict)
    attributes = db.Column(db.JSON, nullable=False, default=dict)
    token = db.Column(db.Text, unique=True, index=True)
    token_version = db.Column(db.Integer, nullable=False, default=2)
    confirmed_at = db.Column(db.DateTime(timezone=True), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    campaign = db.relationship("Campaign", back_populates="recipients")
    submission = db.relationship("Submission", uselist=False, cascade="all, delete-orphan", back_populates="recipient", passive_deletes=True)


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("recipient.id", ondelete="CASCADE"), unique=True, nullable=False)
    answers = db.Column(db.JSON, nullable=False)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    recipient = db.relationship("Recipient", back_populates="submission")


class ImportJob(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = db.Column(db.String(64), nullable=False)
    identity_type = db.Column(db.String(10), nullable=False)
    rows = db.Column(db.JSON, nullable=False)
    errors = db.Column(db.JSON, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    address_hash = db.Column(db.String(64), nullable=False, index=True)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class BrandingConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    accent_color = db.Column(db.String(7), nullable=False, default="#2359dc")
    logo_data = db.deferred(db.Column(db.LargeBinary))
    logo_mime = db.Column(db.String(30))


class ListmonkConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    base_url = db.Column(db.String(2000), nullable=False, default="")
    username = db.Column(db.String(200), nullable=False, default="")
    token = db.Column(db.Text, nullable=False, default="")
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    lease_owner = db.Column(db.String(64))
    lease_job_id = db.Column(db.String(36))
    lease_until = db.Column(db.DateTime(timezone=True))


class TransferJob(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False, index=True)
    config_revision = db.Column(db.Integer, nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    list_id = db.Column(db.Integer, nullable=False)
    list_name = db.Column(db.String(200), nullable=False)
    identity_type = db.Column(db.String(10), nullable=False)
    key_source = db.Column(db.String(20), nullable=False, default="uuid")
    key_attribute = db.Column(db.String(200), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="loading")
    page = db.Column(db.Integer, nullable=False, default=1)
    total = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    campaign = db.relationship("Campaign")


class TransferItem(db.Model):
    __table_args__ = (UniqueConstraint("job_id", "subscriber_id", name="uq_transfer_subscriber"),)
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(36), db.ForeignKey("transfer_job.id", ondelete="CASCADE"), nullable=False, index=True)
    subscriber_id = db.Column(db.Integer)
    recipient_id = db.Column(db.Integer, db.ForeignKey("recipient.id", ondelete="SET NULL"))
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="staged")
    message = db.Column(db.Text, nullable=False, default="")
