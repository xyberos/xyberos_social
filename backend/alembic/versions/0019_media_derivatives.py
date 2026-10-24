"""Derived thumbnails, deduplicated copies, and multipart uploads.

Three additions that all reduce what travels over the wire:

* `thumbnail_key` points at a small WebP derivative generated from the verified bytes, so a
  preview costs kilobytes instead of the original. It is nullable because an image too small to
  benefit gets no derivative, and because the column must exist for rows written before it.
* `checksum_sha256` becomes an index, so a repeat upload of a file the account already stored
  can be answered with a server-side copy instead of a second transfer from the browser.
* `multipart_upload_id` records an in-progress S3 multipart upload so the app can complete or
  abort it, and so an abandoned one does not linger as unreachable parts.

Revision ID: 0019_media_derivatives
Revises: 0018_post_media
Create Date: 2026-10-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_media_derivatives"
down_revision = "0018_post_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attachments", sa.Column("thumbnail_key", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "attachments", sa.Column("multipart_upload_id", sa.String(length=255), nullable=True)
    )
    # The digest of the bytes the server received, before any normalization. It is what a
    # client's own digest of the same file can be matched against.
    op.add_column(
        "attachments",
        sa.Column("source_checksum_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_attachments_owner_source_checksum",
        "attachments",
        ["owner_user_id", "source_checksum_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_owner_source_checksum", table_name="attachments")
    op.drop_column("attachments", "source_checksum_sha256")
    op.drop_column("attachments", "multipart_upload_id")
    op.drop_column("attachments", "thumbnail_key")
