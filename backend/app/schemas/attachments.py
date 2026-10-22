"""JSON contracts for the attachment endpoints."""

from pydantic import BaseModel, Field


class UploadTargetRequest(BaseModel):
    """What the client intends to upload. Every field is a claim until finalize verifies it."""

    filename: str = Field(default="", max_length=200)
    content_type: str = Field(max_length=120)
    size_bytes: int = Field(gt=0)


class UploadTargetResponse(BaseModel):
    """A short-lived, single-key upload target with the size ceiling enforced by storage."""

    attachment_id: str
    upload_url: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int
    max_attachments: int


class AttachmentPayload(BaseModel):
    """Verified attachment metadata, safe to render because the scan already passed."""

    id: str
    filename: str
    content_type: str
    size_bytes: int
    inline: bool
    # One of image, video, audio, document: what element the reader's page renders.
    kind: str = "document"
    expired: bool = False
    download_url: str
