"""S3-compatible object storage for attachment bytes.

Attachment bytes never live in PostgreSQL: the database stores metadata rows and the object
key, and every byte lives here (Decision 15). Nothing in this module decides who may read an
object — authorization happens in `services/attachments.py` before a byte is fetched, and
possession of a key or a URL is never treated as authority.

Browser uploads use a presigned POST policy rather than a presigned PUT so a conforming
storage service enforces the size ceiling before the bytes are stored, rather than only after
they have arrived. Not every S3-compatible service honours the `content-length-range`
condition — the moto stand-in used for local development accepts an oversize upload — so
`finalize` re-checks the stored size and deletes anything over the limit. The application
never relies on the policy condition alone.

The policy fixes the key, so a client cannot choose where its upload lands.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config as BotoClientConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Object storage is unavailable or refused the operation."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    content_type: str | None


class ObjectStorage:
    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
        use_path_style: bool,
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=BotoClientConfig(
                signature_version="s3v4",
                # MinIO and most self-hosted gateways only serve path-style requests.
                s3={"addressing_style": "path" if use_path_style else "auto"},
            ),
        )

    def ensure_bucket(self) -> None:
        """Create the bucket when it is missing. Intended for local and CI setups."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self.bucket)
                logger.info("created bucket", extra={"event": "storage.bucket_created"})
            except (BotoCoreError, ClientError) as error:
                raise StorageError(f"could not create bucket {self.bucket!r}") from error

    def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int
    ) -> dict[str, Any]:
        """A short-lived, single-key upload target with the size ceiling enforced by S3."""
        try:
            return self._client.generate_presigned_post(
                Bucket=self.bucket,
                Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, max_bytes],
                ],
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageError("could not issue an upload target") from error

    def head(self, key: str) -> StoredObject | None:
        try:
            metadata = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise StorageError(f"could not inspect object {key!r}") from error
        except BotoCoreError as error:
            raise StorageError(f"could not inspect object {key!r}") from error
        return StoredObject(
            key=key,
            size=int(metadata.get("ContentLength", 0)),
            content_type=metadata.get("ContentType"),
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"could not read object {key!r}") from error

    def put_bytes(self, key: str, payload: bytes, *, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=payload, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"could not write object {key!r}") from error

    def delete(self, key: str) -> None:
        """Delete one object. A missing object is not an error: deletion is idempotent."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"could not delete object {key!r}") from error

    def delete_many(self, keys: list[str]) -> None:
        if not keys:
            return
        try:
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageError("could not delete objects") from error


@lru_cache
def get_storage() -> ObjectStorage:
    settings = get_settings()
    return ObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        use_path_style=settings.s3_use_path_style,
    )
