import hashlib
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import bot
from .models import Conversation, Message, User, new_id, utcnow
from .security import hash_password, verify_password
from .storage import SqliteStorage
from .validation import (
    MAX_MESSAGE_LENGTH,
    AuthError,
    ValidationError,
    validate_image,
    validate_message_text,
    validate_password,
    validate_title,
    validate_tag,
    validate_username,
)


class MessengerService:
    """Business logic. Loads data once at startup, saves on every change."""

    def __init__(self, storage: SqliteStorage) -> None:
        self.storage = storage
        data = storage.load()
        self.users = [User(**u) for u in data["users"]]
        self.conversations = [Conversation(**c) for c in data["conversations"]]
        self.messages = [
            Message(**{**m, "created_at": datetime.fromisoformat(m["created_at"])})
            for m in data["messages"]
        ]
        # chats saved before read markers existed start as fully read, not as "all unread"
        now = utcnow().isoformat()
        for conv in self.conversations:
            if conv.reads is None:
                conv.reads = {p: now for p in conv.participants}
        # session tokens are stored only as SHA-256 hashes: {hash: user_id}
        self.sessions: dict[str, str] = dict(data.get("sessions", {}))
        self._ensure_bot()

    @property
    def uploads_dir(self) -> Path:
        return self.storage.path.parent / "uploads"

    def _ensure_bot(self) -> None:
        if not any(u.is_bot for u in self.users):
            # "!" is not a valid hash, so nobody can ever log in as the bot
            self.users.append(
                User(username=bot.BOT_NAME, tag=bot.BOT_TAG, password_hash="!", is_bot=True)
            )
            self._save()

    @property
    def bot_user(self) -> User:
        return next(u for u in self.users if u.is_bot)

    def is_bot_chat(self, conversation_id: str) -> bool:
        conv = next((c for c in self.conversations if c.id == conversation_id), None)
        return conv is not None and not conv.is_group and self.bot_user.id in conv.participants

    def ensure_bot_chat(self, user_id: str) -> None:
        """Every real user gets a direct chat with the bot (created once, with a greeting)."""
        bot_id = self.bot_user.id
        if user_id == bot_id or any(
            not c.is_group and set(c.participants) == {user_id, bot_id} for c in self.conversations
        ):
            return
        now = utcnow()
        # the user's marker is a bit older, so the greeting shows up as one unread message
        conv = Conversation(
            participants=[user_id, bot_id],
            reads={user_id: (now - timedelta(seconds=1)).isoformat(), bot_id: now.isoformat()},
        )
        self.conversations.append(conv)
        self.messages.append(
            Message(conversation_id=conv.id, sender_id=bot_id, text=bot.WELCOME, created_at=now)
        )
        self._save()

    def post_bot_message(self, conversation_id: str, text: str) -> Message:
        if not self.is_bot_chat(conversation_id):
            raise ValidationError("Розмову не знайдено")
        msg = Message(
            conversation_id=conversation_id,
            sender_id=self.bot_user.id,
            text=text.strip()[:MAX_MESSAGE_LENGTH] or "…",
        )
        self.messages.append(msg)
        self._save()
        return msg

    def _conversation(self, conversation_id: str) -> Conversation:
        conv = next((c for c in self.conversations if c.id == conversation_id), None)
        if conv is None:
            raise ValidationError("Розмову не знайдено")
        return conv

    def _member_conversation(self, conversation_id: str, user_id: str) -> Conversation:
        conv = self._conversation(conversation_id)
        if user_id not in conv.participants:
            raise ValidationError("Ви не є учасником розмови")
        return conv

    def _user(self, user_id: str) -> User:
        user = next((u for u in self.users if u.id == user_id), None)
        if user is None:
            raise ValidationError("Користувача не знайдено")
        return user

    def _delete_images(self, messages: list[Message]) -> None:
        for m in messages:
            if m.image:
                (self.uploads_dir / m.image).unlink(missing_ok=True)

    def _save(self) -> None:
        self.storage.save(
            {
                "users": [asdict(u) for u in self.users],
                "conversations": [asdict(c) for c in self.conversations],
                "messages": [
                    {**asdict(m), "created_at": m.created_at.isoformat()}
                    for m in self.messages
                ],
                "sessions": self.sessions,
            }
        )

    def create_user(
        self, username: str, tag: str, password: str, password_repeat: str
    ) -> User:
        username = validate_username(username)
        tag = validate_tag(tag)
        validate_password(password, password_repeat)
        if any(u.username.lower() == username.lower() for u in self.users):
            raise ValidationError("Користувач з таким ім'ям вже існує")
        if any(u.tag == tag for u in self.users):
            raise ValidationError("Цей тег уже зайнятий")
        user = User(username=username, tag=tag, password_hash=hash_password(password))
        self.users.append(user)
        self._save()
        self.ensure_bot_chat(user.id)
        return user

    def authenticate(self, tag: str, password: str) -> User:
        try:
            tag = validate_tag(tag)
        except ValidationError:
            raise AuthError("Невірний тег або пароль") from None
        user = next((u for u in self.users if u.tag == tag and not u.is_bot), None)
        # Hash even for an unknown tag so response time doesn't reveal which tags exist.
        stored = user.password_hash if user else hash_password("")
        if not verify_password(password, stored) or user is None:
            raise AuthError("Невірний тег або пароль")
        return user

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, user_id: str) -> str:
        """Returns a new random session token; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        self.sessions[self._token_hash(token)] = user_id
        self._save()
        return token

    def user_for_token(self, token: str) -> User | None:
        user_id = self.sessions.get(self._token_hash(token))
        return next((u for u in self.users if u.id == user_id), None)

    def end_session(self, token: str) -> None:
        if self.sessions.pop(self._token_hash(token), None) is not None:
            self._save()

    def create_conversation(
        self, participant_ids: list[str], title: str | None = None
    ) -> Conversation:
        ids = list(dict.fromkeys(participant_ids))
        if len(ids) < 2:
            raise ValidationError("У розмові має бути щонайменше 2 учасники")
        known = {u.id for u in self.users}
        if not set(ids) <= known:
            raise ValidationError("Невідомий учасник розмови")
        if len(ids) == 2 and any(
            not c.is_group and set(c.participants) == set(ids) for c in self.conversations
        ):
            raise ValidationError("Розмова з цим користувачем уже існує")
        title = validate_title(title) if title and title.strip() else None
        # the first participant is the creator and becomes the group admin
        now = utcnow().isoformat()
        conv = Conversation(
            participants=ids,
            title=title,
            admin_id=ids[0] if len(ids) > 2 else None,
            reads={p: now for p in ids},
        )
        self.conversations.append(conv)
        self._save()
        return conv

    def send_message(
        self, conversation_id: str, sender_id: str, text: str, image_data: str | None = None
    ) -> Message:
        conv = self._conversation(conversation_id)
        if sender_id not in conv.participants:
            raise ValidationError("Відправник не є учасником розмови")
        if not conv.is_group:
            other = self._user(next(p for p in conv.participants if p != sender_id))
            if sender_id in other.blocked:
                raise ValidationError("Користувач обмежив надсилання повідомлень")
            if other.id in self._user(sender_id).blocked:
                raise ValidationError("Ви заблокували цього користувача. Розблокуйте, щоб писати")
        image = None
        raw = None
        if image_data:
            raw, ext = validate_image(image_data)
            image = f"{new_id()}.{ext}"
            text = text.strip()
            if len(text) > MAX_MESSAGE_LENGTH:
                raise ValidationError(f"Повідомлення довше за {MAX_MESSAGE_LENGTH} символів")
        else:
            text = validate_message_text(text)
        msg = Message(conversation_id=conversation_id, sender_id=sender_id, text=text, image=image)
        path = None
        if raw is not None:
            path = self.uploads_dir / image
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.messages.append(msg)
        try:
            self._save()
        except Exception:
            self.messages.remove(msg)
            if path:
                path.unlink(missing_ok=True)
            raise
        return msg

    def block(self, user_id: str, target_id: str) -> None:
        user = self._user(user_id)
        self._user(target_id)
        if user_id == target_id:
            raise ValidationError("Не можна заблокувати себе")
        if target_id not in user.blocked:
            user.blocked.append(target_id)
            self._save()

    def unblock(self, user_id: str, target_id: str) -> None:
        user = self._user(user_id)
        if target_id in user.blocked:
            user.blocked.remove(target_id)
            self._save()

    def list_blocked(self, user_id: str) -> list[str]:
        return list(self._user(user_id).blocked)

    def _require_admin(self, conv: Conversation, user_id: str) -> None:
        if conv.is_group and conv.admin != user_id:
            raise ValidationError("Це може зробити лише адміністратор групи")

    def _drop_messages(self, conversation_id: str) -> None:
        removed = [m for m in self.messages if m.conversation_id == conversation_id]
        self.messages = [m for m in self.messages if m.conversation_id != conversation_id]
        self._save()
        self._delete_images(removed)

    def clear_history(self, conversation_id: str, user_id: str) -> None:
        """Deletes all messages (and their photos) for everyone. In a group: admin only."""
        conv = self._member_conversation(conversation_id, user_id)
        self._require_admin(conv, user_id)
        self._drop_messages(conversation_id)

    def delete_conversation(
        self, conversation_id: str, user_id: str, everyone: bool = False
    ) -> None:
        """Direct chat: removed with all messages for both sides.
        Group: the user leaves (admin role passes on); `everyone` (admin only) deletes it."""
        conv = self._member_conversation(conversation_id, user_id)
        if conv.is_group and not everyone:
            admin = conv.admin
            conv.participants.remove(user_id)
            if len(conv.participants) < 2:
                everyone = True  # nobody to talk to: drop the group
            else:
                conv.admin_id = conv.participants[0] if admin == user_id else admin
                self._save()
                return
        else:
            self._require_admin(conv, user_id)
        self._drop_messages(conversation_id)
        if conv in self.conversations:
            self.conversations.remove(conv)
        self._save()

    def rename_conversation(self, conversation_id: str, user_id: str, title: str) -> Conversation:
        conv = self._member_conversation(conversation_id, user_id)
        if not conv.is_group:
            raise ValidationError("Перейменувати можна лише групу")
        self._require_admin(conv, user_id)
        conv.title = validate_title(title)
        conv.admin_id = conv.admin
        self._save()
        return conv

    def mark_read(self, conversation_id: str, user_id: str) -> None:
        conv = self._member_conversation(conversation_id, user_id)
        if conv.reads is None:
            conv.reads = {}
        conv.reads[user_id] = utcnow().isoformat()
        self._save()

    def unread_counts(self, user_id: str) -> dict[str, int]:
        """Number of messages from other people newer than the user's read marker, per chat."""
        marks: dict[str, datetime] = {}
        for c in self.list_conversations(user_id):
            raw = (c.reads or {}).get(user_id)
            marks[c.id] = datetime.fromisoformat(raw) if raw else datetime.min.replace(tzinfo=utcnow().tzinfo)
        counts = {cid: 0 for cid in marks}
        for m in self.messages:
            mark = marks.get(m.conversation_id)
            if mark is not None and m.sender_id != user_id and m.created_at > mark:
                counts[m.conversation_id] += 1
        return counts

    def last_messages(self, user_id: str) -> dict[str, Message]:
        """Newest message of each of the user's conversations, by conversation id."""
        allowed = {c.id for c in self.list_conversations(user_id)}
        last: dict[str, Message] = {}
        for m in self.messages:
            if m.conversation_id in allowed:
                cur = last.get(m.conversation_id)
                if cur is None or m.created_at >= cur.created_at:
                    last[m.conversation_id] = m
        return last

    def add_members(
        self, conversation_id: str, admin_id: str, user_ids: list[str]
    ) -> Conversation:
        conv = self._member_conversation(conversation_id, admin_id)
        if not conv.is_group:
            raise ValidationError("Додавати учасників можна лише в групу")
        self._require_admin(conv, admin_id)
        new_ids = list(dict.fromkeys(user_ids))
        if not new_ids:
            raise ValidationError("Оберіть, кого додати")
        known = {u.id for u in self.users}
        if not set(new_ids) <= known:
            raise ValidationError("Користувача не знайдено")
        if any(i in conv.participants for i in new_ids):
            raise ValidationError("Цей користувач уже в групі")
        now = utcnow().isoformat()
        conv.admin_id = conv.admin
        if conv.reads is None:
            conv.reads = {}
        for i in new_ids:
            conv.participants.append(i)
            conv.reads[i] = now  # earlier messages are visible but not counted as unread
        self._save()
        return conv

    def remove_member(self, conversation_id: str, admin_id: str, member_id: str) -> None:
        conv = self._member_conversation(conversation_id, admin_id)
        if not conv.is_group:
            raise ValidationError("Видаляти учасників можна лише в групі")
        self._require_admin(conv, admin_id)
        if member_id == admin_id:
            raise ValidationError("Адміністратор не може видалити себе, лише вийти з групи")
        if member_id not in conv.participants:
            raise ValidationError("Цей користувач не є учасником групи")
        if len(conv.participants) <= 2:
            raise ValidationError("У групі має лишитися щонайменше 2 учасники")
        conv.admin_id = conv.admin
        conv.participants.remove(member_id)
        self._save()

    def list_conversations(self, user_id: str | None = None) -> list[Conversation]:
        if user_id is None:
            return list(self.conversations)
        return [c for c in self.conversations if user_id in c.participants]

    def search_messages(self, query: str, user_id: str | None = None) -> list[Message]:
        query = query.strip().lower()
        if not query:
            raise ValidationError("Пошуковий запит не може бути порожнім")
        allowed = {c.id for c in self.list_conversations(user_id)}
        return sorted(
            (
                m
                for m in self.messages
                if m.conversation_id in allowed and query in m.text.lower()
            ),
            key=lambda m: m.created_at,
        )

    def history_for(self, conversation_id: str, user_id: str) -> list[Message]:
        self._member_conversation(conversation_id, user_id)
        return self.get_history(conversation_id)

    def get_history(self, conversation_id: str) -> list[Message]:
        return sorted(
            (m for m in self.messages if m.conversation_id == conversation_id),
            key=lambda m: m.created_at,
        )
