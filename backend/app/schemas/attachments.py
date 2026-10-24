"""JSON contracts for the attachment endpoints."""

from pydantic import BaseModel, Field, field_validator


class UploadTargetRequest(BaseModel):
    """What the client intends to upload. Every field is a claim until finalize verifies it."""

    filename: str = Field(default="", max_length=200)
    content_type: str = Field(max_length=120)
    size_bytes: int = Field(gt=0)
    # The SHA-256 of the file the client is about to send, when it computed one. It is only a
    # lookup key for deduplication; finalize computes the authoritative digest itself.
    checksum_sha256: str | None = Field(default=None, max_length=64)

    @field_validator("checksum_sha256")
    @classmethod
    def _checksum_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
            return None
        return candidate


class MultipartPlanPayload(BaseModel):
    """How a large file should be split, so the client can start or resume a part session."""

    part_size: int
    part_count: int


class UploadTargetResponse(BaseModel):
    """A short-lived, single-key upload target with the size ceiling enforced by storage."""

    attachment_id: str
    upload_url: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int
    max_attachments: int
    # True when the bytes were copied server-side from the account's own earlier upload, and
    # the client therefore has nothing to send.
    reused: bool = False
    multipart: MultipartPlanPayload | None = None


class MultipartPartPayload(BaseModel):
    number: int
    offset: int
    size: int
    url: str


class MultipartSessionResponse(BaseModel):
    upload_id: str
    part_size: int
    expires_in: int
    parts: list[MultipartPartPayload]


class MultipartPartUpload(BaseModel):
    """One completed part, as the storage service reported it in the PUT response."""

    number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=200)


class MultipartCompleteRequest(BaseModel):
    parts: list[MultipartPartUpload] = Field(min_length=1)


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
    # A small preview when one exists, so a page of media does not cost a page of originals.
    thumbnail_url: str | None = None
