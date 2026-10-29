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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar
from urllib.parse import urlparse

import boto3
from botocore.client import Config as BotoClientConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

READ_CHUNK_SIZE = 64 * 1024


class StorageError(RuntimeError):
    """Object storage is unavailable or refused the operation."""


_T = TypeVar("_T")


def error_code(error: ClientError) -> str:
    """The service's own error code, which is what tells two failures apart."""
    return str(error.response.get("Error", {}).get("Code", ""))


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
        cors_origins: Sequence[str] = (),
        auto_provision: bool = False,
    ) -> None:
        self.bucket = bucket
        self._cors_origins = tuple(cors_origins)
        # Healing is a development convenience, not a production behaviour: the local stand-in
        # keeps its state in memory, so restarting it deletes the bucket, while a real bucket is
        # an operator's decision that a request process must not quietly revisit.
        self._auto_provision = auto_provision
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=BotoClientConfig(
                signature_version="s3v4",
                # A storage service that accepts the connection and then never answers must not
                # hold a request, or the startup check, open indefinitely.
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 2},
                # MinIO and most self-hosted gateways only serve path-style requests.
                s3={"addressing_style": "path" if use_path_style else "auto"},
            ),
        )

    def ensure_bucket(self, cors_origins: Sequence[str] = ()) -> None:
        """Create the bucket when it is missing and apply the browser upload policy.

        A browser posts attachment bytes straight to this service, which is a cross-origin
        request: without a CORS policy naming the application's origin, every upload fails in
        the browser *after* the bytes have already been stored. Applying the policy is
        idempotent, so this is safe to run on every deploy.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self.bucket)
                logger.info("created bucket", extra={"event": "storage.bucket_created"})
            except (BotoCoreError, ClientError) as error:
                raise StorageError(f"could not create bucket {self.bucket!r}") from error
        except BotoCoreError as error:
            # The service itself is unreachable, which is a different failure from a missing
            # bucket and must not be mistaken for one.
            raise StorageError(f"could not reach object storage for {self.bucket!r}") from error
        if cors_origins:
            self.apply_cors(cors_origins)

    def apply_cors(self, origins: Sequence[str]) -> None:
        rules = [
            {
                "AllowedOrigins": list(origins),
                # PUT is what a multipart part uses; a dropped connection then costs one part
                # rather than the whole file.
                "AllowedMethods": ["POST", "PUT", "GET", "HEAD"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 300,
            }
        ]
        try:
            self._client.put_bucket_cors(
                Bucket=self.bucket, CORSConfiguration={"CORSRules": rules}
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageError("could not apply the bucket CORS policy") from error
        logger.info(
            "applied bucket CORS",
            extra={"event": "storage.cors_applied", "origins": len(list(origins))},
        )

    def _call(self, action: str, operation: Callable[[], _T]) -> _T:
        """Run one client operation, re-creating a vanished development bucket at most once.

        Startup provisions the bucket, which only helps a process that boots after the storage
        service is back. Healing here means the first request after either container restarts
        repairs the bucket instead of failing an upload that has nothing wrong with it.
        """
        try:
            return operation()
        except ClientError as error:
            if not (self._auto_provision and error_code(error) == "NoSuchBucket"):
                raise StorageError(action) from error
            self._provision()
            try:
                return operation()
            except (BotoCoreError, ClientError) as retried:
                raise StorageError(action) from retried
        except BotoCoreError as error:
            raise StorageError(action) from error

    def _provision(self) -> None:
        """Re-create a development bucket that has gone missing."""
        try:
            self.ensure_bucket(self._cors_origins)
        except StorageError:
            # The retried operation reports the outcome, so a failed heal neither masks it nor
            # turns one failure into a different one.
            pass

    def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int
    ) -> dict[str, Any]:
        """A short-lived, single-key upload target with the size ceiling enforced by S3."""
        return self._call(
            "could not issue an upload target",
            lambda: self._client.generate_presigned_post(
                Bucket=self.bucket,
                Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, max_bytes],
                ],
                ExpiresIn=expires_in,
            ),
        )

    def head(self, key: str) -> StoredObject | None:
        def fetch() -> dict[str, Any] | None:
            try:
                return self._client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as error:
                # A bucket that is present but holds no such object is a normal answer, and it
                # must not be mistaken for a bucket that needs re-creating.
                if error_code(error) in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise

        metadata = self._call(f"could not inspect object {key!r}", fetch)
        if metadata is None:
            return None
        return StoredObject(
            key=key,
            size=int(metadata.get("ContentLength", 0)),
            content_type=metadata.get("ContentType"),
        )

    def get_bytes(self, key: str) -> bytes:
        return self._call(
            f"could not read object {key!r}",
            lambda: self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read(),
        )

    def iter_chunks(
        self, key: str, *, chunk_size: int = READ_CHUNK_SIZE, start: int = 0, length: int | None = None
    ) -> Iterator[bytes]:
        """Yield an object's bytes without holding it: the only read path for large media.

        `start`/`length` ask the service for a byte range, which is what makes seeking in a
        video a range read rather than a re-download of everything before it.
        """
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if start or length is not None:
            end = "" if length is None else str(start + length - 1)
            kwargs["Range"] = f"bytes={start}-{end}"
        body = self._call(
            f"could not read object {key!r}",
            lambda: self._client.get_object(**kwargs)["Body"],
        )
        try:
            yield from body.iter_chunks(chunk_size)
        except (BotoCoreError, ClientError) as error:
            raise StorageError(f"could not stream object {key!r}") from error
        finally:
            body.close()

    def put_bytes(self, key: str, payload: bytes, *, content_type: str) -> None:
        self._call(
            f"could not write object {key!r}",
            lambda: self._client.put_object(
                Bucket=self.bucket, Key=key, Body=payload, ContentType=content_type
            ),
        )

    def copy_object(self, source_key: str, target_key: str, *, content_type: str) -> None:
        """Copy inside the storage service, so a deduplicated upload crosses no client link."""
        self._call(
            f"could not copy object {source_key!r}",
            lambda: self._client.copy_object(
                Bucket=self.bucket,
                Key=target_key,
                CopySource={"Bucket": self.bucket, "Key": source_key},
                ContentType=content_type,
                MetadataDirective="REPLACE",
            ),
        )

    def create_multipart(self, key: str, *, content_type: str) -> str:
        """Open an S3 multipart upload and return the id the client's parts belong to."""
        response = self._call(
            f"could not start a multipart upload for {key!r}",
            lambda: self._client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType=content_type
            ),
        )
        return response["UploadId"]

    def presign_part(
        self, key: str, upload_id: str, part_number: int, *, expires_in: int
    ) -> str:
        """A URL for one part. The part's ETag comes back from the PUT response."""
        return self._call(
            "could not issue a part upload target",
            lambda: self._client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_in,
            ),
        )

    def complete_multipart(
        self, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        self._call(
            f"could not complete the multipart upload for {key!r}",
            lambda: self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            ),
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Discard uncompleted parts. An upload that is already gone is not a failure."""

        def discard() -> None:
            try:
                self._client.abort_multipart_upload(
                    Bucket=self.bucket, Key=key, UploadId=upload_id
                )
            except ClientError as error:
                if error_code(error) in {"NoSuchUpload", "404", "NotFound"}:
                    return
                raise

        self._call(f"could not abort the multipart upload for {key!r}", discard)

    def delete(self, key: str) -> None:
        """Delete one object. A missing object is not an error: deletion is idempotent."""
        self._call(
            f"could not delete object {key!r}",
            lambda: self._client.delete_object(Bucket=self.bucket, Key=key),
        )

    def delete_many(self, keys: list[str]) -> None:
        if not keys:
            return
        self._call(
            "could not delete objects",
            lambda: self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            ),
        )


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
        cors_origins=browser_origins(),
        auto_provision=settings.app_env == "development",
    )


def browser_origins() -> list[str]:
    """The origins a browser posts uploads from, which the bucket's CORS policy must name."""
    settings = get_settings()
    origins = {settings.app_base_url.rstrip("/")}
    if settings.app_env != "production":
        parsed = urlparse(settings.app_base_url)
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            port = f":{parsed.port}" if parsed.port else ""
            # A development server is reached as both loopback names, and a mismatch between
            # the configured origin and the one in the address bar silently blocks uploads.
            origins.add(f"{parsed.scheme}://localhost{port}")
            origins.add(f"{parsed.scheme}://127.0.0.1{port}")
    return sorted(origins)


def bootstrap_bucket() -> list[str]:
    """Create the bucket and allow browser uploads from the application's own origin."""
    origins = browser_origins()
    get_storage().ensure_bucket(origins)
    return origins
