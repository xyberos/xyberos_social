"""JSON contract for chat messages."""

from pydantic import BaseModel


class MessagePayload(BaseModel):
    """The message shape returned by the send endpoint and broadcast over the WebSocket."""

    id: str
    conversation_id: str
    sender_id: str
    sender_name: str
    sender_username: str
    body: str
    created_at: str
