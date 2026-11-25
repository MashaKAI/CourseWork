import base64
import binascii
import re

MAX_MESSAGE_LENGTH = 2000
MAX_USERNAME_LENGTH = 32
MAX_TITLE_LENGTH = 64
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
JPEG_SIGNATURE = bytes.fromhex("ffd8ff")
USERNAME_RE = re.compile(r"^[\w.-]+$")


MIN_TAG_LENGTH = 3
MAX_TAG_LENGTH = 32
TAG_RE = re.compile(r"^[A-Za-z0-9_]+$")
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128


class ValidationError(ValueError):
    pass


class AuthError(Exception):
    pass


def validate_message_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValidationError("Повідомлення не може бути порожнім")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValidationError(f"Повідомлення довше за {MAX_MESSAGE_LENGTH} символів")
    return text


def validate_username(username: str) -> str:
    username = username.strip()
    if not username:
        raise ValidationError("Ім'я користувача не може бути порожнім")
    if len(username) > MAX_USERNAME_LENGTH:
        raise ValidationError(f"Ім'я довше за {MAX_USERNAME_LENGTH} символів")
    if not USERNAME_RE.match(username):
        raise ValidationError("Ім'я може містити лише літери, цифри, '.', '_' і '-'")
    return username


def validate_tag(tag: str) -> str:
    tag = tag.strip().removeprefix("@")
    if not tag:
        raise ValidationError("Тег не може бути порожнім")
    if not MIN_TAG_LENGTH <= len(tag) <= MAX_TAG_LENGTH:
        raise ValidationError(f"Тег має містити від {MIN_TAG_LENGTH} до {MAX_TAG_LENGTH} символів")
    if not TAG_RE.match(tag):
        raise ValidationError("Тег може містити лише латинські літери, цифри та '_'")
    return tag.lower()


def validate_password(password: str, repeat: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Пароль має містити від {MIN_PASSWORD_LENGTH} до {MAX_PASSWORD_LENGTH} символів"
        )
    if password != repeat:
        raise ValidationError("Паролі не збігаються")
    return password


def validate_image(data_url: str) -> tuple[bytes, str]:
    """Decode a base64 data URL, check size and real file type; returns (bytes, extension)."""
    _, _, b64 = data_url.partition(",")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("Некоректне зображення") from None
    if not raw:
        raise ValidationError("Некоректне зображення")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValidationError(f"Зображення більше за {MAX_IMAGE_BYTES // (1024 * 1024)} МБ")
    if raw.startswith(PNG_SIGNATURE):
        return raw, "png"
    if raw.startswith(JPEG_SIGNATURE):
        return raw, "jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return raw, "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return raw, "webp"
    raise ValidationError("Підтримуються лише PNG, JPEG, GIF і WEBP")


def validate_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValidationError("Назва не може бути порожньою")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(f"Назва довша за {MAX_TITLE_LENGTH} символів")
    return title
