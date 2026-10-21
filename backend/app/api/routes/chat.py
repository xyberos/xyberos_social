"""Inbox-first messaging: direct and small-group conversations, plus the WebSocket."""

import re
from datetime import UTC, datetime
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import authenticated_form_user, authenticated_user, current_session, form_context
from app.api.responses import redirect_people, render
from app.core import events
from app.core.database import get_db
from app.core.scoping import conversation_scope
from app.models import (
    Attachment,
    Conversation,
    ConversationEvent,
    ConversationMember,
    ConversationMessage,
    User,
)
from app.core.security import check_rate_limit
from app.services import idempotency
from app.services.attachments import attach_to_message, attachment_payload
from app.services.authorization import is_blocked_between
from app.services.messaging import (
    active_conversation_member,
    conversation_sockets,
    direct_conversation_key,
    first_unread_message,
    message_payload,
    record_conversation_event,
    shell_sockets,
    unread_message_total,
)
from app.services.outbox import enqueue_event

router = APIRouter()


async def publish_unread_totals(
    database: Session, conversation_id: UUID, *, exclude_user_id: UUID | None = None
) -> None:
    """Send every participant their own total, so a badge is never a client-side guess."""
    recipient_ids = database.scalars(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.left_at.is_(None),
        )
    ).all()
    for recipient_id in recipient_ids:
        if recipient_id == exclude_user_id:
            continue
        await shell_sockets.send(
            recipient_id,
            {
                "type": "chat_unread",
                "total": unread_message_total(database, user_id=recipient_id),
            },
        )


@router.get("/conversations", response_class=HTMLResponse)
def conversation_inbox(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    rows = database.execute(
        select(Conversation, func.max(ConversationMessage.created_at))
        .join(
            ConversationMember,
            and_(
                ConversationMember.conversation_id == Conversation.id,
                ConversationMember.user_id == user.id,
                ConversationMember.left_at.is_(None),
            ),
        )
        .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(
            func.coalesce(func.max(ConversationMessage.created_at), Conversation.created_at).desc()
        )
        .limit(100)
    ).all()
    conversations = []
    for conversation, _ in rows:
        membership = database.get(ConversationMember, (conversation.id, user.id))
        member_rows = database.execute(
            select(ConversationMember, User)
            .join(User, User.id == ConversationMember.user_id)
            .where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.left_at.is_(None),
            )
        ).all()
        participants = [member_user for _, member_user in member_rows]
        peer = next((person for person in participants if person.id != user.id), None)
        latest = database.scalar(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(1)
        )
        unread_query = select(func.count(ConversationMessage.id)).where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.sender_id != user.id,
        )
        if membership.last_read_at is not None:
            unread_query = unread_query.where(
                ConversationMessage.created_at > membership.last_read_at
            )
        unread_count = database.scalar(unread_query) or 0
        blocked = bool(
            conversation.kind == "direct"
            and peer is not None
            and is_blocked_between(database, user.id, peer.id)
        )
        conversations.append(
            {
                "conversation": conversation,
                "peer": peer,
                "participants": participants,
                "latest": latest,
                "unread_count": unread_count,
                "blocked": blocked,
            }
        )
    browser_session = current_session(request, database)
    return render(
        request,
        "chat/inbox.html",
        {
            "user": user,
            "conversations": conversations,
            "csrf_token": browser_session.csrf_token,
        },
    )


@router.post("/conversations/direct/{target_id}")
def start_direct_conversation(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot message yourself.")
    if is_blocked_between(database, user.id, target.id):
        return redirect_people("A block prevents direct messages.")
    direct_key = direct_conversation_key(user.id, target.id)
    conversation = database.scalar(
        select(Conversation).where(Conversation.direct_key == direct_key)
    )
    if conversation is None:
        conversation = Conversation(kind="direct", direct_key=direct_key, created_by=user.id)
        database.add(conversation)
        database.flush()
        database.add_all(
            [
                ConversationMember(conversation_id=conversation.id, user_id=user.id, role="owner"),
                ConversationMember(
                    conversation_id=conversation.id, user_id=target.id, role="member"
                ),
            ]
        )
        enqueue_event(
            database,
            event=events.CONVERSATION_CREATED,
            aggregate_type="conversation",
            aggregate_id=conversation.id,
            scope=conversation_scope(conversation.id),
            payload={"kind": "direct", "created_by": str(user.id)},
        )
        try:
            database.commit()
        except IntegrityError:
            database.rollback()
            conversation = database.scalar(
                select(Conversation).where(Conversation.direct_key == direct_key)
            )
            if conversation is None:
                raise
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.get("/conversations/new", response_class=HTMLResponse)
def new_group_conversation_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(request, "chat/new.html", form_context(browser_session, user=user))


@router.post("/conversations/groups")
def create_group_conversation(
    request: Request,
    title: str = Form(),
    usernames: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    title = title.strip()
    requested_usernames = list(
        dict.fromkeys(
            username.lower() for username in re.split(r"[,\s]+", usernames.strip()) if username
        )
    )
    error = None
    if not title or len(title) > 80:
        error = "Group name must be 1–80 characters."
    elif not requested_usernames:
        error = "Add at least one participant by username."
    elif len(requested_usernames) > 19:
        error = "A group can have up to 20 participants, including you."
    elif user.username in requested_usernames:
        error = "You are already included in this group."
    participants = []
    if error is None:
        participants = database.scalars(
            select(User).where(
                User.username.in_(requested_usernames),
                User.is_active.is_(True),
            )
        ).all()
        found = {person.username for person in participants}
        if found != set(requested_usernames):
            error = "One or more usernames were not found."
        elif any(is_blocked_between(database, user.id, person.id) for person in participants):
            error = "A block prevents adding one of those participants."
    if error:
        return render(
            request,
            "chat/new.html",
            form_context(browser_session, user=user, error=error, title=title, usernames=usernames),
            status_code=422,
        )
    conversation = Conversation(kind="group", title=title, created_by=user.id)
    database.add(conversation)
    database.flush()
    database.add(ConversationMember(conversation_id=conversation.id, user_id=user.id, role="owner"))
    enqueue_event(
        database,
        event=events.CONVERSATION_CREATED,
        aggregate_type="conversation",
        aggregate_id=conversation.id,
        scope=conversation_scope(conversation.id),
        payload={"kind": "group", "created_by": str(user.id)},
    )
    for participant in participants:
        database.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=participant.id,
                role="member",
            )
        )
        record_conversation_event(
            database,
            conversation.id,
            event=events.CONVERSATION_MEMBER_ADDED,
            action="member_added",
            actor_user_id=user.id,
            subject_user_id=participant.id,
            details=f"@{participant.username} was added to the group",
            after_state={"role": "member"},
        )
    database.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(
    conversation_id: UUID,
    request: Request,
    before: UUID | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    membership = active_conversation_member(database, conversation_id, user.id)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    member_rows = database.execute(
        select(ConversationMember, User)
        .join(User, User.id == ConversationMember.user_id)
        .where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.left_at.is_(None),
        )
        .order_by(ConversationMember.joined_at)
    ).all()
    participants = [
        {"membership": member, "user": participant} for member, participant in member_rows
    ]
    peer = next(
        (participant for participant in participants if participant["user"].id != user.id),
        None,
    )
    blocked = bool(
        conversation.kind == "direct"
        and peer
        and is_blocked_between(database, user.id, peer["user"].id)
    )
    message_query = select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation.id
    )
    if before is not None:
        cursor_message = database.get(ConversationMessage, before)
        if cursor_message is None or cursor_message.conversation_id != conversation.id:
            raise HTTPException(status_code=404, detail="Message not found.")
        message_query = message_query.where(
            or_(
                ConversationMessage.created_at < cursor_message.created_at,
                and_(
                    ConversationMessage.created_at == cursor_message.created_at,
                    ConversationMessage.id < cursor_message.id,
                ),
            )
        )
    message_rows = database.scalars(
        message_query.order_by(
            ConversationMessage.created_at.desc(), ConversationMessage.id.desc()
        ).limit(101)
    ).all()
    has_older = len(message_rows) > 100
    message_rows = list(reversed(message_rows[:100]))
    messages = [
        {
            "message": message,
            "sender": database.get(User, message.sender_id),
            "mine": message.sender_id == user.id,
        }
        for message in message_rows
    ]
    message_attachments: dict[UUID, list] = {}
    if message_rows:
        attachment_rows = database.scalars(
            select(Attachment)
            .where(
                Attachment.message_id.in_([message.id for message in message_rows]),
                Attachment.scan_state == "scanned",
            )
            .order_by(Attachment.created_at)
        ).all()
        for attachment in attachment_rows:
            message_attachments.setdefault(attachment.message_id, []).append(
                attachment_payload(attachment)
            )
    peer_read_at = None
    if conversation.kind == "direct" and peer:
        peer_read_at = peer["membership"].last_read_at
    # Where the reading pane opens: the oldest message this visit has not seen yet. Taken
    # before the mark below moves, so it reflects what was unread when the page was asked for.
    unread_marker = first_unread_message(
        message_rows, user_id=user.id, read_at=membership.last_read_at
    )
    events = database.scalars(
        select(ConversationEvent)
        .where(ConversationEvent.conversation_id == conversation.id)
        .order_by(ConversationEvent.created_at.desc())
        .limit(25)
    ).all()
    membership.last_read_at = datetime.now(UTC)
    database.commit()
    browser_session = current_session(request, database)
    return render(
        request,
        "chat/conversation.html",
        {
            "user": user,
            "conversation": conversation,
            "conversation_id": str(conversation.id),
            "participants": participants,
            "peer": peer,
            "blocked": blocked,
            "messages": messages,
            "message_attachments": message_attachments,
            "has_older": has_older,
            "older_cursor": message_rows[0].id if has_older and message_rows else None,
            "first_unread_id": unread_marker.id if unread_marker else None,
            # Loading earlier messages keeps the pane at the boundary between the two windows
            # instead of jumping to the newest message.
            "anchor_message_id": (
                message_rows[-1].id if before is not None and message_rows else None
            ),
            "peer_read_at": peer_read_at,
            "events": events,
            "csrf_token": browser_session.csrf_token,
        },
    )


@router.post("/conversations/{conversation_id}/members")
async def invite_conversation_member(
    conversation_id: UUID,
    request: Request,
    username: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None or conversation.kind != "group" or conversation.created_by != user.id:
        raise HTTPException(status_code=404, detail="Group not found.")
    active_conversation_member(database, conversation.id, user.id)
    target = database.scalar(
        select(User).where(User.username == username.strip().lower(), User.is_active.is_(True))
    )
    if target is None:
        return RedirectResponse(
            f"/conversations/{conversation.id}?error=user-not-found", status_code=303
        )
    if is_blocked_between(database, user.id, target.id):
        return RedirectResponse(f"/conversations/{conversation.id}?error=blocked", status_code=303)
    membership = database.get(ConversationMember, (conversation.id, target.id))
    if membership is not None and membership.left_at is None:
        return RedirectResponse(
            f"/conversations/{conversation.id}?error=already-member", status_code=303
        )
    active_count = (
        database.scalar(
            select(func.count(ConversationMember.user_id)).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.left_at.is_(None),
            )
        )
        or 0
    )
    if active_count >= 20:
        return RedirectResponse(
            f"/conversations/{conversation.id}?error=group-full", status_code=303
        )
    if membership is None:
        membership = ConversationMember(
            conversation_id=conversation.id,
            user_id=target.id,
            role="member",
        )
        database.add(membership)
    else:
        membership.left_at = None
        membership.last_read_at = None
    record_conversation_event(
        database,
        conversation.id,
        event=events.CONVERSATION_MEMBER_ADDED,
        action="member_added",
        actor_user_id=user.id,
        subject_user_id=target.id,
        details=f"@{target.username} was added to the group",
        after_state={"role": "member"},
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_added",
            "subject_user_id": str(target.id),
            "details": f"@{target.username} was added to the group",
        },
    )
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/members/{target_id}/remove")
async def remove_conversation_member(
    conversation_id: UUID,
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None or conversation.kind != "group" or conversation.created_by != user.id:
        raise HTTPException(status_code=404, detail="Group not found.")
    active_conversation_member(database, conversation.id, user.id)
    target_membership = active_conversation_member(database, conversation.id, target_id)
    if target_membership.role == "owner":
        raise HTTPException(status_code=409, detail="The group owner cannot be removed.")
    target = database.get(User, target_id)
    target_membership.left_at = datetime.now(UTC)
    record_conversation_event(
        database,
        conversation.id,
        event=events.CONVERSATION_MEMBER_REMOVED,
        action="member_removed",
        actor_user_id=user.id,
        subject_user_id=target_id,
        details=f"@{target.username if target else 'member'} was removed from the group",
        before_state={"left_at": None},
        after_state={"left_at": "set"},
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_removed",
            "subject_user_id": str(target_id),
            "details": f"@{target.username if target else 'member'} was removed from the group",
        },
    )
    await conversation_sockets.disconnect_user(conversation.id, target_id)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.post("/conversations/{conversation_id}/leave")
async def leave_group_conversation(
    conversation_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    membership = active_conversation_member(database, conversation_id, user.id)
    if conversation is None or conversation.kind != "group":
        raise HTTPException(status_code=404, detail="Group not found.")
    if membership.role == "owner":
        raise HTTPException(status_code=409, detail="The group owner cannot leave.")
    membership.left_at = datetime.now(UTC)
    record_conversation_event(
        database,
        conversation.id,
        event=events.CONVERSATION_MEMBER_LEFT,
        action="member_left",
        actor_user_id=user.id,
        subject_user_id=user.id,
        details=f"@{user.username} left the group",
        before_state={"left_at": None},
        after_state={"left_at": "set"},
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_left",
            "subject_user_id": str(user.id),
            "details": f"@{user.username} left the group",
        },
    )
    await conversation_sockets.disconnect_user(conversation.id, user.id)
    return RedirectResponse("/conversations", status_code=303)


@router.post("/conversations/{conversation_id}/read")
def mark_conversation_read(
    conversation_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    membership = active_conversation_member(database, conversation_id, user.id)
    membership.last_read_at = datetime.now(UTC)
    database.commit()
    return {"status": "ok"}


@router.post("/conversations/{conversation_id}/messages")
async def send_conversation_message(
    conversation_id: UUID,
    request: Request,
    body: str = Form(default=""),
    csrf_token: str = Form(),
    idempotency_key: str | None = Form(default=None),
    attachment_ids: list[UUID] = Form(default=[]),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    membership = active_conversation_member(database, conversation_id, user.id)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.kind == "direct":
        recipient = database.scalar(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id != user.id,
                ConversationMember.left_at.is_(None),
            )
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if is_blocked_between(database, user.id, recipient):
            raise HTTPException(
                status_code=403, detail="Direct messaging is blocked between these accounts."
            )
    body = body.strip()
    if len(body) > 5000 or (not body and not attachment_ids):
        raise HTTPException(
            status_code=422,
            detail="A message needs text or an attachment (up to 5,000 characters of text).",
        )
    if not check_rate_limit(str(user.id), f"message:{conversation.id}", limit=60, seconds=60):
        raise HTTPException(status_code=429, detail="You are sending messages too quickly.")
    key = idempotency.presented_key(request, idempotency_key)
    reservation = None
    if key is not None:
        reservation = idempotency.begin(
            database,
            scope=idempotency.actor_scope(user.id),
            key=key,
            endpoint="send_conversation_message",
            fingerprint=idempotency.request_fingerprint(
                conversation.id, body, sorted(str(item) for item in attachment_ids)
            ),
        )
        if reservation.replay is not None:
            return idempotency.replay_response(reservation.replay)
    message = ConversationMessage(
        conversation_id=conversation.id,
        sender_id=user.id,
        body=body,
    )
    database.add(message)
    membership.last_read_at = datetime.now(UTC)
    database.flush()
    attachments = attach_to_message(
        database,
        message_id=message.id,
        conversation_id=conversation.id,
        user=user,
        attachment_ids=attachment_ids,
    )
    payload = message_payload(message, user, attachments)
    enqueue_event(
        database,
        event=events.MESSAGE_CREATED,
        aggregate_type="conversation_message",
        aggregate_id=message.id,
        scope=conversation_scope(conversation.id),
        payload={
            "conversation_id": str(conversation.id),
            "sender_id": str(user.id),
            "attachments": len(attachments),
        },
    )
    if reservation is not None:
        if request.headers.get("X-Chat-Request") == "true":
            idempotency.complete(database, reservation, status_code=200, body=payload)
        else:
            idempotency.complete(
                database,
                reservation,
                status_code=303,
                location=f"/conversations/{conversation.id}",
            )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {"type": "message", "message": payload},
    )
    await publish_unread_totals(database, conversation.id, exclude_user_id=user.id)
    if request.headers.get("X-Chat-Request") == "true":
        return JSONResponse(payload)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@router.websocket("/ws/inbox")
async def inbox_websocket(
    websocket: WebSocket,
    database: Session = Depends(get_db),
):
    """Push each signed-in reader their own unread total, on whatever page they are on."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if origin and origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
        await websocket.close(code=4403)
        return
    browser_session = current_session(websocket, database)
    if browser_session is None or browser_session.user_id is None:
        await websocket.close(code=4401)
        return
    user_id = browser_session.user_id
    await shell_sockets.connect(user_id, websocket)
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
            elif message == "refresh":
                total = unread_message_total(database, user_id=user_id)
                # The socket outlives any one request, so end the read transaction instead of
                # holding its snapshot open for the life of the connection.
                database.rollback()
                await websocket.send_json({"type": "chat_unread", "total": total})
    except WebSocketDisconnect:
        pass
    finally:
        shell_sockets.disconnect(user_id, websocket)


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_websocket(
    websocket: WebSocket,
    conversation_id: UUID,
    database: Session = Depends(get_db),
):
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if origin and origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
        await websocket.close(code=4403)
        return
    browser_session = current_session(websocket, database)
    if browser_session is None or browser_session.user_id is None:
        await websocket.close(code=4401)
        return
    member = database.get(ConversationMember, (conversation_id, browser_session.user_id))
    if member is None or member.left_at is not None:
        await websocket.close(code=4403)
        return
    await conversation_sockets.connect(conversation_id, browser_session.user_id, websocket)
    try:
        while True:
            heartbeat = await websocket.receive_text()
            if heartbeat == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        conversation_sockets.disconnect(conversation_id, websocket)
