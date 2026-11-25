from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


@dataclass
class User:
    username: str
    tag: str = ""  # unique login handle, lowercase, without "@"
    password_hash: str = ""  # salted scrypt hash, see security.py
    blocked: list[str] = field(default_factory=list)  # ids this user blocked
    is_bot: bool = False  # built-in bots cannot log in
    id: str = field(default_factory=new_id)


@dataclass
class Conversation:
    participants: list[str]  # user ids
    title: str | None = None
    admin_id: str | None = None  # group admin; None for direct chats (and old groups)
    reads: dict[str, str] | None = None  # user id -> UTC time of the last read (ISO 8601)
    id: str = field(default_factory=new_id)

    @property
    def is_group(self) -> bool:
        return self.admin_id is not None or len(self.participants) > 2

    @property
    def admin(self) -> str | None:
        """Group admin; old groups without a saved admin fall back to the first member."""
        if not self.is_group:
            return None
        return self.admin_id or self.participants[0]


@dataclass
class Message:
    conversation_id: str
    sender_id: str
    text: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)  # always UTC
    image: str | None = None  # file name inside the uploads dir
