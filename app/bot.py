"""CalorieBot: estimates calories of the food in a photo.

Uses the free Gemini API when GEMINI_API_KEY is set, otherwise a free local model via Ollama.
"""

import base64
import io
import os
from pathlib import Path

import httpx
from PIL import Image

BOT_TAG = "calorie_bot"
BOT_NAME = "CalorieBot"


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Reads KEY=VALUE lines from .env (git-ignored) so the key survives terminal restarts."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
MODEL = os.environ.get("CALORIE_BOT_MODEL", "qwen2.5vl:3b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

WELCOME = (
    "Привіт! Я CalorieBot. Надішліть мені фото їжі, і я оціню калорії та БЖВ. "
    "Можна додати підпис, наприклад вагу порції."
)
NO_PHOTO = "Надішліть фото їжі, і я порахую калорії."
NOT_RUNNING = (
    "Бот зараз недоступний: не запущена Ollama. Запустіть її командою «ollama serve» і спробуйте ще раз."
)
NO_MODEL = f"Бот ще не готовий: модель не завантажена. Виконайте на сервері «ollama pull {MODEL}»."
BAD_KEY = "Бот не може увійти в Gemini: ключ GEMINI_API_KEY недійсний. Перевірте його на aistudio.google.com/apikey."
RATE_LIMIT = "Забагато запитів до Gemini (безкоштовний ліміт). Спробуйте за хвилину."
NO_GEMINI_MODEL = f"Модель Gemini «{GEMINI_MODEL}» недоступна. Задайте іншу змінною GEMINI_MODEL."
FAILED = "Не вдалося обробити фото. Спробуйте ще раз трохи пізніше."

SYSTEM = (
    "Ти CalorieBot, помічник у месенджері, який оцінює калорійність їжі за фото. "
    "Відповідай українською, коротко, звичайним текстом без markdown і зірочок. "
    "Формат: перелік страв, для кожної приблизна вага і ккал; потім рядок «Разом: N ккал» "
    "та рядок «Б/Ж/В: x/y/z г». В кінці одне речення, що це приблизна оцінка. "
    "Якщо користувач вказав вагу або склад у підписі, використай це. "
    "Якщо на фото немає їжі, скажи про це і попроси інше фото."
)


def shrink(image: bytes, side: int = 512) -> bytes:
    """Smaller photo = far fewer vision tokens = much faster answer; food stays recognisable."""
    try:
        img = Image.open(io.BytesIO(image))
        img.thumbnail((side, side))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - fall back to the original bytes
        return image


def _gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _ask_gemini(key: str, image: bytes, note: str) -> str:
    r = httpx.post(
        GEMINI_URL.format(model=GEMINI_MODEL),
        headers={"x-goog-api-key": key},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.standard_b64encode(shrink(image, 1024)).decode("ascii"),
                            }
                        },
                        {"text": "Підпис користувача: " + note},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=60.0,
    )
    if r.status_code in (400, 401, 403):
        return BAD_KEY
    if r.status_code == 404:
        return NO_GEMINI_MODEL
    if r.status_code == 429:
        return RATE_LIMIT
    r.raise_for_status()
    parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip() or FAILED


def _ask_ollama(image: bytes, note: str) -> str:
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "stream": False,
                "options": {"temperature": 0.2},
                "messages": [
                    {
                        "role": "user",
                        "content": SYSTEM + "\n\nПідпис користувача: " + note,
                        "images": [base64.standard_b64encode(shrink(image)).decode("ascii")],
                    }
                ],
            },
            timeout=300.0,  # the first request also loads the model into memory
        )
    except httpx.ConnectError:
        return NOT_RUNNING
    if r.status_code == 404:
        return NO_MODEL
    r.raise_for_status()
    return r.json()["message"]["content"].strip() or FAILED


def reply(image: bytes | None, ext: str | None, caption: str) -> str:
    """Blocking call (run it in a thread). Always returns text for the chat, never raises."""
    if not image:
        return NO_PHOTO
    note = caption.strip() or "(немає)"
    try:
        key = _gemini_key()
        return _ask_gemini(key, image, note) if key else _ask_ollama(image, note)
    except Exception:  # noqa: BLE001 - the chat must always get an answer
        return FAILED
