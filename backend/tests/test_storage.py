"""What object storage does when the development stand-in loses its bucket.

The local stand-in keeps its state in memory, so restarting it deletes the bucket while the
application keeps running. These tests pin both halves of the answer: a development client
re-creates the bucket and retries the operation, and a production client reports the failure
instead of quietly deciding that a bucket should exist.
"""

import pytest
from botocore.stub import Stubber

from app.core.storage import ObjectStorage, StorageError


def make_storage(*, auto_provision: bool) -> ObjectStorage:
    return ObjectStorage(
        endpoint_url="http://localhost:9000",
        bucket="social-attachments",
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
        use_path_style=True,
        cors_origins=("http://localhost:8000",),
        auto_provision=auto_provision,
    )


def test_a_vanished_development_bucket_is_recreated_and_the_write_retried():
    storage = make_storage(auto_provision=True)
    stubber = Stubber(storage._client)
    with stubber:
        stubber.add_client_error(
            "put_object", service_error_code="NoSuchBucket", http_status_code=404
        )
        stubber.add_response("head_bucket", {})
        stubber.add_response("put_bucket_cors", {})
        stubber.add_response("put_object", {})

        storage.put_bytes("avatars/1.png", b"bytes", content_type="image/png")

    stubber.assert_no_pending_responses()


def test_a_missing_key_is_not_mistaken_for_a_missing_bucket():
    storage = make_storage(auto_provision=True)
    stubber = Stubber(storage._client)
    with stubber:
        stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

        assert storage.head("avatars/missing.png") is None

    stubber.assert_no_pending_responses()


def test_production_reports_a_missing_bucket_instead_of_creating_one():
    storage = make_storage(auto_provision=False)
    stubber = Stubber(storage._client)
    with stubber:
        stubber.add_client_error(
            "put_object", service_error_code="NoSuchBucket", http_status_code=404
        )

        with pytest.raises(StorageError):
            storage.put_bytes("avatars/1.png", b"bytes", content_type="image/png")

    stubber.assert_no_pending_responses()
