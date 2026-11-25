import asyncio
import os
import re
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import bot
from .models import Conversation, Message, User
from .services import MessengerService
from .storage import SqliteStorage, StorageError
from .validation import AuthError, ValidationError

router = APIRouter(prefix="/api")


@lru_cache
def get_service() -> MessengerService:
    path = os.environ.get("MESSENGER_DATA", "data/messenger.db")
    return MessengerService(SqliteStorage(path))


class UserIn(BaseModel):
    username: str
    tag: str
    password: str
    password_repeat: str


class LoginIn(BaseModel):
    tag: str
    password: str


class ConversationIn(BaseModel):
    participant_ids: list[str]  # the current user is added automatically and becomes admin
    title: str | None = None


class MembersIn(BaseModel):
    user_ids: list[str]


class TitleIn(BaseModel):
    title: str


class MessageIn(BaseModel):
    text: str = ""
    image_data: str | None = None  # data URL (base64) of a PNG/JPEG/GIF/WEBP photo


class BlockIn(BaseModel):
    target_id: str


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "tag": u.tag, "is_bot": u.is_bot}  # never the hash


def conversation_out(
    c: Conversation, last: Message | None = None, unread: int = 0, is_bot_chat: bool = False
) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "participants": c.participants,
        "is_group": c.is_group,
        "admin_id": c.admin,
        "last_message": message_out(last) if last else None,
        "unread": unread,
        "is_bot_chat": is_bot_chat,
    }


def message_out(m: Message) -> dict:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "sender_id": m.sender_id,
        "text": m.text,
        "created_at": m.created_at.isoformat(),  # UTC, ISO 8601
        "image_url": f"/api/uploads/{m.image}" if m.image else None,
    }


def fail(e: Exception) -> HTTPException:
    if isinstance(e, StorageError):
        return HTTPException(500, str(e))
    return HTTPException(400, str(e))


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Потрібен вхід")
    return token


def current_user(
    token: str = Depends(bearer_token), svc: MessengerService = Depends(get_service)
) -> User:
    user = svc.user_for_token(token)
    if user is None:
        raise HTTPException(401, "Сесія недійсна, увійдіть знову")
    return user


# Endpoints are `async def` on purpose: they run on the event loop one at a time,
# so the in-memory service needs no extra locking.
@router.post("/users", status_code=201)
async def create_user(body: UserIn, svc: MessengerService = Depends(get_service)):
    try:
        user = svc.create_user(body.username, body.tag, body.password, body.password_repeat)
        return {**user_out(user), "token": svc.create_session(user.id)}
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.post("/login")
async def login(body: LoginIn, svc: MessengerService = Depends(get_service)):
    try:
        user = svc.authenticate(body.tag, body.password)
        svc.ensure_bot_chat(user.id)
        return {**user_out(user), "token": svc.create_session(user.id)}
    except AuthError as e:
        raise HTTPException(401, str(e))
    except StorageError as e:
        raise fail(e)


@router.post("/logout", status_code=204)
async def logout(
    _: User = Depends(current_user),
    token: str = Depends(bearer_token),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.end_session(token)
    except StorageError as e:
        raise fail(e)


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return user_out(user)


@router.get("/users")
async def list_users(
    _: User = Depends(current_user), svc: MessengerService = Depends(get_service)
):
    return [user_out(u) for u in svc.users]


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: ConversationIn,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        ids = [user.id, *[i for i in body.participant_ids if i != user.id]]
        return conversation_out(svc.create_conversation(ids, body.title))
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.get("/conversations")
async def list_conversations(
    user: User = Depends(current_user), svc: MessengerService = Depends(get_service)
):
    last = svc.last_messages(user.id)
    unread = svc.unread_counts(user.id)
    return [
        conversation_out(c, last.get(c.id), unread.get(c.id, 0), svc.is_bot_chat(c.id))
        for c in svc.list_conversations(user.id)
    ]


@router.post("/conversations/{conversation_id}/read", status_code=204)
async def mark_read(
    conversation_id: str,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.mark_read(conversation_id, user.id)
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str,
    body: TitleIn,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        return conversation_out(svc.rename_conversation(conversation_id, user.id, body.title))
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.get("/conversations/{conversation_id}/messages")
async def get_history(
    conversation_id: str,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        return [message_out(m) for m in svc.history_for(conversation_id, user.id)]
    except ValidationError as e:
        raise fail(e)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(
    conversation_id: str,
    body: MessageIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        msg = svc.send_message(conversation_id, user.id, body.text, body.image_data)
    except (ValidationError, StorageError) as e:
        raise fail(e)
    if svc.is_bot_chat(conversation_id):
        background.add_task(bot_reply, svc, conversation_id, msg)
    return message_out(msg)


async def bot_reply(svc: MessengerService, conversation_id: str, msg: Message) -> None:
    """Asks the vision model in a worker thread (it takes seconds), then posts the answer from the event loop."""
    data = ext = None
    if msg.image:
        data = await asyncio.to_thread((svc.uploads_dir / msg.image).read_bytes)
        ext = msg.image.rsplit(".", 1)[-1]
    text = await asyncio.to_thread(bot.reply, data, ext, msg.text)
    try:
        svc.post_bot_message(conversation_id, text)
    except (ValidationError, StorageError):
        pass  # the chat was deleted while the bot was thinking


@router.get("/search")
async def search(
    q: str, user: User = Depends(current_user), svc: MessengerService = Depends(get_service)
):
    try:
        return [message_out(m) for m in svc.search_messages(q, user.id)]
    except ValidationError as e:
        raise fail(e)


@router.delete("/conversations/{conversation_id}/messages", status_code=204)
async def clear_history(
    conversation_id: str,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.clear_history(conversation_id, user.id)
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    everyone: bool = False,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.delete_conversation(conversation_id, user.id, everyone)
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.post("/conversations/{conversation_id}/members", status_code=201)
async def add_members(
    conversation_id: str,
    body: MembersIn,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        return conversation_out(svc.add_members(conversation_id, user.id, body.user_ids))
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.delete("/conversations/{conversation_id}/members/{member_id}", status_code=204)
async def remove_member(
    conversation_id: str,
    member_id: str,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.remove_member(conversation_id, user.id, member_id)
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.get("/blocks")
async def list_blocks(
    user: User = Depends(current_user), svc: MessengerService = Depends(get_service)
):
    return svc.list_blocked(user.id)


@router.post("/blocks", status_code=204)
async def block_user(
    body: BlockIn,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.block(user.id, body.target_id)
    except (ValidationError, StorageError) as e:
        raise fail(e)


@router.delete("/blocks", status_code=204)
async def unblock_user(
    target_id: str,
    user: User = Depends(current_user),
    svc: MessengerService = Depends(get_service),
):
    try:
        svc.unblock(user.id, target_id)
    except (ValidationError, StorageError) as e:
        raise fail(e)


UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")


# Photos are opened by <img src>, which cannot send a token header, so the file name
# itself is the secret: it is a random 128-bit id that is only shown to chat members.
@router.get("/uploads/{name}")
async def get_upload(name: str, svc: MessengerService = Depends(get_service)):
    if not UPLOAD_NAME_RE.match(name):  # also blocks path traversal
        raise HTTPException(404, "Файл не знайдено")
    path = svc.uploads_dir / name
    if not path.is_file():
        raise HTTPException(404, "Файл не знайдено")
    return FileResponse(path)
