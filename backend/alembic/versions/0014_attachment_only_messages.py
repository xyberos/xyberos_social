"""Allow an attachment to be the whole message.

A message may now carry only an attachment, so the body-length check permits zero
characters. The route still refuses a message that has neither text nor an attachment.

Revision ID: 0014_attachment_only_messages
Revises: 0013_post_audience
Create Date: 2026-10-21
"""

from alembic import op

revision = "0014_attachment_only_messages"
down_revision = "0013_post_audience"
branch_labels = None
depends_on = None

_BODY_LENGTH = "length(trim(body)) <= 5000"
_BODY_LENGTH_WITH_TEXT = "length(trim(body)) BETWEEN 1 AND 5000"


def upgrade() -> None:
    op.drop_constraint("ck_message_body_length", "conversation_messages", type_="check")
    op.create_check_constraint("ck_message_body_length", "conversation_messages", _BODY_LENGTH)


def downgrade() -> None:
    # Fails if an attachment-only message exists: narrowing the check would discard it.
    op.drop_constraint("ck_message_body_length", "conversation_messages", type_="check")
    op.create_check_constraint(
        "ck_message_body_length", "conversation_messages", _BODY_LENGTH_WITH_TEXT
    )
