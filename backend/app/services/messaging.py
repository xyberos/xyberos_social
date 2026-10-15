"""Conversation membership, message payloads, and the in-process WebSocket fan-out hub."""

from hashlib import sha256
from uuid import UUID
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from app.models import ConversationMember, ConversationMessage, User
from app.schemas.chat import MessagePayload


class ConversationSocketHub:
    def __init__(self):
        self.connections: dict[UUID, list[tuple[WebSocket, UUID]]] = {}

    async def connect(self, conversation_id: UUID, user_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(conversation_id, []).append((websocket, user_id))

    def disconnect(self, conversation_id: UUID, websocket: WebSocket) -> None:
        active = self.connections.get(conversation_id, [])
        self.connections[conversation_id] = [
            (socket, user_id) for socket, user_id in active if socket is not websocket
        ]
        if not self.connections[conversation_id]:
            self.connections.pop(conversation_id, None)

    async def disconnect_user(self, conversation_id: UUID, user_id: UUID) -> None:
        for websocket, connected_user_id in list(self.connections.get(conversation_id, [])):
            if connected_user_id == user_id:
                try:
                    await websocket.close(code=4403)
                except (WebSocketDisconnect, RuntimeError):
                    pass
                self.disconnect(conversation_id, websocket)

    async def broadcast(self, conversation_id: UUID, event: dict) -> None:
        for websocket, _ in list(self.connections.get(conversation_id, [])):
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(conversation_id, websocket)


conversation_sockets = ConversationSocketHub()


def active_conversation_member(
    database: Session, conversation_id: UUID, user_id: UUID
) -> ConversationMember:
    member = database.get(ConversationMember, (conversation_id, user_id))
    if member is None or member.left_at is not None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return member


def direct_conversation_key(first_user_id: UUID, second_user_id: UUID) -> str:
    pair = sorted((str(first_user_id), str(second_user_id)))
    return sha256(":".join(pair).encode("utf-8")).hexdigest()


def message_payload(message: ConversationMessage, sender: User) -> dict:
    return MessagePayload(
        id=str(message.id),
        conversation_id=str(message.conversation_id),
        sender_id=str(sender.id),
        sender_name=sender.display_name,
        sender_username=sender.username,
        body=message.body,
        created_at=message.created_at.isoformat(),
    ).model_dump()
