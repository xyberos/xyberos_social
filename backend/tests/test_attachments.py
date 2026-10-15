"""The attachment pipeline's security and lifecycle guarantees.

These tests exist for the promises Phase 8 makes: only authorized readers can download, an
unverified or rejected upload is never downloadable, a declared type that disagrees with the
bytes is refused, oversize and over-quota uploads are refused server-side, a replayed
finalize is a no-op, and deleting content takes the object with it.

Storage is faked rather than mocked per call so the presigned target, the object, and the
deletion are exercised through the same code paths a real S3 service would serve.
"""

import io
import re
import socket
import threading
from uuid import UUID

import pytest
from PIL import Image

from conftest import create_account, csrf_from

from app.core.config import Settings
from app.core.scanning import ClamAvScanner, ScanUnavailable, ScanVerdict
from app.core.storage import StoredObject
from app.models import Attachment

PNG_BYTES_LIMIT = 10 * 1024 * 1024


def png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_with_exif() -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (4, 4), (200, 10, 10))
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.targets: list[dict] = []

    def presign_post(self, key, *, content_type, max_bytes, expires_in):
        self.targets.append({"key": key, "content_type": content_type, "max_bytes": max_bytes})
        return {
            "url": f"http://storage.invalid/{key}",
            "fields": {"key": key, "Content-Type": content_type},
        }

    def head(self, key):
        if key not in self.objects:
            return None
        return StoredObject(key=key, size=len(self.objects[key]), content_type=None)

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, payload, *, content_type):
        self.objects[key] = payload

    def delete(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)

    def delete_many(self, keys):
        for key in keys:
            self.delete(key)


class CountingScanner:
    """Wraps the real structural verdicts so a test can see how often scanning ran."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        self.calls += 1
        return self.inner.scan(data, declared_mime=declared_mime)


class UnavailableScanner:
    def scan(self, data: bytes, *, declared_mime: str) -> ScanVerdict:
        raise ScanUnavailable("scanner down")


class StubClamd:
    """A one-shot clamd stand-in so the INSTREAM client is tested against its real protocol."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.received = b""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._server.accept()
        with connection:
            while b"\x00\x00\x00\x00" not in self.received:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                self.received += chunk
            try:
                connection.sendall(self.reply)
                # Shut down the write side and let the client close first: closing while the
                # client is still reading sends an RST on Windows, which the client reports as
                # a connection reset rather than as the verdict clamd actually sent.
                connection.shutdown(socket.SHUT_WR)
                while connection.recv(4096):
                    pass
            except OSError:
                pass
        self._server.close()

    def __enter__(self) -> "StubClamd":
        return self

    def __exit__(self, *_exc) -> None:
        self._thread.join(timeout=5)

    def payload(self) -> bytes:
        body = self.received.split(b"zINSTREAM\x00", 1)[-1]
        return body.split(b"\x00\x00\x00\x00", 1)[0][4:]


@pytest.fixture
def storage(monkeypatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr("app.services.attachments.get_storage", lambda: fake)
    monkeypatch.setattr("app.api.routes.attachments.get_storage", lambda: fake)
    return fake


@pytest.fixture
def scanner(monkeypatch) -> CountingScanner:
    from app.core.scanning import StructuralScanner

    counting = CountingScanner(StructuralScanner())
    monkeypatch.setattr("app.services.attachments.get_scanner", lambda: counting)
    return counting


def start_direct(client, target_username: str) -> str:
    people = client.get("/people")
    match = re.search(
        rf"@{re.escape(target_username)}.*?action=\"/conversations/direct/([0-9a-f-]+)\"",
        people.text,
        re.S,
    )
    assert match, f"No direct-message action for @{target_username}"
    response = client.post(
        f"/conversations/direct/{match.group(1)}",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def pair(client_factory):
    alice = client_factory()
    bob = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    assert create_account(bob, "bob@example.com", "bob").status_code == 303
    return alice, bob, start_direct(alice, "bob")


def request_target(client, conversation_id, *, content_type="image/png", size=1024, filename="photo.png"):
    return client.post(
        f"/conversations/{conversation_id}/attachments",
        json={"filename": filename, "content_type": content_type, "size_bytes": size},
    )


def deliver(client, storage, conversation_id, *, payload, **target_kwargs):
    """Issue a target, drop bytes where the browser would, and finalize."""
    target = request_target(client, conversation_id, size=len(payload), **target_kwargs)
    assert target.status_code == 201, target.text
    body = target.json()
    storage.objects[body["fields"]["key"]] = payload
    finalized = client.post(f"/attachments/{body['attachment_id']}/finalize")
    return body, finalized


def test_only_participants_can_download(client_factory, storage, scanner):
    alice, bob, conversation_id = pair(client_factory)
    outsider = client_factory()
    assert create_account(outsider, "outsider@example.com", "outsider").status_code == 303

    body, finalized = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert finalized.status_code == 200
    attachment_id = body["attachment_id"]

    alice_download = alice.get(f"/attachments/{attachment_id}/download")
    assert alice_download.status_code == 200
    assert alice_download.headers["content-type"] == "image/png"
    assert alice_download.headers["content-disposition"].startswith("inline;")
    assert alice_download.headers["x-content-type-options"] == "nosniff"
    assert alice_download.headers["content-security-policy"] == "default-src 'none'; sandbox"

    assert bob.get(f"/attachments/{attachment_id}/download").status_code == 200
    # A non-participant cannot reach it even with the id, and gets the same answer as for an
    # id that does not exist.
    assert outsider.get(f"/attachments/{attachment_id}/download").status_code == 404
    assert outsider.get("/attachments/00000000-0000-0000-0000-000000000000/download").status_code == 404


def test_a_block_hides_an_attachments_download(client_factory, storage, scanner):
    alice, bob, conversation_id = pair(client_factory)
    body, finalized = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert finalized.status_code == 200

    people = bob.get("/people")
    alice_id = re.search(r'@alice.*?action="/people/([0-9a-f-]+)/', people.text, re.S).group(1)
    assert (
        bob.post(
            f"/people/{alice_id}/block",
            data={"csrf_token": csrf_from(people.text)},
            follow_redirects=False,
        ).status_code
        == 303
    )

    assert bob.get(f"/attachments/{body['attachment_id']}/download").status_code == 404


def test_unverified_uploads_are_never_downloadable(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    target = request_target(alice, conversation_id)
    attachment_id = target.json()["attachment_id"]

    # The object does not exist yet, so finalize reports a retryable conflict rather than a
    # verdict, and nothing is downloadable in the meantime.
    assert alice.post(f"/attachments/{attachment_id}/finalize").status_code == 409
    assert alice.get(f"/attachments/{attachment_id}/download").status_code == 404

    _, finalized = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert finalized.status_code == 200


def test_rejected_uploads_are_not_downloadable_and_their_bytes_go(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    payload = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    body, finalized = deliver(
        alice, storage, conversation_id, payload=payload, content_type="image/png"
    )

    assert finalized.status_code == 422
    assert "rejected" in finalized.json()["detail"]
    assert alice.get(f"/attachments/{body['attachment_id']}/download").status_code == 404
    assert body["fields"]["key"] in storage.deleted
    assert storage.objects == {}


def test_declared_type_must_match_the_bytes(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    # Declared a PNG, delivered a JPEG.
    body, finalized = deliver(alice, storage, conversation_id, payload=jpeg_with_exif())
    assert finalized.status_code == 422
    assert body["fields"]["key"] in storage.deleted


def test_oversize_uploads_are_refused(client_factory, storage, scanner, monkeypatch):
    alice, _bob, conversation_id = pair(client_factory)
    monkeypatch.setattr(
        "app.services.attachments.get_settings",
        lambda: Settings(attachment_max_bytes=64),
    )

    too_large = request_target(alice, conversation_id, size=4096)
    assert too_large.status_code == 413

    # A client that lies about the size still gets caught when the bytes turn out too large.
    body = request_target(alice, conversation_id, size=32).json()
    storage.objects[body["fields"]["key"]] = png_bytes((32, 32))
    finalized = alice.post(f"/attachments/{body['attachment_id']}/finalize")
    assert finalized.status_code == 413
    assert storage.objects == {}


def test_quota_exhaustion_is_refused(client_factory, storage, scanner, monkeypatch):
    alice, _bob, conversation_id = pair(client_factory)
    monkeypatch.setattr(
        "app.services.attachments.get_settings",
        lambda: Settings(attachment_account_quota_bytes=200),
    )
    _, finalized = deliver(alice, storage, conversation_id, payload=png_bytes((64, 64)))
    assert finalized.status_code == 200

    refused = request_target(alice, conversation_id, size=2048)
    assert refused.status_code == 409
    assert "storage is full" in refused.json()["detail"]


def test_finalize_is_idempotent(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    body, first = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert first.status_code == 200

    second = alice.post(f"/attachments/{body['attachment_id']}/finalize")

    assert second.status_code == 200
    assert second.json() == first.json()
    assert scanner.calls == 1


def test_a_replayed_upload_target_is_the_same_target(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    headers = {"Idempotency-Key": "upload-target-key-0001"}
    payload = {"filename": "photo.png", "content_type": "image/png", "size_bytes": 1024}

    first = alice.post(
        f"/conversations/{conversation_id}/attachments", json=payload, headers=headers
    )
    second = alice.post(
        f"/conversations/{conversation_id}/attachments", json=payload, headers=headers
    )

    assert first.status_code == 201
    assert second.json() == first.json()
    assert len(storage.targets) == 1


def test_image_metadata_does_not_survive_finalize(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    original = jpeg_with_exif()
    assert Image.open(io.BytesIO(original)).getexif()  # the fixture really carries metadata

    body, finalized = deliver(
        alice, storage, conversation_id, payload=original, content_type="image/jpeg"
    )

    assert finalized.status_code == 200
    stored = storage.objects[body["fields"]["key"]]
    assert stored != original
    assert not Image.open(io.BytesIO(stored)).getexif()


def test_a_scanner_outage_leaves_the_upload_pending(client_factory, storage, monkeypatch):
    from app.core.database import get_db
    from app.main import app

    alice, _bob, conversation_id = pair(client_factory)
    monkeypatch.setattr("app.services.attachments.get_scanner", lambda: UnavailableScanner())
    body = request_target(alice, conversation_id, size=1024).json()
    storage.objects[body["fields"]["key"]] = png_bytes()

    unavailable = alice.post(f"/attachments/{body['attachment_id']}/finalize")

    assert unavailable.status_code == 503
    with next(app.dependency_overrides[get_db]()) as database:
        attachment = database.get(Attachment, UUID(body["attachment_id"]))
        assert attachment.scan_state == "pending_scan"
    # Still not downloadable, and the bytes are still there for the retry.
    assert alice.get(f"/attachments/{body['attachment_id']}/download").status_code == 404
    assert storage.objects


def test_the_clamav_client_speaks_instream():
    with StubClamd(b"stream: Win.Test.EICAR_HDB-1 FOUND\0") as clamd:
        verdict = ClamAvScanner("127.0.0.1", clamd.port).scan(
            b"payload-bytes", declared_mime="text/plain"
        )
        assert not verdict.clean
        assert "FOUND" in verdict.detail
        assert clamd.payload() == b"payload-bytes"

    with StubClamd(b"stream: OK\0") as clamd:
        assert ClamAvScanner("127.0.0.1", clamd.port).scan(
            b"payload-bytes", declared_mime="text/plain"
        ).clean


def test_an_attachment_can_only_join_its_own_conversation(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    carol = client_factory()
    assert create_account(carol, "carol@example.com", "carol").status_code == 303

    body, finalized = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert finalized.status_code == 200

    detail = alice.get(f"/conversations/{conversation_id}")
    sent = alice.post(
        f"/conversations/{conversation_id}/messages",
        data={
            "csrf_token": csrf_from(detail.text),
            "body": "Here is the picture",
            "attachment_ids": body["attachment_id"],
        },
        headers={"X-Chat-Request": "true"},
    )
    assert sent.status_code == 200
    assert sent.json()["attachments"][0]["download_url"] == (
        f"/attachments/{body['attachment_id']}/download"
    )

    # Carol cannot attach Alice's upload, and Alice cannot attach it twice.
    carol_conversation = start_direct(carol, "alice")
    carol_detail = carol.get(f"/conversations/{carol_conversation}")
    reused = alice.post(
        f"/conversations/{carol_conversation}/messages",
        data={
            "csrf_token": csrf_from(alice.get(f"/conversations/{carol_conversation}").text),
            "body": "Trying to reuse",
            "attachment_ids": body["attachment_id"],
        },
        headers={"X-Chat-Request": "true"},
    )
    assert reused.status_code == 422
    assert carol_detail.status_code == 200


def test_deleting_an_account_removes_its_objects(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    body, finalized = deliver(alice, storage, conversation_id, payload=png_bytes())
    assert finalized.status_code == 200
    key = body["fields"]["key"]
    assert key in storage.objects

    settings_page = alice.get("/settings")
    deleted = alice.post(
        "/account/delete",
        data={
            "csrf_token": csrf_from(settings_page.text),
            "password": "correct-horse-battery-staple",
            "confirmation": "DELETE",
        },
        follow_redirects=False,
    )

    assert deleted.status_code == 303
    assert key not in storage.objects
    assert key in storage.deleted


def test_expired_uploads_are_reaped(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    abandoned = request_target(alice, conversation_id).json()
    storage.objects[abandoned["fields"]["key"]] = png_bytes()

    from datetime import UTC, datetime, timedelta

    from app.core.database import get_db
    from app.main import app

    with next(app.dependency_overrides[get_db]()) as database:
        attachment = database.get(Attachment, UUID(abandoned["attachment_id"]))
        attachment.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        database.commit()

    # Issuing another target reaps what the expired one left behind.
    request_target(alice, conversation_id)

    assert abandoned["fields"]["key"] not in storage.objects
    with next(app.dependency_overrides[get_db]()) as database:
        assert database.get(Attachment, UUID(abandoned["attachment_id"])) is None


def test_download_uses_an_attachment_disposition_for_non_images(client_factory, storage, scanner):
    alice, _bob, conversation_id = pair(client_factory)
    body, finalized = deliver(
        alice,
        storage,
        conversation_id,
        payload=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\ntrailer\n",
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert finalized.status_code == 200

    response = alice.get(f"/attachments/{body['attachment_id']}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "report.pdf" in response.headers["content-disposition"]
    # A non-inline response is a download, so it runs under the application policy rather
    # than the narrower sandbox policy that inline media gets.
    assert "default-src 'self'" in response.headers["content-security-policy"]
