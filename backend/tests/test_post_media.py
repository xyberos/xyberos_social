"""A post's media: what may be attached, who may read it, and what it becomes.

These tests hold the promises the composer makes. A file must be verified before a post can
name it; a post can be media alone, because a photo does not need a caption; the read rule of
the post governs its bytes, not the other way round; and a removed post takes its files with
it. Range reads are covered because a video is played by a browser that asks for one part at
a time.
"""

from uuid import UUID

import pytest
from sqlalchemy import select

from conftest import create_account, csrf_from
from test_attachments import FakeStorage, noisy_png_bytes, png_bytes

from app.core.config import Settings
from app.models import Attachment, User


def mp4_bytes() -> bytes:
    """An `ftyp` box is what makes a file an MP4; the rest of the container is not sniffed."""
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 32


def webm_bytes() -> bytes:
    return b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 32


def mp3_bytes() -> bytes:
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def wav_bytes() -> bytes:
    return b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 24


@pytest.fixture
def storage(monkeypatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr("app.services.attachments.get_storage", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def structural_scanner(monkeypatch):
    """The verdicts are the real structural ones; only the storage is faked."""
    from app.core.scanning import StructuralScanner

    scanner = StructuralScanner()
    monkeypatch.setattr("app.services.attachments.get_scanner", lambda: scanner)
    return scanner


def request_target(client, *, content_type="image/png", size=1024, filename="photo.png"):
    return client.post(
        "/posts/media",
        json={"filename": filename, "content_type": content_type, "size_bytes": size},
    )


def upload(client, storage, payload, *, content_type="image/png", filename="photo.png"):
    """Issue a target, put the bytes where the browser would, and finalize."""
    target = request_target(client, content_type=content_type, size=len(payload), filename=filename)
    assert target.status_code == 201, target.text
    body = target.json()
    storage.objects[body["fields"]["key"]] = payload
    finalized = client.post(f"/attachments/{body['attachment_id']}/finalize")
    assert finalized.status_code == 200, finalized.text
    return finalized.json()


def publish(client, *, body="", title="", audience="followers", attachment_ids=()):
    page = client.get("/posts/new")
    data = {
        "csrf_token": csrf_from(page.text),
        "title": title,
        "body": body,
        "audience": audience,
        "return_to": "/posts/new",
    }
    if attachment_ids:
        data["attachment_ids"] = list(attachment_ids)
    return client.post("/posts", data=data, follow_redirects=False)


def alice_and_bob(client_factory):
    alice = client_factory()
    bob = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    assert create_account(bob, "bob@example.com", "bob").status_code == 303
    return alice, bob


def follow(client, username: str) -> None:
    """Follow `username` from `client`, through the page that offers the button."""
    people = client.get("/people")
    import re

    match = re.search(
        rf'@\w+.*?@{re.escape(username)}.*?action="/people/([0-9a-f-]+)/follow"',
        people.text,
        re.S,
    )
    assert match, f"@{username} is not listed with a follow action"
    assert (
        client.post(
            f"/people/{match.group(1)}/follow",
            data={"csrf_token": csrf_from(people.text)},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_a_post_can_carry_a_photo(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    media = upload(alice, storage, png_bytes(), filename="sunset.png")
    published = publish(alice, body="A photo", attachment_ids=[media["id"]])

    assert published.status_code == 303
    page = alice.get(published.headers["location"])
    assert page.status_code == 200
    assert f'src="{media["download_url"]}"' in page.text
    assert 'alt="sunset.png"' in page.text


def test_a_post_can_be_media_alone(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    media = upload(alice, storage, png_bytes())
    published = publish(alice, attachment_ids=[media["id"]])

    # No text, no title: the photo is the update.
    assert published.status_code == 303
    page = alice.get(published.headers["location"])
    assert page.status_code == 200
    assert "<img" in page.text


def test_a_post_without_text_or_media_is_still_refused(client_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    refused = publish(alice)

    assert refused.status_code == 303
    assert refused.headers["location"].endswith("error=invalid-post")


def test_video_and_audio_render_as_players(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    video = upload(alice, storage, mp4_bytes(), content_type="video/mp4", filename="clip.mp4")
    track = upload(alice, storage, mp3_bytes(), content_type="audio/mpeg", filename="song.mp3")
    published = publish(alice, body="Both", attachment_ids=[video["id"], track["id"]])

    page = alice.get(published.headers["location"])
    assert video["kind"] == "video" and track["kind"] == "audio"
    assert "<video" in page.text and 'controls playsinline' in page.text
    assert "<audio" in page.text
    assert "song.mp3" in page.text


def test_webm_and_wav_are_accepted(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    webm = upload(alice, storage, webm_bytes(), content_type="video/webm", filename="clip.webm")
    wav = upload(alice, storage, wav_bytes(), content_type="audio/wav", filename="talk.wav")

    assert webm["kind"] == "video"
    assert wav["kind"] == "audio"


def test_a_declared_type_must_match_the_container(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    # A PNG announced as a video: the bytes decide, and they disagree.
    target = request_target(alice, content_type="video/mp4", size=1024, filename="fake.mp4")
    body = target.json()
    storage.objects[body["fields"]["key"]] = png_bytes()
    refused = alice.post(f"/attachments/{body['attachment_id']}/finalize")

    assert refused.status_code == 422
    assert body["fields"]["key"] not in storage.objects


def test_matroska_is_not_relabelled_as_webm(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    target = request_target(alice, content_type="video/webm", size=1024, filename="film.mkv")
    body = target.json()
    # EBML, but the DocType says matroska, which no browser plays from this pipeline.
    storage.objects[body["fields"]["key"]] = b"\x1a\x45\xdf\xa3\x42\x82\x88matroska" + b"\x00" * 32
    refused = alice.post(f"/attachments/{body['attachment_id']}/finalize")

    assert refused.status_code == 422


def test_video_has_its_own_ceiling(client_factory, storage, monkeypatch):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    monkeypatch.setattr(
        "app.services.attachments.get_settings",
        lambda: Settings(post_video_max_bytes=2048, post_image_max_bytes=1024),
    )

    refused = request_target(
        alice, content_type="video/mp4", size=4096, filename="long.mp4"
    )
    assert refused.status_code == 413
    assert "Videos are limited to" in refused.json()["detail"]

    # The same size is fine for a photo under the photo ceiling.
    allowed = request_target(alice, content_type="image/png", size=1024)
    assert allowed.status_code == 201


def test_a_file_cannot_be_attached_twice_or_by_someone_else(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    media = upload(alice, storage, png_bytes())

    twice = publish(alice, body="Twice", attachment_ids=[media["id"], media["id"]])
    assert twice.headers["location"].endswith("error=invalid-media")

    stolen = publish(bob, body="Not mine", attachment_ids=[media["id"]])
    assert stolen.headers["location"].endswith("error=invalid-media")

    # Neither attempt named the file, so the author can still publish it.
    assert publish(alice, body="Once", attachment_ids=[media["id"]]).status_code == 303


def test_a_chat_upload_cannot_be_walked_into_a_post(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    people = alice.get("/people")
    import re

    match = re.search(
        rf'@{re.escape("bob")}.*?action="/conversations/direct/([0-9a-f-]+)"', people.text, re.S
    )
    conversation_id = match.group(1)
    started = alice.post(
        f"/conversations/direct/{conversation_id}",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    conversation = started.headers["location"].rsplit("/", 1)[1]
    target = alice.post(
        f"/conversations/{conversation}/attachments",
        json={"filename": "photo.png", "content_type": "image/png", "size_bytes": 1024},
    ).json()
    storage.objects[target["fields"]["key"]] = png_bytes()
    finalized = alice.post(f"/attachments/{target['attachment_id']}/finalize").json()

    refused = publish(alice, body="Borrowed", attachment_ids=[finalized["id"]])

    assert refused.headers["location"].endswith("error=invalid-media")


def test_the_read_rule_of_the_post_governs_its_media(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    outsider = client_factory()
    assert create_account(outsider, "outsider@example.com", "outsider").status_code == 303

    media = upload(alice, storage, png_bytes())
    published = publish(alice, body="For followers", attachment_ids=[media["id"]])
    url = media["download_url"]

    # Followers only: a reader who does not follow the author cannot name the file.
    assert bob.get(url).status_code == 404
    follow(bob, "alice")
    assert bob.get(url).status_code == 200
    assert outsider.get(url).status_code == 404
    # And the same answer as an id that never existed.
    assert outsider.get("/attachments/00000000-0000-0000-0000-000000000000/download").status_code == 404
    assert alice.get(published.headers["location"]).status_code == 200


def test_public_media_is_readable_without_an_account(client_factory, storage):
    alice = client_factory()
    anonymous = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    media = upload(alice, storage, png_bytes())
    published = publish(alice, body="For the web", audience="public", attachment_ids=[media["id"]])
    assert published.status_code == 303
    assert anonymous.get(published.headers["location"]).status_code == 200
    assert anonymous.get(media["download_url"]).status_code == 200
    if media.get("thumbnail_url"):
        assert anonymous.get(media["thumbnail_url"]).status_code == 200

    # Narrowing the audience closes the bytes with it: the file follows the post, not a URL.
    post_id = published.headers["location"].rsplit("/", 1)[-1]
    workspace = alice.get("/workspace")
    assert (
        alice.post(
            f"/posts/{post_id}/audience",
            data={
                "csrf_token": csrf_from(workspace.text),
                "audience": "everyone",
                "return_to": "/workspace",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert anonymous.get(media["download_url"]).status_code == 404


def test_a_deactivated_author_takes_the_media_with_it(client_factory, database_factory, storage):
    alice = client_factory()
    anonymous = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    media = upload(alice, storage, png_bytes())
    published = publish(alice, body="For the web", audience="public", attachment_ids=[media["id"]])
    assert published.status_code == 303
    assert anonymous.get(media["download_url"]).status_code == 200

    # The page already refuses a deactivated account; its bytes must not outlive that verdict.
    with database_factory() as database:
        database.scalar(select(User).where(User.username == "alice")).is_active = False
        database.commit()

    assert anonymous.get(published.headers["location"]).status_code == 404
    assert anonymous.get(media["download_url"]).status_code == 404


def test_a_block_closes_the_media_too(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    media = upload(alice, storage, png_bytes())
    publish(alice, body="Public", audience="everyone", attachment_ids=[media["id"]])
    assert bob.get(media["download_url"]).status_code == 200

    people = bob.get("/people")
    import re

    alice_id = re.search(r'@alice.*?action="/people/([0-9a-f-]+)/', people.text, re.S).group(1)
    bob.post(
        f"/people/{alice_id}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )

    assert bob.get(media["download_url"]).status_code == 404


def test_a_removed_update_takes_its_media_out_of_reach(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    follow(bob, "alice")
    media = upload(alice, storage, png_bytes())
    published = publish(alice, body="Short lived", attachment_ids=[media["id"]])
    post_path = published.headers["location"]
    assert bob.get(media["download_url"]).status_code == 200

    page = alice.get(post_path)
    removed = alice.post(
        f"{post_path}/remove",
        data={"csrf_token": csrf_from(page.text), "return_to": "/workspace"},
        follow_redirects=False,
    )
    assert removed.status_code == 303

    assert bob.get(media["download_url"]).status_code == 404


def test_a_video_can_be_read_in_parts(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    payload = mp4_bytes()
    media = upload(alice, storage, payload, content_type="video/mp4", filename="clip.mp4")
    publish(alice, body="Seek", attachment_ids=[media["id"]])

    whole = alice.get(media["download_url"])
    assert whole.status_code == 200
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-type"] == "video/mp4"

    part = alice.get(media["download_url"], headers={"Range": "bytes=0-3"})
    assert part.status_code == 206
    assert part.content == payload[:4]
    assert part.headers["content-range"] == f"bytes 0-3/{len(payload)}"

    tail = alice.get(media["download_url"], headers={"Range": "bytes=-4"})
    assert tail.status_code == 206
    assert tail.content == payload[-4:]

    past_end = alice.get(media["download_url"], headers={"Range": f"bytes={len(payload) + 5}-"})
    assert past_end.status_code == 416


def test_the_owner_can_withdraw_a_draft(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    media = upload(alice, storage, png_bytes())
    key = next(iter(storage.objects))

    discarded = alice.delete(f"/posts/media/{media['id']}")

    assert discarded.status_code == 204
    assert key not in storage.objects
    assert alice.get(media["download_url"]).status_code == 404


def test_a_draft_is_private_until_it_is_published(client_factory, storage):
    alice, bob = alice_and_bob(client_factory)
    media = upload(alice, storage, png_bytes())

    assert alice.get(media["download_url"]).status_code == 200
    assert bob.get(media["download_url"]).status_code == 404
    assert bob.delete(f"/posts/media/{media['id']}").status_code == 404


def test_a_published_file_is_the_posts_business(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    media = upload(alice, storage, png_bytes())
    publish(alice, body="Kept", attachment_ids=[media["id"]])

    # Withdrawing the draft route cannot orphan the bytes of a post that names them.
    assert alice.delete(f"/posts/media/{media['id']}").status_code == 404
    assert alice.get(media["download_url"]).status_code == 200


def test_too_many_files_are_refused(client_factory, storage, monkeypatch):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    monkeypatch.setattr(
        "app.services.attachments.get_settings", lambda: Settings(post_media_limit=1)
    )

    first = upload(alice, storage, png_bytes())
    second = upload(alice, storage, png_bytes(), filename="second.png")

    refused = publish(alice, body="Two", attachment_ids=[first["id"], second["id"]])
    assert refused.headers["location"].endswith("error=invalid-media")


def test_a_media_post_is_named_in_the_places_that_list_posts(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    media = upload(alice, storage, png_bytes())
    publish(alice, attachment_ids=[media["id"]])

    # A listed post carries the photo itself as a tile, not only a link to the post.
    dashboard = alice.get("/dashboard")
    assert dashboard.status_code == 200
    assert "post-media-gallery post-media-count-1" in dashboard.text
    assert "data-gallery" in dashboard.text
    assert "data-gallery-item" in dashboard.text
    assert f'href="/attachments/{media["id"]}/download"' in dashboard.text

    workspace = alice.get("/workspace")
    assert workspace.status_code == 200
    assert "Photo update" in workspace.text
    assert "post-media-count-1" in workspace.text


def test_the_post_records_its_media(client_factory, storage, database_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    media = upload(alice, storage, png_bytes())
    published = publish(alice, body="Tracked", attachment_ids=[media["id"]])
    post_id = UUID(published.headers["location"].rsplit("/", 1)[1])

    with database_factory() as database:
        attachment = database.get(Attachment, UUID(media["id"]))
        assert attachment.post_id == post_id
        assert attachment.scope_type == "account"
        assert attachment.checksum_sha256


def test_a_photo_is_a_tile_that_loads_its_preview_and_links_the_original(
    client_factory, storage
):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    media = upload(alice, storage, noisy_png_bytes())
    published = publish(alice, body="One photo", attachment_ids=[media["id"]])

    page = alice.get(published.headers["location"])
    assert page.status_code == 200
    assert "post-media-gallery post-media-count-1" in page.text
    assert 'data-gallery' in page.text
    # The tile renders the preview and the link still names the original.
    assert f'src="/attachments/{media["id"]}/thumbnail"' in page.text
    assert f'href="/attachments/{media["id"]}/download"' in page.text
    assert f'data-full="/attachments/{media["id"]}/download"' in page.text
    # Without the viewer, the link opens the original in a new tab.
    assert 'target="_blank"' in page.text
    # The viewer script is loaded, under a versioned address.
    assert 'src="/static/js/gallery.js?v=' in page.text


def test_three_photos_lay_out_as_a_lead_tile_with_two_alongside(client_factory, storage):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    ids = [
        upload(alice, storage, png_bytes(), filename=f"photo-{index}.png")["id"]
        for index in range(3)
    ]
    published = publish(alice, body="Three photos", attachment_ids=ids)

    page = alice.get(published.headers["location"])
    assert page.status_code == 200
    assert "post-media-gallery post-media-count-3" in page.text
    assert page.text.count("data-gallery-item") == 3
    assert page.text.count("post-media-tile") == 3
