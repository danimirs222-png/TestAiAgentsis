#!/usr/bin/env python3
"""TG Studio - single-file Telegram bot manager + Telegram-style messenger UI.

Это ОДИН Python-файл, который запускает:
  * Telegram-бот (python-telegram-bot >= 20.8) — ловит и сохраняет все апдейты
    (сообщения, реакции, опросы, изменения, участники) в локальную SQLite.
  * Веб-сервер aiohttp с готовым HTML/CSS/JS интерфейсом в стиле Telegram —
    весь фронтенд встроен прямо в этот файл (никаких внешних статиков).
  * Реал-тайм WebSocket-канал, чтобы UI обновлялся мгновенно.

Возможности:
  * Чаты, группы, каналы, в которых состоит бот, со всей видимой ему историей.
  * Полноценный композер: ответы, редактирование, удаление, реакции,
    стикеры, фото/видео/документы (drag-and-drop), опросы, inline-кнопки.
  * Жесты: свайп-вправо чтобы ответить, long-press / правый клик → меню,
    двойной тап → ❤, pinch-zoom картинок, плавный скролл, мобильная навигация.
  * Авто-модерация по чатам: бан-слова, антифлуд, антикапс, антиссылки,
    AI-модерация через OpenRouter с выбором модели и порога.
  * Онлайн-редактор профиля бота: имя, описание, краткое описание, команды.
  * AI-ассистент с переключением между всеми моделями OpenRouter из .env.
  * Мини-апы — пишите HTML/JS, запускайте в песочнице с собственным storage.
  * PWA: можно «установить» как приложение на телефон.

КАК ЗАПУСТИТЬ:

  1. Установить зависимости:
       pip install "python-telegram-bot>=20.8,<21" "aiohttp>=3.9,<4"

  2. Запустить файл первый раз:
       python app.py
     Если рядом нет .env — он будет создан с шаблоном. Откройте .env,
     впишите туда хотя бы TG_BOT_TOKEN, и запустите снова.

  3. Открыть в браузере http://localhost:8080/?token=<WEB_ACCESS_TOKEN>

ВАЖНО ПРО ОГРАНИЧЕНИЯ Telegram Bot API:
  * Бот видит только сообщения, адресованные ему, или (в группах) все, если
    в @BotFather у бота выключен Privacy Mode (BotFather → /mybots → выбрать
    бота → Bot Settings → Group Privacy → Turn off).
  * Историю до момента запуска бота получить нельзя — это ограничение API.
  * Аватарку самого бота можно менять только через @BotFather, не через API.
  * Нельзя «увидеть выделение текста в Telegram-приложении пользователя» —
    Bot API такого не отдаёт. Выделение работает внутри нашего UI.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import html as html_lib
import json
import logging
import mimetypes
import os
import re
import io
import secrets
import sqlite3
import sys
import time
import traceback
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web, WSMsgType

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    PollAnswerHandler,
    PollHandler,
    filters,
)

# ============================================================================
# Config
# ============================================================================

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FILES_DIR = ROOT / "files"
DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tgstudio.sqlite3"


DEFAULT_ENV = """\
# TG Studio config. Auto-created on first run. Don't commit this file.
# After editing, restart the app.

# --- Telegram bot ---
# Токен бота из @BotFather (формата 1234567890:AA...).
TG_BOT_TOKEN=
TG_OWNER_ID=
TG_CHANNEL_ID=
TG_OWNER_USERNAME=

# --- Web app ---
WEB_HOST=0.0.0.0
WEB_PORT=8080
# !!! ЭТО НЕ ТОКЕН БОТА !!!
# Опциональный пароль на вход. По умолчанию пустой — и это ок:
# при пустом значении вход в админ-панель без окна ввода токена.
# Впишите любую строку, если хотите защитить доступ по локалке.
WEB_ACCESS_TOKEN=

# --- OpenRouter (заполните те ключи, что есть; можно частично) ---
OPENROUTER_KEY_1=
OPENROUTER_KEY_2=
OPENROUTER_KEY_3=
OPENROUTER_KEY_4=
OPENROUTER_KEY_5=
OPENROUTER_KEY_6=
OPENROUTER_KEY_7=
OPENROUTER_KEY_8=
OPENROUTER_KEY_9=
OPENROUTER_KEY_10=
OPENROUTER_KEY_11=
OPENROUTER_KEY_12=
OPENROUTER_KEY_13=

AI_DEFAULT_MODEL=qwen/qwen3-next-80b-a3b-instruct:free
"""


# ---------------------------------------------------------------------------
# Inline defaults — DO NOT EDIT IN-REPO. Personalized at delivery time so the
# user can `python3 app.py` with zero configuration. Anything set here is used
# *only* when the same key is missing from the environment / `.env` file.
# Strings beginning with `__SET_ME` are treated as unset.
# ---------------------------------------------------------------------------
INLINE_DEFAULTS: dict[str, str] = {
    "TG_BOT_TOKEN": "__SET_ME_TG_BOT_TOKEN__",
    "TG_OWNER_ID": "__SET_ME_TG_OWNER_ID__",
    "TG_OWNER_USERNAME": "__SET_ME_TG_OWNER_USERNAME__",
    "TG_CHANNEL_ID": "__SET_ME_TG_CHANNEL_ID__",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": "8080",
    "WEB_ACCESS_TOKEN": "",
    "AI_DEFAULT_MODEL": "qwen/qwen3-next-80b-a3b-instruct:free",
    "OPENROUTER_KEY_1": "__SET_ME_OPENROUTER_KEY_1__",
    "OPENROUTER_KEY_2": "__SET_ME_OPENROUTER_KEY_2__",
    "OPENROUTER_KEY_3": "__SET_ME_OPENROUTER_KEY_3__",
    "OPENROUTER_KEY_4": "__SET_ME_OPENROUTER_KEY_4__",
    "OPENROUTER_KEY_5": "__SET_ME_OPENROUTER_KEY_5__",
    "OPENROUTER_KEY_6": "__SET_ME_OPENROUTER_KEY_6__",
    "OPENROUTER_KEY_7": "__SET_ME_OPENROUTER_KEY_7__",
    "OPENROUTER_KEY_8": "__SET_ME_OPENROUTER_KEY_8__",
    "OPENROUTER_KEY_9": "__SET_ME_OPENROUTER_KEY_9__",
    "OPENROUTER_KEY_10": "__SET_ME_OPENROUTER_KEY_10__",
    "OPENROUTER_KEY_11": "__SET_ME_OPENROUTER_KEY_11__",
    "OPENROUTER_KEY_12": "__SET_ME_OPENROUTER_KEY_12__",
    "OPENROUTER_KEY_13": "__SET_ME_OPENROUTER_KEY_13__",
}


def load_env() -> None:
    """Minimal .env loader (no external dependency). Inline defaults are
    applied last and only fill in keys that are still unset."""
    env_file = ROOT / ".env"
    # Only auto-create .env if NONE of the inline defaults are populated. If the
    # file was personalized for the user, they don't need a .env at all.
    inline_populated = any(
        not v.startswith("__SET_ME") for k, v in INLINE_DEFAULTS.items()
        if k in {"TG_BOT_TOKEN", "OPENROUTER_KEY_1"}
    )
    if not env_file.exists() and not inline_populated:
        env_file.write_text(DEFAULT_ENV, "utf-8")
        print(
            f"[tgstudio] Создан шаблон {env_file}.\n"
            f"           Впишите туда TG_BOT_TOKEN (токен бота из @BotFather),\n"
            f"           чтобы бот заработал. Web-UI запустится и без него (будет без чатов)."
        )
    if env_file.exists():
        for raw in env_file.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
    # Apply INLINE_DEFAULTS only for keys still unset.
    for k, v in INLINE_DEFAULTS.items():
        if v.startswith("__SET_ME"):
            continue
        if not os.environ.get(k):
            os.environ[k] = v
    # The "changeme-please" placeholder from earlier versions of .env should
    # behave like no token at all (auth disabled). Drop it.
    cur = os.environ.get("WEB_ACCESS_TOKEN", "").strip()
    if cur in ("changeme-please", "changeme", "please-change-me"):
        os.environ["WEB_ACCESS_TOKEN"] = ""


load_env()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_OWNER_ID = int(env("TG_OWNER_ID", "0") or 0)
TG_CHANNEL_ID = int(env("TG_CHANNEL_ID", "0") or 0)
TG_OWNER_USERNAME = env("TG_OWNER_USERNAME")
WEB_HOST = env("WEB_HOST", "0.0.0.0")
WEB_PORT = int(env("WEB_PORT", "8080"))
WEB_ACCESS_TOKEN = env("WEB_ACCESS_TOKEN", "").strip()
# Empty token == auth disabled for everyone (web UI is local-admin tool).
AUTH_ENABLED = bool(WEB_ACCESS_TOKEN)
AI_DEFAULT_MODEL = env("AI_DEFAULT_MODEL", "qwen/qwen3-next-80b-a3b-instruct:free")

OPENROUTER_KEYS: list[str] = [
    env(f"OPENROUTER_KEY_{i}") for i in range(1, 14)
]
OPENROUTER_KEYS = [k for k in OPENROUTER_KEYS if k]

# Registry of known models -> (provider key index, capability)
MODEL_REGISTRY: list[dict[str, Any]] = [
    {"name": "baidu/cobuddy:free", "key_idx": 0, "kind": "chat", "reasoning": True},
    {"name": "google/gemma-4-31b-it:free", "key_idx": 1, "kind": "chat", "reasoning": True},
    {"name": "qwen/qwen3-next-80b-a3b-instruct:free", "key_idx": 2, "kind": "chat"},
    {"name": "openai/gpt-oss-120b:free", "key_idx": 3, "kind": "chat", "reasoning": True},
    {"name": "qwen/qwen3-coder:free", "key_idx": 4, "kind": "chat"},
    {"name": "openrouter/owl-alpha", "key_idx": 5, "kind": "chat"},
    {"name": "poolside/laguna-xs.2:free", "key_idx": 6, "kind": "chat", "reasoning": True},
    {"name": "openai/gpt-oss-120b:free#2", "key_idx": 7, "kind": "chat", "reasoning": True,
     "real_name": "openai/gpt-oss-120b:free"},
    {"name": "black-forest-labs/flux.2-pro", "key_idx": 8, "kind": "image"},
    {"name": "sourceful/riverflow-v2-fast", "key_idx": 9, "kind": "image"},
    {"name": "nvidia/llama-nemotron-embed-vl-1b-v2:free", "key_idx": 10, "kind": "embed"},
    {"name": "google/lyria-3-clip-preview", "key_idx": 11, "kind": "vision"},
    {"name": "cohere/rerank-v3.5", "key_idx": 12, "kind": "rerank"},
]


def model_key(model_id: str) -> tuple[str, str]:
    """Return (api_key, real_model_name) for a registry id."""
    for m in MODEL_REGISTRY:
        if m["name"] == model_id:
            idx = m["key_idx"]
            key = OPENROUTER_KEYS[idx] if idx < len(OPENROUTER_KEYS) else ""
            return key, m.get("real_name", m["name"])
    # Unknown model -> try first key
    return (OPENROUTER_KEYS[0] if OPENROUTER_KEYS else ""), model_id


# ============================================================================
# Logging
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tgstudio")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


# ============================================================================
# Database
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    type TEXT,
    title TEXT,
    username TEXT,
    photo_file_id TEXT,
    members_count INTEGER,
    last_message_id INTEGER,
    last_message_at INTEGER,
    unread_count INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_bot INTEGER,
    is_premium INTEGER,
    photo_file_id TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER,
    message_id INTEGER,
    sender_id INTEGER,
    sender_chat_id INTEGER,
    date INTEGER,
    edit_date INTEGER,
    text TEXT,
    caption TEXT,
    entities_json TEXT,
    media_type TEXT,
    file_id TEXT,
    file_unique_id TEXT,
    thumb_file_id TEXT,
    media_group_id TEXT,
    sticker_emoji TEXT,
    sticker_set TEXT,
    sticker_is_animated INTEGER DEFAULT 0,
    sticker_is_video INTEGER DEFAULT 0,
    sticker_type TEXT,
    poll_id TEXT,
    poll_json TEXT,
    reply_to_message_id INTEGER,
    forward_from_id INTEGER,
    reply_markup_json TEXT,
    reactions_json TEXT,
    deleted INTEGER DEFAULT 0,
    via_bot_id INTEGER,
    width INTEGER,
    height INTEGER,
    duration INTEGER,
    file_name TEXT,
    file_size INTEGER,
    raw_json TEXT,
    PRIMARY KEY(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(media_group_id);
CREATE INDEX IF NOT EXISTS idx_messages_poll ON messages(poll_id);

CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    file_unique_id TEXT,
    local_path TEXT,
    mime_type TEXT,
    size INTEGER,
    downloaded_at INTEGER
);

CREATE TABLE IF NOT EXISTS moderation (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    banwords_json TEXT DEFAULT '[]',
    antiflood INTEGER DEFAULT 0,
    antiflood_count INTEGER DEFAULT 5,
    antiflood_seconds INTEGER DEFAULT 5,
    antilinks INTEGER DEFAULT 0,
    anticaps INTEGER DEFAULT 0,
    caps_threshold INTEGER DEFAULT 70,
    ai_moderation INTEGER DEFAULT 0,
    ai_threshold INTEGER DEFAULT 70,
    ai_model TEXT,
    action TEXT DEFAULT 'delete',
    mute_minutes INTEGER DEFAULT 10,
    warn_threshold INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS warnings (
    chat_id INTEGER,
    user_id INTEGER,
    count INTEGER DEFAULT 0,
    last_at INTEGER,
    PRIMARY KEY(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS mini_apps (
    id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    html TEXT,
    icon TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS ai_chats (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    messages_json TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    kind TEXT,
    chat_id INTEGER,
    user_id INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS poll_voters (
    poll_id TEXT,
    user_id INTEGER,
    option_ids_json TEXT,
    voted_at INTEGER,
    PRIMARY KEY(poll_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_pollvoters ON poll_voters(poll_id);

CREATE TABLE IF NOT EXISTS user_photos (
    user_id INTEGER,
    file_id TEXT,
    idx INTEGER,
    fetched_at INTEGER,
    PRIMARY KEY(user_id, idx)
);

CREATE TABLE IF NOT EXISTS custom_emoji (
    emoji_id TEXT PRIMARY KEY,
    file_id TEXT,
    is_animated INTEGER DEFAULT 0,
    is_video INTEGER DEFAULT 0,
    sticker_set TEXT,
    emoji TEXT,
    fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id INTEGER,
    user_id INTEGER,
    status TEXT,
    title TEXT,
    custom_title TEXT,
    can_post INTEGER DEFAULT 0,
    can_edit INTEGER DEFAULT 0,
    can_delete INTEGER DEFAULT 0,
    can_invite INTEGER DEFAULT 0,
    can_pin INTEGER DEFAULT 0,
    can_promote INTEGER DEFAULT 0,
    can_restrict INTEGER DEFAULT 0,
    updated_at INTEGER,
    PRIMARY KEY(chat_id, user_id)
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.lock = asyncio.Lock()

    def _migrate(self) -> None:
        """Add new columns to old DBs that pre-date schema changes."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        for col, ddl in [
            ("thumb_file_id", "TEXT"),
            ("media_group_id", "TEXT"),
            ("sticker_is_animated", "INTEGER DEFAULT 0"),
            ("sticker_is_video", "INTEGER DEFAULT 0"),
            ("sticker_type", "TEXT"),
            ("poll_id", "TEXT"),
            ("width", "INTEGER"),
            ("height", "INTEGER"),
            ("duration", "INTEGER"),
            ("file_name", "TEXT"),
            ("file_size", "INTEGER"),
        ]:
            if col not in cols:
                try:
                    self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def exec(self, sql: str, params: tuple | list = ()) -> None:
        self.conn.execute(sql, params)


db = DB(DB_PATH)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def now_ts() -> int:
    return int(time.time())


def audit(kind: str, chat_id: int | None = None, user_id: int | None = None, message: str = "") -> None:
    db.exec(
        "INSERT INTO audit_log(ts, kind, chat_id, user_id, message) VALUES (?,?,?,?,?)",
        (now_ts(), kind, chat_id, user_id, message),
    )


# ============================================================================
# WebSocket pub/sub
# ============================================================================


class Hub:
    """Broadcast events to all connected web clients."""

    def __init__(self) -> None:
        self.clients: set[web.WebSocketResponse] = set()

    async def add(self, ws: web.WebSocketResponse) -> None:
        self.clients.add(ws)

    async def remove(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self.clients:
            return
        payload = json.dumps(event, default=str)
        dead: list[web.WebSocketResponse] = []
        for ws in self.clients:
            try:
                await ws.send_str(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()


# ============================================================================
# File cache (downloads Telegram files to disk on demand)
# ============================================================================


async def cache_file(bot: Bot, file_id: str) -> dict[str, Any] | None:
    """Ensure a Telegram file is downloaded locally; return file metadata."""
    if not file_id:
        return None
    row = db.one("SELECT * FROM files WHERE file_id=?", (file_id,))
    if row and row["local_path"] and Path(row["local_path"]).exists():
        return row_to_dict(row)
    try:
        tg_file = await bot.get_file(file_id)
    except TelegramError as e:
        log.warning("get_file failed for %s: %s", file_id, e)
        return None
    suffix = Path(tg_file.file_path or "").suffix or ".bin"
    local = FILES_DIR / f"{tg_file.file_unique_id}{suffix}"
    if not local.exists():
        try:
            await tg_file.download_to_drive(str(local))
        except TelegramError as e:
            log.warning("download failed: %s", e)
            return None
    mime, _ = mimetypes.guess_type(str(local))
    db.exec(
        "INSERT OR REPLACE INTO files(file_id, file_unique_id, local_path, mime_type, size, downloaded_at)"
        " VALUES (?,?,?,?,?,?)",
        (file_id, tg_file.file_unique_id, str(local), mime or "", local.stat().st_size, now_ts()),
    )
    return row_to_dict(db.one("SELECT * FROM files WHERE file_id=?", (file_id,)))


# ============================================================================
# Telegram serialization helpers
# ============================================================================


def serialize_user(u: Any) -> dict[str, Any] | None:
    if not u:
        return None
    return {
        "id": u.id,
        "is_bot": getattr(u, "is_bot", False),
        "first_name": getattr(u, "first_name", ""),
        "last_name": getattr(u, "last_name", ""),
        "username": getattr(u, "username", ""),
        "is_premium": getattr(u, "is_premium", False) or False,
        "language_code": getattr(u, "language_code", ""),
    }


def serialize_chat(c: Any) -> dict[str, Any] | None:
    if not c:
        return None
    return {
        "id": c.id,
        "type": str(c.type),
        "title": getattr(c, "title", None) or "",
        "username": getattr(c, "username", None) or "",
        "first_name": getattr(c, "first_name", None) or "",
        "last_name": getattr(c, "last_name", None) or "",
    }


def serialize_entities(entities: Any) -> list[dict[str, Any]]:
    if not entities:
        return []
    out: list[dict[str, Any]] = []
    for e in entities:
        entry: dict[str, Any] = {
            "type": str(e.type),
            "offset": e.offset,
            "length": e.length,
        }
        if getattr(e, "url", None):
            entry["url"] = e.url
        if getattr(e, "user", None):
            entry["user"] = serialize_user(e.user)
        if getattr(e, "language", None):
            entry["language"] = e.language
        if getattr(e, "custom_emoji_id", None):
            entry["custom_emoji_id"] = e.custom_emoji_id
        out.append(entry)
    return out


def serialize_reply_markup(rm: Any) -> dict[str, Any] | None:
    if not rm or not getattr(rm, "inline_keyboard", None):
        return None
    rows = []
    for row in rm.inline_keyboard:
        r = []
        for b in row:
            r.append(
                {
                    "text": b.text,
                    "url": getattr(b, "url", None),
                    "callback_data": getattr(b, "callback_data", None),
                    "switch_inline_query": getattr(b, "switch_inline_query", None),
                }
            )
        rows.append(r)
    return {"inline_keyboard": rows}


def serialize_poll(p: Any) -> dict[str, Any] | None:
    if not p:
        return None
    return {
        "id": p.id,
        "question": p.question,
        "options": [{"text": o.text, "voter_count": o.voter_count} for o in p.options],
        "total_voter_count": p.total_voter_count,
        "is_closed": p.is_closed,
        "is_anonymous": p.is_anonymous,
        "type": str(p.type),
        "allows_multiple_answers": p.allows_multiple_answers,
        "correct_option_id": getattr(p, "correct_option_id", None),
    }


def detect_media(m: Any) -> dict[str, Any]:
    """Return a dict with media metadata for a Message."""
    out: dict[str, Any] = {
        "media_type": "",
        "file_id": "",
        "file_unique_id": "",
        "thumb_file_id": "",
        "width": None,
        "height": None,
        "duration": None,
        "file_name": "",
        "file_size": None,
    }
    if not m:
        return out
    if m.photo:
        ph = m.photo[-1]
        out.update(media_type="photo", file_id=ph.file_id, file_unique_id=ph.file_unique_id,
                   width=ph.width, height=ph.height, file_size=ph.file_size)
    elif m.video:
        v = m.video
        out.update(media_type="video", file_id=v.file_id, file_unique_id=v.file_unique_id,
                   width=v.width, height=v.height, duration=v.duration, file_size=v.file_size,
                   file_name=v.file_name or "",
                   thumb_file_id=v.thumbnail.file_id if v.thumbnail else "")
    elif m.animation:
        a = m.animation
        out.update(media_type="animation", file_id=a.file_id, file_unique_id=a.file_unique_id,
                   width=a.width, height=a.height, duration=a.duration, file_size=a.file_size,
                   thumb_file_id=a.thumbnail.file_id if a.thumbnail else "")
    elif m.voice:
        v = m.voice
        out.update(media_type="voice", file_id=v.file_id, file_unique_id=v.file_unique_id,
                   duration=v.duration, file_size=v.file_size)
    elif m.audio:
        a = m.audio
        out.update(media_type="audio", file_id=a.file_id, file_unique_id=a.file_unique_id,
                   duration=a.duration, file_size=a.file_size,
                   file_name=(a.title or "") + (" - " + a.performer if a.performer else "") or (a.file_name or ""),
                   thumb_file_id=a.thumbnail.file_id if a.thumbnail else "")
    elif m.video_note:
        vn = m.video_note
        out.update(media_type="video_note", file_id=vn.file_id, file_unique_id=vn.file_unique_id,
                   duration=vn.duration, file_size=vn.file_size,
                   thumb_file_id=vn.thumbnail.file_id if vn.thumbnail else "")
    elif m.sticker:
        s = m.sticker
        out.update(media_type="sticker", file_id=s.file_id, file_unique_id=s.file_unique_id,
                   width=s.width, height=s.height, file_size=s.file_size,
                   thumb_file_id=s.thumbnail.file_id if s.thumbnail else "")
    elif m.document:
        d = m.document
        out.update(media_type="document", file_id=d.file_id, file_unique_id=d.file_unique_id,
                   file_name=d.file_name or "", file_size=d.file_size,
                   thumb_file_id=d.thumbnail.file_id if d.thumbnail else "")
    return out


def upsert_user(user: Any) -> None:
    if not user:
        return
    db.exec(
        "INSERT OR REPLACE INTO users(id, username, first_name, last_name, is_bot, is_premium, photo_file_id, raw_json)"
        " VALUES (?,?,?,?,?,?,COALESCE((SELECT photo_file_id FROM users WHERE id=?), NULL), ?)",
        (
            user.id,
            getattr(user, "username", "") or "",
            getattr(user, "first_name", "") or "",
            getattr(user, "last_name", "") or "",
            int(bool(getattr(user, "is_bot", False))),
            int(bool(getattr(user, "is_premium", False))),
            user.id,
            json.dumps(serialize_user(user), ensure_ascii=False),
        ),
    )


def upsert_chat(chat: Any, last_message_id: int | None = None, last_message_at: int | None = None) -> None:
    if not chat:
        return
    title = getattr(chat, "title", None) or (
        " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]))
        or getattr(chat, "username", "") or str(chat.id)
    )
    db.exec(
        "INSERT INTO chats(id, type, title, username, last_message_id, last_message_at, raw_json)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username, type=excluded.type,"
        "   last_message_id=COALESCE(excluded.last_message_id, chats.last_message_id),"
        "   last_message_at=COALESCE(excluded.last_message_at, chats.last_message_at),"
        "   raw_json=excluded.raw_json",
        (
            chat.id,
            str(chat.type),
            title,
            getattr(chat, "username", "") or "",
            last_message_id,
            last_message_at,
            json.dumps(serialize_chat(chat), ensure_ascii=False),
        ),
    )


def store_message(msg: Any, deleted: int = 0) -> dict[str, Any]:
    """Persist a Message object to DB and return a serialized dict."""
    media = detect_media(msg)
    media_type = media["media_type"]
    file_id = media["file_id"]
    file_unique_id = media["file_unique_id"]
    sender = msg.from_user
    sender_chat = msg.sender_chat
    text = msg.text or ""
    caption = msg.caption or ""
    entities = serialize_entities(msg.entities or msg.caption_entities)
    reply_markup = serialize_reply_markup(msg.reply_markup)
    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    poll = serialize_poll(msg.poll) if msg.poll else None
    poll_id = msg.poll.id if msg.poll else None
    sticker_emoji = ""
    sticker_set = ""
    sticker_is_animated = 0
    sticker_is_video = 0
    sticker_type = ""
    if msg.sticker:
        s = msg.sticker
        sticker_emoji = s.emoji or ""
        sticker_set = s.set_name or ""
        sticker_is_animated = int(bool(getattr(s, "is_animated", False)))
        sticker_is_video = int(bool(getattr(s, "is_video", False)))
        sticker_type = str(getattr(s, "type", "") or "")
    media_group_id = getattr(msg, "media_group_id", None) or None

    upsert_chat(msg.chat, last_message_id=msg.message_id, last_message_at=int(msg.date.timestamp()))
    if sender:
        upsert_user(sender)
    if sender_chat:
        upsert_chat(sender_chat)

    raw = {
        "chat": serialize_chat(msg.chat),
        "from": serialize_user(sender),
        "sender_chat": serialize_chat(sender_chat),
        "text": text,
        "caption": caption,
        "media_type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "thumb_file_id": media["thumb_file_id"],
        "media_group_id": media_group_id,
        "entities": entities,
        "reply_markup": reply_markup,
        "poll": poll,
        "reply_to": reply_to,
        "sticker_emoji": sticker_emoji,
        "sticker_set": sticker_set,
        "sticker_is_animated": sticker_is_animated,
        "sticker_is_video": sticker_is_video,
        "sticker_type": sticker_type,
        "width": media["width"],
        "height": media["height"],
        "duration": media["duration"],
        "file_name": media["file_name"],
        "file_size": media["file_size"],
    }

    db.exec(
        "INSERT OR REPLACE INTO messages("
        " chat_id, message_id, sender_id, sender_chat_id, date, edit_date,"
        " text, caption, entities_json, media_type, file_id, file_unique_id,"
        " thumb_file_id, media_group_id,"
        " sticker_emoji, sticker_set, sticker_is_animated, sticker_is_video, sticker_type,"
        " poll_id, poll_json, reply_to_message_id, forward_from_id, reply_markup_json,"
        " reactions_json, deleted, via_bot_id,"
        " width, height, duration, file_name, file_size, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        " COALESCE((SELECT reactions_json FROM messages WHERE chat_id=? AND message_id=?), '[]'),?,?,?,?,?,?,?,?)",
        (
            msg.chat.id,
            msg.message_id,
            sender.id if sender else None,
            sender_chat.id if sender_chat else None,
            int(msg.date.timestamp()),
            int(msg.edit_date.timestamp()) if msg.edit_date else None,
            text,
            caption,
            json.dumps(entities, ensure_ascii=False),
            media_type,
            file_id,
            file_unique_id,
            media["thumb_file_id"],
            media_group_id,
            sticker_emoji,
            sticker_set,
            sticker_is_animated,
            sticker_is_video,
            sticker_type,
            poll_id,
            json.dumps(poll, ensure_ascii=False) if poll else None,
            reply_to,
            (msg.forward_from.id if getattr(msg, "forward_from", None) else None),
            json.dumps(reply_markup, ensure_ascii=False) if reply_markup else None,
            msg.chat.id,
            msg.message_id,
            deleted,
            (msg.via_bot.id if getattr(msg, "via_bot", None) else None),
            media["width"],
            media["height"],
            media["duration"],
            media["file_name"],
            media["file_size"],
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return {
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "date": int(msg.date.timestamp()),
        "edit_date": int(msg.edit_date.timestamp()) if msg.edit_date else None,
        "sender_id": sender.id if sender else None,
        "text": text,
        "caption": caption,
        "entities": entities,
        "media_type": media_type,
        "file_id": file_id,
        "thumb_file_id": media["thumb_file_id"],
        "media_group_id": media_group_id,
        "sticker_emoji": sticker_emoji,
        "sticker_set": sticker_set,
        "sticker_is_animated": sticker_is_animated,
        "sticker_is_video": sticker_is_video,
        "sticker_type": sticker_type,
        "width": media["width"],
        "height": media["height"],
        "duration": media["duration"],
        "file_name": media["file_name"],
        "file_size": media["file_size"],
        "poll": poll,
        "reply_to_message_id": reply_to,
        "reply_markup": reply_markup,
        "deleted": deleted,
    }


# ============================================================================
# Auto-moderation
# ============================================================================


_link_re = re.compile(r"https?://|t\.me/|telegram\.me/|@[A-Za-z0-9_]{4,}", re.IGNORECASE)
_user_history: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=50))


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters)


async def ai_moderation_score(text: str, model_id: str) -> float | None:
    """Ask the AI: is this text toxic/spam? Returns 0..100 or None on error."""
    key, real = model_key(model_id)
    if not key:
        return None
    prompt = (
        "You are a strict but fair chat moderator. Output ONLY a JSON object "
        '{"toxicity": int 0-100, "spam": int 0-100, "reason": "short string"}. '
        "Rate the following user message:\n\n" + text
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": real,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", content, re.S)
                if not m:
                    return None
                obj = json.loads(m.group(0))
                return float(max(obj.get("toxicity", 0), obj.get("spam", 0)))
    except Exception as e:  # noqa: BLE001
        log.warning("AI moderation failed: %s", e)
        return None


async def moderate(bot: Bot, msg: Any) -> bool:
    """Run auto-moderation on a message. Returns True if action was taken."""
    chat_id = msg.chat.id
    user = msg.from_user
    if not user or user.id == TG_OWNER_ID:
        return False
    row = db.one("SELECT * FROM moderation WHERE chat_id=?", (chat_id,))
    if not row or not row["enabled"]:
        return False
    settings = row_to_dict(row) or {}
    text = (msg.text or msg.caption or "").strip()
    reason = ""

    # banwords
    try:
        banwords = json.loads(settings.get("banwords_json") or "[]")
    except Exception:
        banwords = []
    for w in banwords:
        if w and w.lower() in text.lower():
            reason = f"banword: {w}"
            break

    # links
    if not reason and settings.get("antilinks") and _link_re.search(text):
        reason = "link"

    # caps
    if not reason and settings.get("anticaps") and len(text) >= 6:
        thr = (settings.get("caps_threshold") or 70) / 100
        if caps_ratio(text) >= thr:
            reason = "caps"

    # antiflood
    if not reason and settings.get("antiflood"):
        hist = _user_history[(chat_id, user.id)]
        now = time.time()
        hist.append(now)
        window = settings.get("antiflood_seconds") or 5
        limit = settings.get("antiflood_count") or 5
        recent = [t for t in hist if now - t <= window]
        if len(recent) > limit:
            reason = "flood"

    # AI moderation (only run if cheap checks passed and AI enabled)
    if not reason and settings.get("ai_moderation") and text:
        model = settings.get("ai_model") or AI_DEFAULT_MODEL
        score = await ai_moderation_score(text, model)
        if score is not None and score >= (settings.get("ai_threshold") or 70):
            reason = f"ai:{score:.0f}"

    if not reason:
        return False

    action = settings.get("action") or "delete"
    try:
        if action in ("delete", "warn", "mute", "ban"):
            try:
                await bot.delete_message(chat_id, msg.message_id)
            except TelegramError:
                pass
        if action == "ban":
            await bot.ban_chat_member(chat_id, user.id)
        elif action == "mute":
            mins = settings.get("mute_minutes") or 10
            await bot.restrict_chat_member(
                chat_id,
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + mins * 60,
            )
        elif action == "warn":
            row_w = db.one("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            c = (row_w["count"] if row_w else 0) + 1
            db.exec(
                "INSERT INTO warnings(chat_id,user_id,count,last_at) VALUES (?,?,?,?)"
                " ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count, last_at=excluded.last_at",
                (chat_id, user.id, c, now_ts()),
            )
            if c >= (settings.get("warn_threshold") or 3):
                mins = settings.get("mute_minutes") or 10
                with contextlib.suppress(TelegramError):
                    await bot.restrict_chat_member(
                        chat_id,
                        user.id,
                        ChatPermissions(can_send_messages=False),
                        until_date=int(time.time()) + mins * 60,
                    )
    except TelegramError as e:
        log.warning("moderation action failed: %s", e)
    audit("moderation", chat_id=chat_id, user_id=user.id, message=reason)
    await hub.broadcast({"type": "moderation", "chat_id": chat_id, "user_id": user.id, "reason": reason})
    return True


# ============================================================================
# Telegram handlers
# ============================================================================


async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    payload = store_message(msg)
    # Cache thumbnail / sticker eagerly (small files)
    if payload.get("file_id") and payload.get("media_type") in ("photo", "sticker"):
        with contextlib.suppress(Exception):
            await cache_file(context.bot, payload["file_id"])
    await hub.broadcast({"type": "message", "data": payload})
    await moderate(context.bot, msg)


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message or update.edited_channel_post
    if not msg:
        return
    payload = store_message(msg)
    await hub.broadcast({"type": "message_edit", "data": payload})


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    audit("callback", chat_id=q.message.chat.id if q.message else None, user_id=q.from_user.id, message=q.data or "")
    await q.answer()
    await hub.broadcast(
        {
            "type": "callback",
            "data": q.data,
            "from": serialize_user(q.from_user),
            "message_id": q.message.message_id if q.message else None,
            "chat_id": q.message.chat.id if q.message else None,
        }
    )


async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-user reaction change (delivered when bot is admin in the chat,
    or in private chats / small groups)."""
    r = update.message_reaction
    if not r:
        return
    chat_id = r.chat.id
    msg_id = r.message_id
    new = []
    for rt in r.new_reaction or []:
        if isinstance(rt, ReactionTypeEmoji):
            new.append({"type": "emoji", "emoji": rt.emoji})
        elif isinstance(rt, ReactionTypeCustomEmoji):
            new.append({"type": "custom", "custom_emoji_id": rt.custom_emoji_id})
    row = db.one("SELECT reactions_json FROM messages WHERE chat_id=? AND message_id=?", (chat_id, msg_id))
    try:
        existing = json.loads(row["reactions_json"]) if row and row["reactions_json"] else []
    except Exception:
        existing = []
    user_id = r.user.id if r.user else (r.actor_chat.id if r.actor_chat else None)
    # Drop only this user's existing per-user reactions; keep aggregated entries.
    existing = [e for e in existing if e.get("user_id") != user_id or e.get("agg")]
    for n in new:
        n["user_id"] = user_id
        existing.append(n)
    db.exec(
        "UPDATE messages SET reactions_json=? WHERE chat_id=? AND message_id=?",
        (json.dumps(existing, ensure_ascii=False), chat_id, msg_id),
    )
    await hub.broadcast(
        {"type": "reaction", "chat_id": chat_id, "message_id": msg_id, "reactions": existing}
    )


async def on_reaction_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anonymous aggregated reaction counts for large chats / non-admin bots.
    Telegram delivers this instead of per-user reactions when the bot can't see
    individual reactors."""
    rc = update.message_reaction_count
    if not rc:
        return
    chat_id = rc.chat.id
    msg_id = rc.message_id
    aggregated = []
    for rcr in rc.reactions or []:
        rt = getattr(rcr, "type", None)
        count = int(getattr(rcr, "total_count", 0) or 0)
        if count <= 0:
            continue
        if isinstance(rt, ReactionTypeEmoji):
            aggregated.append({"type": "emoji", "emoji": rt.emoji, "count": count, "agg": True})
        elif isinstance(rt, ReactionTypeCustomEmoji):
            aggregated.append({"type": "custom", "custom_emoji_id": rt.custom_emoji_id, "count": count, "agg": True})
    row = db.one("SELECT reactions_json FROM messages WHERE chat_id=? AND message_id=?", (chat_id, msg_id))
    try:
        existing = json.loads(row["reactions_json"]) if row and row["reactions_json"] else []
    except Exception:
        existing = []
    # Keep only per-user entries; replace aggregated ones with fresh data.
    existing = [e for e in existing if not e.get("agg")]
    existing.extend(aggregated)
    db.exec(
        "UPDATE messages SET reactions_json=? WHERE chat_id=? AND message_id=?",
        (json.dumps(existing, ensure_ascii=False), chat_id, msg_id),
    )
    await hub.broadcast(
        {"type": "reaction", "chat_id": chat_id, "message_id": msg_id, "reactions": existing}
    )


async def on_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll-state updates (vote counts changed)."""
    p = update.poll
    if not p:
        return
    poll_data = serialize_poll(p)
    db.exec(
        "UPDATE messages SET poll_json=? WHERE poll_id=?",
        (json.dumps(poll_data, ensure_ascii=False), p.id),
    )
    await hub.broadcast({"type": "poll", "poll_id": p.id, "poll": poll_data})


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A user voted in a non-anonymous poll."""
    pa = update.poll_answer
    if not pa:
        return
    user = pa.user
    if user:
        upsert_user(user)
    options = list(pa.option_ids or [])
    if not options:
        # Retracted vote
        db.exec("DELETE FROM poll_voters WHERE poll_id=? AND user_id=?",
                (pa.poll_id, user.id if user else 0))
    else:
        db.exec(
            "INSERT OR REPLACE INTO poll_voters(poll_id, user_id, option_ids_json, voted_at)"
            " VALUES (?,?,?,?)",
            (pa.poll_id, user.id if user else 0,
             json.dumps(options), now_ts()),
        )
    await hub.broadcast({
        "type": "poll_vote",
        "poll_id": pa.poll_id,
        "user_id": user.id if user else None,
        "option_ids": options,
    })


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.my_chat_member
    if not cm:
        return
    upsert_chat(cm.chat)
    await hub.broadcast(
        {
            "type": "chat_member",
            "chat": serialize_chat(cm.chat),
            "status": cm.new_chat_member.status if cm.new_chat_member else "",
        }
    )


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.chat_member
    if not cm:
        return
    upsert_user(cm.new_chat_member.user if cm.new_chat_member else None)
    upsert_chat(cm.chat)
    await hub.broadcast(
        {
            "type": "member",
            "chat_id": cm.chat.id,
            "user": serialize_user(cm.new_chat_member.user) if cm.new_chat_member else None,
            "status": cm.new_chat_member.status if cm.new_chat_member else "",
        }
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "TG Studio is online.\n"
        "Owner: @" + (TG_OWNER_USERNAME or "<unset>") + "\n"
        "Use the web UI to manage me."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        f"chat_id: <code>{msg.chat.id}</code>\nyour_id: <code>{msg.from_user.id if msg.from_user else 0}</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================================
# OpenRouter clients
# ============================================================================


async def openrouter_chat(model_id: str, messages: list[dict[str, Any]], reasoning: bool = False) -> dict[str, Any]:
    key, real = model_key(model_id)
    if not key:
        return {"error": "No API key configured for this model"}
    body: dict[str, Any] = {"model": real, "messages": messages}
    if reasoning:
        body["reasoning"] = {"enabled": True}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                return await r.json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def openrouter_image(model_id: str, prompt: str) -> dict[str, Any]:
    key, real = model_key(model_id)
    if not key:
        return {"error": "No API key configured for this model"}
    body = {
        "model": real,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                return await r.json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ============================================================================
# HTTP API
# ============================================================================


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(request: web.Request) -> bool:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer:
        host = peer[0]
        # Strip IPv4-mapped IPv6 prefix
        if host.startswith("::ffff:"):
            host = host[7:]
        if host in LOOPBACK_HOSTS:
            return True
    return False


@web.middleware
async def auth_mw(request: web.Request, handler: Any) -> web.StreamResponse:
    # Auth disabled entirely (default).
    if not AUTH_ENABLED:
        return await handler(request)
    path = request.path
    # Allow static assets, root index, ws upgrade (handled inside)
    if path.startswith(("/api/auth", "/ws", "/file/", "/static/", "/favicon", "/manifest", "/sw.js")) or path == "/" or path == "/index.html":
        return await handler(request)
    # Trust loopback — same pattern as Jupyter/devtools. The web UI is for
    # local admin use; if you open it on the same machine, no password.
    if _is_loopback(request):
        return await handler(request)
    token = request.cookies.get("token") or request.headers.get("X-Token") or request.query.get("token", "")
    if token != WEB_ACCESS_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def get_bot(request: web.Request) -> Bot:
    return request.app["bot"]


_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


async def api_auth(request: web.Request) -> web.Response:
    # Auth disabled — always green-light.
    if not AUTH_ENABLED:
        resp = web.json_response({"ok": True, "token": "", "auth_required": False})
        return resp
    # Loopback gets free pass — return the real token so the SPA can use it.
    if _is_loopback(request):
        resp = web.json_response({"ok": True, "token": WEB_ACCESS_TOKEN})
        resp.set_cookie("token", WEB_ACCESS_TOKEN, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=False)
        return resp
    data = await request.json()
    submitted = (data.get("token") or "").strip()
    if submitted != WEB_ACCESS_TOKEN:
        # Helpful hint if the user pasted a Telegram bot token by mistake.
        hint = None
        if _BOT_TOKEN_RE.match(submitted):
            hint = (
                "Похоже, вы ввели TG_BOT_TOKEN (токен Telegram-бота). "
                "Сюда нужен WEB_ACCESS_TOKEN — отдельный пароль на вход в админ-панель. "
                "Его значение указано в файле .env рядом с app.py и печатается при старте программы в консоль."
            )
        return web.json_response({"ok": False, "error": "Bad token", "hint": hint}, status=401)
    resp = web.json_response({"ok": True, "token": WEB_ACCESS_TOKEN})
    resp.set_cookie("token", WEB_ACCESS_TOKEN, max_age=60 * 60 * 24 * 30, samesite="Lax", httponly=False)
    return resp


async def api_me(request: web.Request) -> web.Response:
    bot: Bot | None = request.app["bot"]
    row = db.one("SELECT value FROM bot_settings WHERE key='description'")
    desc = row["value"] if row else ""
    row2 = db.one("SELECT value FROM bot_settings WHERE key='short_description'")
    short = row2["value"] if row2 else ""
    base = {
        "id": None,
        "username": "",
        "first_name": "",
        "last_name": "",
        "can_join_groups": False,
        "can_read_all_group_messages": False,
        "supports_inline_queries": False,
        "description": desc,
        "short_description": short,
        "owner_id": TG_OWNER_ID,
        "owner_username": TG_OWNER_USERNAME,
        "channel_id": TG_CHANNEL_ID,
        "models": [{"id": m["name"], "kind": m["kind"]} for m in MODEL_REGISTRY],
        "bot_online": False,
    }
    if bot is None:
        base["error"] = "TG_BOT_TOKEN is not set in .env; bot is dormant. Web UI works for AI / mini-apps / browsing logs."
        return web.json_response(base)
    try:
        me = await bot.get_me()
        base.update({
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "last_name": me.last_name or "",
            "can_join_groups": me.can_join_groups,
            "can_read_all_group_messages": me.can_read_all_group_messages,
            "supports_inline_queries": me.supports_inline_queries,
            "bot_online": True,
        })
        return web.json_response(base)
    except Exception as e:  # noqa: BLE001
        base["error"] = str(e)
        return web.json_response(base, status=200)


async def api_chats(request: web.Request) -> web.Response:
    rows = db.query(
        "SELECT c.*, ("
        "  SELECT json_object('text', text, 'caption', caption, 'media_type', media_type,"
        "                     'date', date, 'sender_id', sender_id, 'message_id', message_id, 'deleted', deleted)"
        "  FROM messages m WHERE m.chat_id=c.id ORDER BY date DESC LIMIT 1"
        ") AS last_message"
        " FROM chats c ORDER BY COALESCE(last_message_at,0) DESC LIMIT 500"
    )
    out = []
    for r in rows:
        d = row_to_dict(r) or {}
        if d.get("last_message"):
            try:
                d["last_message"] = json.loads(d["last_message"])
            except Exception:
                d["last_message"] = None
        out.append(d)
    return web.json_response(out)


async def api_messages(request: web.Request) -> web.Response:
    chat_id = int(request.match_info["chat_id"])
    before = int(request.query.get("before", "0") or 0)
    limit = min(int(request.query.get("limit", "100") or 100), 500)
    if before:
        rows = db.query(
            "SELECT * FROM messages WHERE chat_id=? AND date<? ORDER BY date DESC LIMIT ?",
            (chat_id, before, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY date DESC LIMIT ?",
            (chat_id, limit),
        )
    msgs = [row_to_dict(r) for r in rows][::-1]
    # parse JSON fields client-side via parse, but pre-parse for convenience
    for m in msgs:
        if not m:
            continue
        for f in ("entities_json", "poll_json", "reply_markup_json", "reactions_json"):
            v = m.get(f)
            if v:
                try:
                    m[f.replace("_json", "")] = json.loads(v)
                except Exception:
                    m[f.replace("_json", "")] = None
            else:
                m[f.replace("_json", "")] = None
    # Attach sender meta
    sender_ids = {m["sender_id"] for m in msgs if m and m.get("sender_id")}
    senders = {}
    if sender_ids:
        ph = ",".join("?" * len(sender_ids))
        rows = db.query(f"SELECT * FROM users WHERE id IN ({ph})", tuple(sender_ids))
        senders = {r["id"]: row_to_dict(r) for r in rows}
    return web.json_response({"messages": msgs, "senders": senders})


async def api_send(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    text = data.get("text", "")
    parse_mode = data.get("parse_mode")
    reply_to = data.get("reply_to_message_id")
    disable_preview = bool(data.get("disable_link_preview"))
    inline_kb = data.get("inline_keyboard")
    reply_markup = None
    if inline_kb:
        rows = []
        for r in inline_kb:
            rrow = []
            for b in r:
                if b.get("url"):
                    rrow.append(InlineKeyboardButton(b["text"], url=b["url"]))
                else:
                    rrow.append(InlineKeyboardButton(b["text"], callback_data=b.get("callback_data") or b["text"]))
            rows.append(rrow)
        reply_markup = InlineKeyboardMarkup(rows)
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to,
            link_preview_options=LinkPreviewOptions(is_disabled=disable_preview) if disable_preview else None,
            reply_markup=reply_markup,
        )
        store_message(msg)
        return web.json_response({"ok": True, "message_id": msg.message_id})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_edit(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    message_id = int(data["message_id"])
    text = data.get("text", "")
    try:
        msg = await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
        if hasattr(msg, "message_id"):
            store_message(msg)
        return web.json_response({"ok": True})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_delete(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    message_id = int(data["message_id"])
    try:
        await bot.delete_message(chat_id, message_id)
        db.exec("UPDATE messages SET deleted=1 WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        await hub.broadcast({"type": "delete", "chat_id": chat_id, "message_id": message_id})
        return web.json_response({"ok": True})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_set_reaction(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    message_id = int(data["message_id"])
    emoji = data.get("emoji")
    reactions = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=reactions, is_big=False)
        return web.json_response({"ok": True})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_send_photo(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    reader = await request.multipart()
    chat_id = None
    caption = ""
    reply_to = None
    file_bytes: bytes = b""
    filename = "photo.bin"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "chat_id":
            chat_id = int(await part.text())
        elif part.name == "caption":
            caption = await part.text()
        elif part.name == "reply_to_message_id":
            reply_to = int(await part.text())
        elif part.name == "file":
            filename = part.filename or filename
            file_bytes = await part.read(decode=False)
    if chat_id is None:
        return web.json_response({"ok": False, "error": "missing chat_id"}, status=400)
    try:
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            msg = await bot.send_photo(chat_id=chat_id, photo=file_bytes, caption=caption, reply_to_message_id=reply_to)
        else:
            msg = await bot.send_document(chat_id=chat_id, document=file_bytes, filename=filename, caption=caption, reply_to_message_id=reply_to)
        store_message(msg)
        return web.json_response({"ok": True, "message_id": msg.message_id})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_send_poll(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    question = data.get("question", "")
    options = data.get("options", [])
    anonymous = bool(data.get("anonymous", True))
    multiple = bool(data.get("multiple", False))
    try:
        msg = await bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=anonymous,
            allows_multiple_answers=multiple,
        )
        store_message(msg)
        return web.json_response({"ok": True, "message_id": msg.message_id})
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_set_profile(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    out: dict[str, Any] = {"ok": True}
    try:
        if "name" in data:
            await bot.set_my_name(data["name"])
            out["name"] = data["name"]
        if "description" in data:
            await bot.set_my_description(data["description"])
            db.exec("INSERT OR REPLACE INTO bot_settings(key,value) VALUES ('description', ?)", (data["description"],))
            out["description"] = data["description"]
        if "short_description" in data:
            await bot.set_my_short_description(data["short_description"])
            db.exec("INSERT OR REPLACE INTO bot_settings(key,value) VALUES ('short_description', ?)", (data["short_description"],))
            out["short_description"] = data["short_description"]
        if "commands" in data:
            cmds = [BotCommand(c["command"], c["description"]) for c in data["commands"]]
            await bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
            out["commands"] = data["commands"]
    except TelegramError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response(out)


async def api_moderation_get(request: web.Request) -> web.Response:
    chat_id = int(request.match_info["chat_id"])
    row = db.one("SELECT * FROM moderation WHERE chat_id=?", (chat_id,))
    if not row:
        return web.json_response(
            {
                "chat_id": chat_id,
                "enabled": 0,
                "banwords": [],
                "antiflood": 0,
                "antiflood_count": 5,
                "antiflood_seconds": 5,
                "antilinks": 0,
                "anticaps": 0,
                "caps_threshold": 70,
                "ai_moderation": 0,
                "ai_threshold": 70,
                "ai_model": AI_DEFAULT_MODEL,
                "action": "delete",
                "mute_minutes": 10,
                "warn_threshold": 3,
            }
        )
    d = row_to_dict(row) or {}
    try:
        d["banwords"] = json.loads(d.pop("banwords_json", "[]") or "[]")
    except Exception:
        d["banwords"] = []
    return web.json_response(d)


async def api_moderation_set(request: web.Request) -> web.Response:
    data = await request.json()
    chat_id = int(data["chat_id"])
    fields = {
        "enabled": int(bool(data.get("enabled"))),
        "banwords_json": json.dumps(data.get("banwords") or [], ensure_ascii=False),
        "antiflood": int(bool(data.get("antiflood"))),
        "antiflood_count": int(data.get("antiflood_count", 5)),
        "antiflood_seconds": int(data.get("antiflood_seconds", 5)),
        "antilinks": int(bool(data.get("antilinks"))),
        "anticaps": int(bool(data.get("anticaps"))),
        "caps_threshold": int(data.get("caps_threshold", 70)),
        "ai_moderation": int(bool(data.get("ai_moderation"))),
        "ai_threshold": int(data.get("ai_threshold", 70)),
        "ai_model": data.get("ai_model") or AI_DEFAULT_MODEL,
        "action": data.get("action") or "delete",
        "mute_minutes": int(data.get("mute_minutes", 10)),
        "warn_threshold": int(data.get("warn_threshold", 3)),
    }
    cols = ",".join(fields.keys())
    placeholders = ",".join(["?"] * len(fields))
    update = ",".join([f"{k}=excluded.{k}" for k in fields])
    db.exec(
        f"INSERT INTO moderation(chat_id,{cols}) VALUES (?,{placeholders}) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {update}",
        (chat_id, *fields.values()),
    )
    return web.json_response({"ok": True})


async def api_ai_chat(request: web.Request) -> web.Response:
    data = await request.json()
    model = data.get("model", AI_DEFAULT_MODEL)
    messages = data.get("messages", [])
    reasoning = bool(data.get("reasoning", False))
    resp = await openrouter_chat(model, messages, reasoning=reasoning)
    return web.json_response(resp)


async def api_ai_image(request: web.Request) -> web.Response:
    data = await request.json()
    model = data.get("model", "black-forest-labs/flux.2-pro")
    prompt = data.get("prompt", "")
    resp = await openrouter_image(model, prompt)
    return web.json_response(resp)


async def api_mini_apps(request: web.Request) -> web.Response:
    if request.method == "GET":
        rows = db.query("SELECT id, name, description, icon, created_at, updated_at FROM mini_apps ORDER BY updated_at DESC")
        return web.json_response([row_to_dict(r) for r in rows])
    data = await request.json()
    app_id = data.get("id") or uuid.uuid4().hex
    db.exec(
        "INSERT INTO mini_apps(id, name, description, html, icon, created_at, updated_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, html=excluded.html,"
        "  icon=excluded.icon, updated_at=excluded.updated_at",
        (
            app_id,
            data.get("name", "Untitled"),
            data.get("description", ""),
            data.get("html", ""),
            data.get("icon", "🧩"),
            now_ts(),
            now_ts(),
        ),
    )
    return web.json_response({"ok": True, "id": app_id})


async def api_mini_app_html(request: web.Request) -> web.Response:
    app_id = request.match_info["app_id"]
    row = db.one("SELECT * FROM mini_apps WHERE id=?", (app_id,))
    if not row:
        return web.Response(status=404, text="Not found")
    # Wrap with sandbox helpers + storage namespace
    user_html = row["html"] or ""
    full = MINI_APP_WRAPPER.replace("{{TITLE}}", html_lib.escape(row["name"] or "")).replace("{{ID}}", app_id).replace("{{BODY}}", user_html)
    return web.Response(text=full, content_type="text/html")


async def api_mini_app_delete(request: web.Request) -> web.Response:
    app_id = request.match_info["app_id"]
    db.exec("DELETE FROM mini_apps WHERE id=?", (app_id,))
    return web.json_response({"ok": True})


async def api_users(request: web.Request) -> web.Response:
    rows = db.query("SELECT id, username, first_name, last_name, is_bot, is_premium FROM users ORDER BY first_name")
    return web.json_response([row_to_dict(r) for r in rows])


async def api_user_photo(request: web.Request) -> web.Response:
    """Lazy-load a user profile photo via Telegram API."""
    bot: Bot = get_bot(request)
    user_id = int(request.match_info["user_id"])
    row = db.one("SELECT photo_file_id FROM users WHERE id=?", (user_id,))
    if row and row["photo_file_id"]:
        meta = await cache_file(bot, row["photo_file_id"])
        if meta and meta.get("local_path"):
            return web.FileResponse(meta["local_path"])
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0 and photos.photos:
            ph = photos.photos[0][-1]
            db.exec("UPDATE users SET photo_file_id=? WHERE id=?", (ph.file_id, user_id))
            meta = await cache_file(bot, ph.file_id)
            if meta and meta.get("local_path"):
                return web.FileResponse(meta["local_path"])
    except TelegramError:
        pass
    return web.Response(status=404)


async def file_handler(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    file_id = request.match_info["file_id"]
    meta = await cache_file(bot, file_id)
    if not meta or not meta.get("local_path"):
        return web.Response(status=404)
    return web.FileResponse(meta["local_path"])


async def api_chat_photo(request: web.Request) -> web.Response:
    """Lazy-load chat avatar (groups/channels). Uses cached photo_file_id;
    if missing, calls bot.get_chat() to refresh it."""
    bot: Bot = get_bot(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except (TypeError, ValueError):
        return web.Response(status=400)
    row = db.one("SELECT photo_file_id FROM chats WHERE id=?", (chat_id,))
    file_id: Optional[str] = row["photo_file_id"] if row and row["photo_file_id"] else None
    if not file_id and bot:
        try:
            chat = await bot.get_chat(chat_id)
            ph = getattr(chat, "photo", None)
            if ph and getattr(ph, "big_file_id", None):
                file_id = ph.big_file_id
                db.exec("UPDATE chats SET photo_file_id=? WHERE id=?", (file_id, chat_id))
        except TelegramError:
            pass
    if not file_id:
        return web.Response(status=404)
    meta = await cache_file(bot, file_id)
    if meta and meta.get("local_path"):
        return web.FileResponse(meta["local_path"])
    return web.Response(status=404)


async def api_audit(request: web.Request) -> web.Response:
    rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")
    return web.json_response([row_to_dict(r) for r in rows])


async def api_poll_voters(request: web.Request) -> web.Response:
    poll_id = request.match_info["poll_id"]
    voters = db.query(
        "SELECT pv.*, u.username, u.first_name, u.last_name, u.is_premium, u.is_bot"
        " FROM poll_voters pv LEFT JOIN users u ON u.id=pv.user_id"
        " WHERE pv.poll_id=? ORDER BY voted_at DESC",
        (poll_id,),
    )
    out = []
    for r in voters:
        d = row_to_dict(r) or {}
        try:
            d["option_ids"] = json.loads(d.get("option_ids_json") or "[]")
        except Exception:
            d["option_ids"] = []
        out.append(d)
    return web.json_response(out)


async def api_user_profile(request: web.Request) -> web.Response:
    """Return user info + photo file_ids."""
    user_id = int(request.match_info["user_id"])
    bot: Bot | None = request.app["bot"]
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    user = row_to_dict(row) if row else {"id": user_id}
    # Fetch photos via Bot API if missing or stale (>1h)
    photos: list[dict[str, str]] = []
    cached = db.query(
        "SELECT * FROM user_photos WHERE user_id=? ORDER BY idx ASC", (user_id,)
    )
    last_fetch = max((r["fetched_at"] for r in cached), default=0)
    if bot and (not cached or now_ts() - last_fetch > 3600):
        try:
            up = await bot.get_user_profile_photos(user_id, limit=10)
            db.exec("DELETE FROM user_photos WHERE user_id=?", (user_id,))
            for i, pset in enumerate(up.photos):
                if not pset:
                    continue
                ph = pset[-1]
                db.exec(
                    "INSERT OR REPLACE INTO user_photos(user_id, file_id, idx, fetched_at)"
                    " VALUES (?,?,?,?)",
                    (user_id, ph.file_id, i, now_ts()),
                )
                photos.append({"file_id": ph.file_id, "idx": i})
        except TelegramError as e:
            log.debug("get_user_profile_photos failed: %s", e)
    if cached and not photos:
        photos = [{"file_id": r["file_id"], "idx": r["idx"]} for r in cached]
    user["photos"] = photos
    return web.json_response(user)


async def api_custom_emoji(request: web.Request) -> web.Response:
    """Fetch + cache custom emoji metadata.

    POST {ids: [emoji_id, ...]} -> {emoji_id: {file_id, is_animated, is_video,
                                              sticker_set, emoji}}
    """
    bot: Bot | None = request.app["bot"]
    data = await request.json()
    ids = list({str(x) for x in (data.get("ids") or []) if x})
    if not ids:
        return web.json_response({})
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for eid in ids:
        row = db.one("SELECT * FROM custom_emoji WHERE emoji_id=?", (eid,))
        if row and row["fetched_at"]:
            out[eid] = {
                "file_id": row["file_id"],
                "is_animated": bool(row["is_animated"]),
                "is_video": bool(row["is_video"]),
                "sticker_set": row["sticker_set"] or "",
                "emoji": row["emoji"] or "",
            }
        else:
            missing.append(eid)
    if bot and missing:
        try:
            stickers = await bot.get_custom_emoji_stickers(missing)
            for st in stickers:
                eid = st.custom_emoji_id
                db.exec(
                    "INSERT OR REPLACE INTO custom_emoji"
                    "(emoji_id, file_id, is_animated, is_video, sticker_set, emoji, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        eid,
                        st.file_id,
                        int(bool(getattr(st, "is_animated", False))),
                        int(bool(getattr(st, "is_video", False))),
                        st.set_name or "",
                        st.emoji or "",
                        now_ts(),
                    ),
                )
                out[eid] = {
                    "file_id": st.file_id,
                    "is_animated": bool(getattr(st, "is_animated", False)),
                    "is_video": bool(getattr(st, "is_video", False)),
                    "sticker_set": st.set_name or "",
                    "emoji": st.emoji or "",
                }
        except TelegramError as e:
            log.debug("get_custom_emoji_stickers failed: %s", e)
    return web.json_response(out)


async def api_media_group(request: web.Request) -> web.Response:
    group_id = request.match_info["group_id"]
    rows = db.query(
        "SELECT * FROM messages WHERE media_group_id=? ORDER BY date ASC, message_id ASC",
        (group_id,),
    )
    return web.json_response([row_to_dict(r) for r in rows])


async def api_chat_admins(request: web.Request) -> web.Response:
    bot: Bot | None = request.app["bot"]
    chat_id = int(request.match_info["chat_id"])
    if not bot:
        return web.json_response([])
    try:
        admins = await bot.get_chat_administrators(chat_id)
        out = []
        for a in admins:
            u = a.user
            upsert_user(u)
            out.append({
                "user_id": u.id,
                "username": u.username or "",
                "first_name": u.first_name or "",
                "last_name": u.last_name or "",
                "is_premium": bool(getattr(u, "is_premium", False)),
                "is_bot": bool(u.is_bot),
                "status": str(a.status),
                "is_anonymous": bool(getattr(a, "is_anonymous", False)),
                "custom_title": getattr(a, "custom_title", "") or "",
                "can_post_messages": bool(getattr(a, "can_post_messages", False)),
                "can_edit_messages": bool(getattr(a, "can_edit_messages", False)),
                "can_delete_messages": bool(getattr(a, "can_delete_messages", False)),
                "can_invite_users": bool(getattr(a, "can_invite_users", False)),
                "can_pin_messages": bool(getattr(a, "can_pin_messages", False)),
                "can_promote_members": bool(getattr(a, "can_promote_members", False)),
                "can_restrict_members": bool(getattr(a, "can_restrict_members", False)),
            })
        return web.json_response(out)
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_chat_member(request: web.Request) -> web.Response:
    """Kick / ban / unban / mute / unmute a user in a chat."""
    bot: Bot = get_bot(request)
    data = await request.json()
    chat_id = int(data["chat_id"])
    user_id = int(data["user_id"])
    action = data.get("action", "")
    try:
        if action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
        elif action == "unban":
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        elif action == "mute":
            until = int(time.time()) + int(data.get("minutes", 60)) * 60
            await bot.restrict_chat_member(
                chat_id, user_id, ChatPermissions(can_send_messages=False), until_date=until,
            )
        elif action == "unmute":
            await bot.restrict_chat_member(
                chat_id, user_id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        audit(f"member_{action}", chat_id=chat_id, user_id=user_id, message="from web UI")
        return web.json_response({"ok": True})
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_bot_settings_extra(request: web.Request) -> web.Response:
    """Combined endpoint for misc bot settings (menu button, admin rights, etc.)."""
    bot: Bot = get_bot(request)
    data = await request.json()
    op = data.get("op")
    try:
        if op == "set_menu_button":
            kind = data.get("kind", "default")  # default | commands | webapp
            if kind == "default":
                from telegram import MenuButtonDefault
                await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            elif kind == "commands":
                from telegram import MenuButtonCommands
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            elif kind == "webapp":
                from telegram import MenuButtonWebApp, WebAppInfo
                text = data.get("text", "Open")
                url = data.get("url", "")
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text=text, web_app=WebAppInfo(url=url))
                )
            return web.json_response({"ok": True})
        if op == "set_default_admin_rights":
            from telegram import ChatAdministratorRights
            for_channels = bool(data.get("for_channels", False))
            rights = ChatAdministratorRights(
                is_anonymous=bool(data.get("is_anonymous", False)),
                can_manage_chat=bool(data.get("can_manage_chat", True)),
                can_delete_messages=bool(data.get("can_delete_messages", True)),
                can_manage_video_chats=bool(data.get("can_manage_video_chats", False)),
                can_restrict_members=bool(data.get("can_restrict_members", True)),
                can_promote_members=bool(data.get("can_promote_members", False)),
                can_change_info=bool(data.get("can_change_info", False)),
                can_invite_users=bool(data.get("can_invite_users", True)),
                can_post_messages=bool(data.get("can_post_messages", False)) if for_channels else None,
                can_edit_messages=bool(data.get("can_edit_messages", False)) if for_channels else None,
                can_pin_messages=bool(data.get("can_pin_messages", True)) if not for_channels else None,
                can_manage_topics=bool(data.get("can_manage_topics", False)) if not for_channels else None,
            )
            await bot.set_my_default_administrator_rights(rights=rights, for_channels=for_channels)
            return web.json_response({"ok": True})
        if op == "send_chat_action":
            chat_id = int(data["chat_id"])
            action = data.get("action", "typing")
            await bot.send_chat_action(chat_id, action)
            return web.json_response({"ok": True})
        if op == "pin":
            chat_id = int(data["chat_id"])
            mid = int(data["message_id"])
            await bot.pin_chat_message(chat_id, mid, disable_notification=bool(data.get("silent", False)))
            return web.json_response({"ok": True})
        if op == "unpin":
            chat_id = int(data["chat_id"])
            mid = int(data.get("message_id", 0)) or None
            if mid:
                await bot.unpin_chat_message(chat_id, mid)
            else:
                await bot.unpin_all_chat_messages(chat_id)
            return web.json_response({"ok": True})
        if op == "leave_chat":
            chat_id = int(data["chat_id"])
            await bot.leave_chat(chat_id)
            return web.json_response({"ok": True})
        if op == "set_channel_photo":
            chat_id = int(data["chat_id"])
            b64 = data.get("image_base64", "")
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            buf = io.BytesIO(base64.b64decode(b64))
            buf.name = "avatar.jpg"
            await bot.set_chat_photo(chat_id, buf)
            return web.json_response({"ok": True})
        return web.json_response({"error": "unknown op"}, status=400)
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_forward(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    from_chat = int(data["from_chat_id"])
    to_chat = int(data["to_chat_id"])
    mid = int(data["message_id"])
    try:
        msg = await bot.forward_message(to_chat, from_chat, mid)
        m = store_message(msg)
        await hub.broadcast({"type": "new_message", **m})
        return web.json_response({"ok": True, "message_id": msg.message_id})
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_copy(request: web.Request) -> web.Response:
    bot: Bot = get_bot(request)
    data = await request.json()
    from_chat = int(data["from_chat_id"])
    to_chat = int(data["to_chat_id"])
    mid = int(data["message_id"])
    try:
        m = await bot.copy_message(to_chat, from_chat, mid)
        return web.json_response({"ok": True, "message_id": m.message_id})
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_send_media_group(request: web.Request) -> web.Response:
    """Send an album of photos / videos / docs."""
    bot: Bot = get_bot(request)
    reader = await request.multipart()
    chat_id = 0
    files: list[tuple[str, bytes, str]] = []
    caption = ""
    while True:
        part = await reader.next()
        if not part:
            break
        name = part.name
        if name == "chat_id":
            chat_id = int(await part.text())
        elif name == "caption":
            caption = await part.text()
        elif name and name.startswith("file"):
            content = await part.read(decode=False)
            files.append((part.filename or "file", content, part.headers.get("Content-Type", "")))
    if not chat_id or not files:
        return web.json_response({"error": "chat_id and files required"}, status=400)
    media = []
    for i, (fname, content, mime) in enumerate(files):
        bio = io.BytesIO(content)
        bio.name = fname
        cap = caption if i == 0 else None
        if mime.startswith("video"):
            from telegram import InputMediaVideo
            media.append(InputMediaVideo(media=bio, caption=cap))
        elif mime.startswith("image"):
            media.append(InputMediaPhoto(media=bio, caption=cap))
        else:
            from telegram import InputMediaDocument
            media.append(InputMediaDocument(media=bio, caption=cap))
    try:
        msgs = await bot.send_media_group(chat_id, media)
        out_msgs = []
        for msg in msgs:
            m = store_message(msg)
            await hub.broadcast({"type": "new_message", **m})
            out_msgs.append(m)
        return web.json_response({"ok": True, "messages": out_msgs})
    except TelegramError as e:
        return web.json_response({"error": str(e)}, status=400)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if AUTH_ENABLED and not _is_loopback(request):
        token = request.query.get("token", "") or request.cookies.get("token", "")
        if token != WEB_ACCESS_TOKEN:
            return web.json_response({"error": "unauthorized"}, status=401)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await hub.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # ping / typing indicators could be relayed here
                pass
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        await hub.remove(ws)
    return ws


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def sw_js(request: web.Request) -> web.Response:
    return web.Response(text=SERVICE_WORKER_JS, content_type="application/javascript")


async def manifest(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "name": "TG Studio",
            "short_name": "TGStudio",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#17212b",
            "theme_color": "#17212b",
            "icons": [
                {
                    "src": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='32' fill='%23229ED9'/><text x='50%25' y='58%25' font-size='110' text-anchor='middle' fill='white'>TG</text></svg>",
                    "sizes": "192x192",
                    "type": "image/svg+xml",
                }
            ],
        }
    )


# ============================================================================
# App lifecycle
# ============================================================================


async def build_telegram_app() -> Application:
    if not TG_BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN is not set; please configure .env")
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.ALL & (~filters.UpdateType.EDITED), on_any_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, on_edited_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageReactionHandler(on_reaction, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED))
    app.add_handler(MessageReactionHandler(on_reaction_count, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(PollHandler(on_poll))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app


async def run() -> None:
    if not TG_BOT_TOKEN:
        log.warning("TG_BOT_TOKEN is not set. Starting in WEB-ONLY mode (bot is dormant).")
        bot = None
        tg_app = None
    else:
        tg_app = await build_telegram_app()
        await tg_app.initialize()
        bot = tg_app.bot

    web_app = web.Application(middlewares=[auth_mw], client_max_size=64 * 1024 * 1024)
    web_app["bot"] = bot

    # Routes
    web_app.router.add_get("/", index)
    web_app.router.add_get("/index.html", index)
    web_app.router.add_get("/sw.js", sw_js)
    web_app.router.add_get("/manifest.webmanifest", manifest)
    web_app.router.add_post("/api/auth", api_auth)
    web_app.router.add_get("/api/me", api_me)
    web_app.router.add_get("/api/chats", api_chats)
    web_app.router.add_get("/api/chats/{chat_id}/messages", api_messages)
    web_app.router.add_post("/api/send", api_send)
    web_app.router.add_post("/api/edit", api_edit)
    web_app.router.add_post("/api/delete", api_delete)
    web_app.router.add_post("/api/reaction", api_set_reaction)
    web_app.router.add_post("/api/send_photo", api_send_photo)
    web_app.router.add_post("/api/send_poll", api_send_poll)
    web_app.router.add_post("/api/profile", api_set_profile)
    web_app.router.add_get("/api/moderation/{chat_id}", api_moderation_get)
    web_app.router.add_post("/api/moderation", api_moderation_set)
    web_app.router.add_post("/api/ai/chat", api_ai_chat)
    web_app.router.add_post("/api/ai/image", api_ai_image)
    web_app.router.add_get("/api/mini_apps", api_mini_apps)
    web_app.router.add_post("/api/mini_apps", api_mini_apps)
    web_app.router.add_get("/mini/{app_id}", api_mini_app_html)
    web_app.router.add_delete("/api/mini_apps/{app_id}", api_mini_app_delete)
    web_app.router.add_get("/api/users", api_users)
    web_app.router.add_get("/api/users/{user_id}/photo", api_user_photo)
    web_app.router.add_get("/api/chats/{chat_id}/photo", api_chat_photo)
    web_app.router.add_get("/api/users/{user_id}/profile", api_user_profile)
    web_app.router.add_get("/api/poll/{poll_id}/voters", api_poll_voters)
    web_app.router.add_post("/api/custom_emoji", api_custom_emoji)
    web_app.router.add_get("/api/media_group/{group_id}", api_media_group)
    web_app.router.add_get("/api/chats/{chat_id}/admins", api_chat_admins)
    web_app.router.add_post("/api/chat_member", api_chat_member)
    web_app.router.add_post("/api/bot/settings", api_bot_settings_extra)
    web_app.router.add_post("/api/forward", api_forward)
    web_app.router.add_post("/api/copy_message", api_copy)
    web_app.router.add_post("/api/send_media_group", api_send_media_group)
    web_app.router.add_get("/file/{file_id}", file_handler)
    web_app.router.add_get("/api/audit", api_audit)
    web_app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    # Pretty, hard-to-miss startup banner with the URL.
    banner_host = "localhost" if WEB_HOST in ("0.0.0.0", "::") else WEB_HOST
    log.info("-" * 72)
    log.info("TG Studio v4 запущен.")
    if AUTH_ENABLED:
        log.info("Откройте: http://%s:%s/?token=%s", banner_host, WEB_PORT, WEB_ACCESS_TOKEN)
        log.info("(с этого компьютера можно и просто http://%s:%s/ без токена)", banner_host, WEB_PORT)
    else:
        log.info("Откройте: http://%s:%s/", banner_host, WEB_PORT)
        log.info("Авторизация выключена (WEB_ACCESS_TOKEN пустой в .env). Это ок для локального использования.")
    log.info("-" * 72)

    if tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(
            allowed_updates=[
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "callback_query",
                "message_reaction",
                "message_reaction_count",
                "my_chat_member",
                "chat_member",
                "poll",
                "poll_answer",
                "chat_join_request",
            ],
            drop_pending_updates=False,
        )

        me = await bot.get_me()
        log.info("Bot online as @%s (id=%s)", me.username, me.id)

    stop_event = asyncio.Event()

    def _shutdown(*_: Any) -> None:  # noqa: ANN401
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(__import__("signal"), sig), _shutdown)
        except (NotImplementedError, AttributeError):
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        if tg_app:
            with contextlib.suppress(Exception):
                await tg_app.updater.stop()
            with contextlib.suppress(Exception):
                await tg_app.stop()
            with contextlib.suppress(Exception):
                await tg_app.shutdown()
        await runner.cleanup()


# ============================================================================
# Embedded HTML / CSS / JS  (filled in next; see __INJECT_HTML__ marker)
# ============================================================================

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1, user-scalable=no, shrink-to-fit=no">
<meta name="theme-color" content="#17212b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="TG Studio">
<meta name="format-detection" content="telephone=no, address=no, email=no">
<link rel="manifest" href="/manifest.webmanifest">
<title>TG Studio</title>
<style>
/* =========================================================================
   Telegram-fidelity dark theme + native-feeling app shell
   ========================================================================= */
:root {
  --tg-bg:#17212b;
  --tg-bg-2:#0e1621;
  --tg-side:#17212b;
  --tg-side-item:#202b36;
  --tg-side-item-active:#2b5278;
  --tg-side-item-hover:#202b36;
  --tg-chat-bg:#0e1621;
  --tg-bubble-in:#182533;
  --tg-bubble-out:#2b5278;
  --tg-bubble-radius:14px;
  --tg-text:#ffffff;
  --tg-text-2:#a8b9cc;
  --tg-text-3:#7d8e9f;
  --tg-accent:#64baf0;
  --tg-accent-2:#317CB5;
  --tg-link:#62a9e8;
  --tg-divider:#0a131c;
  --tg-divider-2:#222d3a;
  --tg-premium:#f7c456;
  --tg-premium-2:#e9a73b;
  --tg-online:#4dcd5e;
  --tg-error:#e53935;
  --tg-system-bubble:rgba(0,0,0,.36);
  --tg-selection:rgba(91,144,201,.32);
  --tg-shadow:0 6px 24px rgba(0,0,0,.34);
  --tg-shadow-2:0 1px 2px rgba(0,0,0,.18);
  --tg-row-h:72px;
  --side-w:336px;
  --header-h:56px;
  --composer-h:54px;
  --safe-t: env(safe-area-inset-top, 0px);
  --safe-b: env(safe-area-inset-bottom, 0px);
  --safe-l: env(safe-area-inset-left, 0px);
  --safe-r: env(safe-area-inset-right, 0px);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
               Helvetica, Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
               "WenQuanYi Micro Hei", sans-serif;
}

/* ---------- Reset / native-app behaviors --------------------------------- */
*,*::before,*::after { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { height: 100%; margin: 0; padding: 0; background: var(--tg-bg-2); color: var(--tg-text); }
html { overscroll-behavior: none; }
body {
  -webkit-user-select: none;
  user-select: none;
  -webkit-touch-callout: none;
  touch-action: manipulation;
  overflow: hidden;
  font-size: 14.5px;
  line-height: 1.4;
  letter-spacing: 0;
}

/* Selectable surfaces - just where it makes sense */
.selectable, .selectable *,
.bubble-text, .bubble-caption,
input[type="text"], input[type="search"], input[type="password"],
input[type="number"], input[type="url"], input[type="email"],
textarea, [contenteditable="true"] {
  -webkit-user-select: text;
  user-select: text;
  -webkit-touch-callout: default;
}

/* Inputs */
input, textarea, button, select { font-family: inherit; font-size: inherit; color: inherit; }
input:focus, textarea:focus, button:focus { outline: none; }
button { background: none; border: 0; cursor: pointer; padding: 0; }

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.15); border-radius: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::selection { background: var(--tg-selection); }

/* Make [hidden] win against later display:grid/display:flex declarations */
[hidden] { display: none !important; }

/* ---------- Auth gate ---------------------------------------------------- */
.gate {
  position: fixed; inset: 0; z-index: 1000;
  display: grid; place-items: center;
  background: linear-gradient(135deg, #1c2733 0%, #0a131c 100%);
  padding: 24px;
}
.gate-card {
  width: min(380px, 100%);
  background: var(--tg-bg);
  border-radius: 16px;
  padding: 24px;
  box-shadow: var(--tg-shadow);
}
.gate-logo {
  width: 80px; height: 80px;
  margin: 0 auto 16px;
  background: linear-gradient(135deg, #4ea4e5 0%, #2b5278 100%);
  border-radius: 50%;
  display: grid; place-items: center;
}
.gate-logo svg { width: 44px; height: 44px; fill: white; }
.gate h1 { font-size: 22px; text-align: center; margin: 0 0 8px; font-weight: 600; }
.gate p  { color: var(--tg-text-2); text-align: center; margin: 0 0 16px; line-height: 1.45; }
.gate .gate-sub  { font-size: 13px; margin-bottom: 18px; }
.gate .gate-sub code { background: rgba(100,186,240,.10); padding: 1px 5px; border-radius: 4px; color: var(--tg-accent); }
.gate .gate-warn { color: #f06c6c; font-size: 12px; }
.gate .gate-hint { font-size: 12px; color: var(--tg-text-3, var(--tg-text-2)); margin: 14px 0 0; opacity: .8; }
.gate-err { background: rgba(240,108,108,.12); color: #ff8585; border-radius: 8px; padding: 10px 12px; font-size: 13px; margin: 0 0 12px; line-height: 1.4; }
.gate input {
  width: 100%; background: var(--tg-bg-2); color: var(--tg-text);
  border: 1px solid #2c3845; padding: 14px 16px; border-radius: 10px;
  font-size: 16px; margin-bottom: 16px;
}
.gate input:focus { border-color: var(--tg-accent); }
.gate button.primary {
  width: 100%; background: var(--tg-accent); color: white;
  padding: 13px; border-radius: 10px; font-weight: 600; font-size: 15px;
  transition: opacity .15s;
}
.gate button.primary:active { opacity: .85; }

/* ---------- App shell ---------------------------------------------------- */
.app {
  position: fixed; inset: 0;
  display: grid;
  grid-template-columns: var(--side-w) 1fr;
  background: var(--tg-bg-2);
  padding-left: var(--safe-l); padding-right: var(--safe-r);
}

/* ---------- Sidebar ------------------------------------------------------ */
.side {
  position: relative;
  background: var(--tg-side);
  display: grid;
  grid-template-rows: auto auto 1fr;  /* header / tabs / scrollable chat list */
  border-right: 1px solid var(--tg-divider);
  min-width: 0;
  height: 100%;
  overflow: hidden;
}
.side-header {
  height: calc(var(--header-h) + var(--safe-t));
  padding-top: var(--safe-t);
  display: flex; align-items: center; gap: 8px;
  padding-left: 14px; padding-right: 10px;
  border-bottom: 1px solid var(--tg-divider);
  background: var(--tg-side);
}
.icon-btn {
  width: 40px; height: 40px;
  display: grid; place-items: center;
  border-radius: 50%;
  color: var(--tg-text-2);
  transition: background .12s, color .12s;
}
.icon-btn:hover { background: rgba(255,255,255,.06); color: var(--tg-text); }
.icon-btn:active { background: rgba(255,255,255,.1); }
.icon-btn svg { width: 22px; height: 22px; fill: currentColor; }
.icon-btn.small { width: 32px; height: 32px; }
.icon-btn.small svg { width: 18px; height: 18px; }

.search-wrap {
  flex: 1;
  display: flex; align-items: center;
  background: var(--tg-bg-2);
  border-radius: 22px;
  padding: 8px 14px; height: 38px;
}
.search-wrap svg { width: 18px; height: 18px; fill: var(--tg-text-3); margin-right: 8px; flex-shrink: 0; }
.search-wrap input {
  flex: 1; background: transparent; border: 0; color: var(--tg-text);
  font-size: 14.5px;
}
.search-wrap input::placeholder { color: var(--tg-text-3); }

.side-tabs {
  display: flex; gap: 4px; padding: 6px 10px 4px;
  border-bottom: 1px solid var(--tg-divider);
  overflow-x: auto; scrollbar-width: none;
}
.side-tabs::-webkit-scrollbar { display: none; }
.side-tab {
  padding: 6px 12px; border-radius: 16px; font-size: 13.5px;
  color: var(--tg-text-2); white-space: nowrap;
  transition: background .12s, color .12s;
}
.side-tab.active { color: var(--tg-text); background: var(--tg-side-item); }
.side-tab .count {
  display: inline-block; min-width: 18px; height: 18px;
  background: var(--tg-accent); color: white; border-radius: 9px;
  font-size: 11px; line-height: 18px; text-align: center;
  margin-left: 6px; padding: 0 5px;
}

.side-body { overflow-y: auto; min-height: 0; }

/* Chat row */
.chat-row {
  display: grid;
  grid-template-columns: 54px 1fr auto;
  grid-template-rows: auto auto;
  gap: 0 12px;
  padding: 10px 14px;
  cursor: pointer;
  transition: background .12s;
  border-bottom: 1px solid transparent;
}
.chat-row:hover { background: var(--tg-side-item-hover); }
.chat-row.active { background: var(--tg-side-item-active); }
.chat-row.active .chat-row-preview { color: rgba(255,255,255,.7); }
.chat-row.active .chat-row-time { color: rgba(255,255,255,.7); }
.chat-row .avatar { grid-row: span 2; }
.chat-row-top {
  grid-column: 2; grid-row: 1;
  display: flex; align-items: center; gap: 6px;
  min-width: 0; overflow: hidden;
}
.chat-row-title {
  font-weight: 500; font-size: 15px;
  white-space: nowrap; text-overflow: ellipsis; overflow: hidden;
  display: flex; align-items: center; gap: 6px;
  min-width: 0;
}
.chat-row-title > span:first-child { overflow: hidden; text-overflow: ellipsis; }
.chat-row-time {
  grid-column: 3; grid-row: 1;
  color: var(--tg-text-3); font-size: 12px;
  white-space: nowrap;
}
.chat-row-preview {
  grid-column: 2; grid-row: 2;
  font-size: 13.5px; color: var(--tg-text-2);
  white-space: nowrap; text-overflow: ellipsis; overflow: hidden;
  min-width: 0;
}
.chat-row-meta {
  grid-column: 3; grid-row: 2;
  display: flex; align-items: center; gap: 4px;
  justify-content: flex-end;
}
.chat-row-meta .badge {
  min-width: 20px; height: 20px; padding: 0 6px;
  background: var(--tg-accent); color: white;
  border-radius: 10px; font-size: 12px; font-weight: 600;
  line-height: 20px; text-align: center;
}
.chat-row-meta .badge.muted { background: var(--tg-text-3); }

/* Avatars */
.avatar {
  width: 54px; height: 54px; border-radius: 50%;
  display: grid; place-items: center;
  color: white; font-weight: 600; font-size: 20px;
  background: linear-gradient(135deg, #6699ff 0%, #4071c2 100%);
  flex-shrink: 0; overflow: hidden;
  user-select: none;
}
.avatar.s32 { width: 32px; height: 32px; font-size: 13px; }
.avatar.s40 { width: 40px; height: 40px; font-size: 16px; }
.avatar.s96 { width: 96px; height: 96px; font-size: 36px; }
.avatar.s120 { width: 120px; height: 120px; font-size: 48px; }
.avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }

/* Telegram-style gradient avatars (8 colors as in Telegram clients) */
.av-c1 { background: linear-gradient(135deg, #ff885e, #ff516a); }
.av-c2 { background: linear-gradient(135deg, #ffcd6a, #ffa85c); }
.av-c3 { background: linear-gradient(135deg, #82b1ff, #665fff); }
.av-c4 { background: linear-gradient(135deg, #a0de7e, #54cb68); }
.av-c5 { background: linear-gradient(135deg, #53edd6, #28c9b7); }
.av-c6 { background: linear-gradient(135deg, #72d5fd, #2a9ef1); }
.av-c7 { background: linear-gradient(135deg, #e0a2f3, #d669ed); }
.av-c8 { background: linear-gradient(135deg, #b1c1f3, #7079ec); }

/* Per-user name colors (mirrors avatar palette but as a solid foreground color).
   Used on .bubble-name and .bubble-reply-name. */
.nc-1 { color: #ff7e6c; }
.nc-2 { color: #ffb961; }
.nc-3 { color: #78a2ff; }
.nc-4 { color: #7ddc7e; }
.nc-5 { color: #4cd7c8; }
.nc-6 { color: #4fb9f6; }
.nc-7 { color: #d780eb; }
.nc-8 { color: #8b95f0; }

.badge-star {
  display: inline-block;
  background: linear-gradient(135deg, var(--tg-premium) 0%, var(--tg-premium-2) 100%);
  -webkit-background-clip: text; background-clip: text;
  color: transparent;
  margin-left: 2px;
  font-size: 15px;
  line-height: 1;
  flex-shrink: 0;
}
.tag-bot, .tag-verified, .tag-channel, .tag-group {
  display: inline-block;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .3px;
  vertical-align: middle;
  margin-left: 4px;
  flex-shrink: 0;
}
.tag-bot      { background: rgba(100,186,240,.18); color: var(--tg-accent); }
.tag-verified { background: var(--tg-accent); color: white; }
.tag-channel  { background: rgba(247,196,86,.18); color: var(--tg-premium); }
.tag-group    { background: rgba(160,222,126,.18); color: #a0de7e; }

/* check marks */
.tick {
  display: inline-block; vertical-align: middle;
  width: 14px; height: 14px; margin-left: 2px;
  background: currentColor;
  -webkit-mask: var(--tick-mask) center/contain no-repeat;
          mask: var(--tick-mask) center/contain no-repeat;
}
.tick-1 { --tick-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M5 12.5l4 4 10-10'/></svg>"); }
.tick-2 { --tick-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M2 12.5l4 4 8-8 M10 12.5l4 4 8-8'/></svg>"); }

/* ---------- Main chat area ---------------------------------------------- */
.main {
  position: relative;
  display: grid;
  grid-template-rows: auto 1fr auto;
  background: var(--tg-chat-bg);
  min-width: 0;
  height: 100%;
  overflow: hidden;
}
.main:empty,
.main.placeholder {
  background:
    radial-gradient(circle at 20% 20%, rgba(100,186,240,.04), transparent 40%),
    radial-gradient(circle at 80% 80%, rgba(247,196,86,.04), transparent 40%),
    var(--tg-chat-bg);
}

.empty-state {
  display: grid; place-items: center;
  text-align: center;
  color: var(--tg-text-3); padding: 24px;
  background: var(--tg-system-bubble);
  border-radius: 16px;
  width: fit-content; margin: auto;
  align-self: center; justify-self: center;
  grid-row: 1 / -1;
}

/* Chat header */
.chat-header {
  height: calc(var(--header-h) + var(--safe-t));
  padding-top: var(--safe-t);
  display: flex; align-items: center; gap: 12px;
  padding-left: 14px; padding-right: 10px;
  background: var(--tg-side);
  border-bottom: 1px solid var(--tg-divider);
  cursor: pointer;
}
.chat-header-meta { flex: 1; min-width: 0; overflow: hidden; }
.chat-header-title {
  font-weight: 500; font-size: 15.5px;
  white-space: nowrap; text-overflow: ellipsis; overflow: hidden;
  display: flex; align-items: center; gap: 6px;
}
.chat-header-status { font-size: 13px; color: var(--tg-text-3); white-space: nowrap; }
.chat-header-status.online { color: var(--tg-accent); }
.chat-header-actions { display: flex; align-items: center; }

/* Messages scroller */
.msgs-scroll {
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 8px 8px;
  display: flex; flex-direction: column;
  scroll-behavior: smooth;
  overscroll-behavior: contain;
  position: relative;
  min-height: 0;
}
.msgs-inner {
  display: flex; flex-direction: column;  /* normal order: oldest top, newest bottom */
  width: 100%;
  margin-top: auto;
  max-width: 720px;
  margin-left: auto; margin-right: auto;
}

/* Date separator */
.date-sep {
  align-self: center;
  background: var(--tg-system-bubble);
  color: white;
  padding: 4px 12px;
  border-radius: 14px;
  font-size: 12.5px; font-weight: 500;
  margin: 10px 0;
}

/* Message row */
.msg {
  display: flex; align-items: flex-end;
  padding: 1px 8px;
  position: relative;
  touch-action: pan-y;
}
.msg.out { justify-content: flex-end; }
.msg.in .avatar.s32 { margin-right: 8px; align-self: flex-end; }
.msg-stack { display: grid; grid-template-columns: 1fr; gap: 1px; max-width: 78%; }
.msg.out .msg-stack { align-items: flex-end; }
/* Add visible breathing room between consecutive different senders */
.msg.first-of-stack { padding-top: 10px; }
.msg.last-of-stack { padding-bottom: 2px; }
/* When the row is the LAST of a stack but the next sender is different, give
   a bit more space before the next stack starts. */
.msg + .msg.first-of-stack { margin-top: 2px; }

.bubble {
  position: relative;
  max-width: 100%;
  padding: 7px 10px 5px;
  border-radius: var(--tg-bubble-radius);
  background: var(--tg-bubble-in);
  color: var(--tg-text);
  word-wrap: break-word;
  overflow-wrap: anywhere;
  font-size: 14.5px;
  line-height: 1.32;
  box-shadow: var(--tg-shadow-2);
}
.msg.out .bubble { background: var(--tg-bubble-out); }
.bubble.same-prev { border-top-left-radius: 6px; }
.msg.out .bubble.same-prev { border-top-right-radius: 6px; border-top-left-radius: var(--tg-bubble-radius); }
.bubble.same-next { border-bottom-left-radius: 6px; }
.msg.out .bubble.same-next { border-bottom-right-radius: 6px; border-bottom-left-radius: var(--tg-bubble-radius); }

.bubble.last-in::before, .bubble.last-out::before {
  content: ''; position: absolute; bottom: 0;
  width: 16px; height: 16px;
}
.bubble.last-in::before {
  left: -8px;
  background: radial-gradient(circle at top right, transparent 16px, var(--tg-bubble-in) 16px);
}
.bubble.last-out::before {
  right: -8px;
  background: radial-gradient(circle at top left, transparent 16px, var(--tg-bubble-out) 16px);
}

.bubble-name {
  font-size: 13.5px; font-weight: 600;
  /* Default color used only if no .nc-N class is set. */
  color: var(--tg-accent);
  display: flex; align-items: center; gap: 4px;
  margin-bottom: 2px;
}
/* When a per-user color class is applied, it wins over the defaults. */
.bubble-name.nc-1, .bubble-name.nc-2, .bubble-name.nc-3, .bubble-name.nc-4,
.bubble-name.nc-5, .bubble-name.nc-6, .bubble-name.nc-7, .bubble-name.nc-8 { color: inherit; }
.bubble-name.nc-1 { color: #ff7e6c; }
.bubble-name.nc-2 { color: #ffb961; }
.bubble-name.nc-3 { color: #78a2ff; }
.bubble-name.nc-4 { color: #7ddc7e; }
.bubble-name.nc-5 { color: #4cd7c8; }
.bubble-name.nc-6 { color: #4fb9f6; }
.bubble-name.nc-7 { color: #d780eb; }
.bubble-name.nc-8 { color: #8b95f0; }
.msg.out .bubble-name { color: #95c8ff; }
.bubble-text { white-space: pre-wrap; word-break: break-word; }
.bubble-text a { color: var(--tg-link); }
.bubble-text code, .bubble-text pre { background: rgba(0,0,0,.25); padding: 1px 4px; border-radius: 4px; font-family: SF Mono, Consolas, monospace; font-size: 13px; }
.bubble-text pre { display: block; padding: 8px; margin: 4px 0; white-space: pre-wrap; }
.bubble-text .mention { color: var(--tg-link); font-weight: 500; }
.bubble-text .spoiler { background: rgba(255,255,255,.18); border-radius: 3px; color: transparent; cursor: pointer; transition: color .2s, background .2s; }
.bubble-text .spoiler.revealed { background: transparent; color: inherit; }
.bubble-caption { margin-top: 4px; font-size: 14.5px; }

.bubble-meta {
  display: inline-flex; align-items: center;
  gap: 2px;
  font-size: 11px; color: var(--tg-text-3);
  float: right;
  margin-left: 6px; margin-top: 2px;
  user-select: none;
  flex-shrink: 0;
}
.msg.out .bubble-meta { color: #84a9d3; }
.bubble-meta .edited { font-style: italic; margin-right: 4px; }
.bubble-meta .tick { color: #84a9d3; }
.bubble.has-meta .bubble-text { display: inline; }
.bubble.has-meta::after { content: ''; display: block; clear: both; }

/* Reply quote */
.bubble-reply {
  border-left: 3px solid var(--tg-accent);
  padding: 4px 8px;
  border-radius: 4px;
  background: rgba(0,0,0,.18);
  font-size: 13px;
  margin-bottom: 4px;
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
}
.bubble-reply-name { color: var(--tg-accent); font-weight: 600; }
.bubble-reply-text { color: var(--tg-text-2); white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }

/* Media */
.bubble.media-only { padding: 2px; background: transparent !important; box-shadow: none; }
.bubble.media-only::before { display: none; }
.media-photo, .media-video {
  border-radius: 12px;
  display: block;
  max-width: 320px;
  max-height: 380px;
  width: 100%; height: auto;
  background: var(--tg-bubble-in);
  cursor: zoom-in;
}
.bubble.media-only .media-photo,
.bubble.media-only .media-video { border-radius: 12px; max-width: 320px; }

.media-group {
  display: grid;
  gap: 2px;
  border-radius: 12px;
  overflow: hidden;
  max-width: 360px;
}
.media-group.cols-1 { grid-template-columns: 1fr; }
.media-group.cols-2 { grid-template-columns: 1fr 1fr; }
.media-group.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
.media-group .mg-item {
  position: relative;
  background: var(--tg-bubble-in);
  aspect-ratio: 1 / 1;
  overflow: hidden;
}
.media-group .mg-item img,
.media-group .mg-item video { width: 100%; height: 100%; object-fit: cover; display: block; cursor: zoom-in; }

.media-doc {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 10px;
  align-items: center;
  padding: 4px 0;
  min-width: 200px;
  cursor: pointer;
}
.media-doc-icon {
  width: 44px; height: 44px;
  border-radius: 50%;
  display: grid; place-items: center;
  background: var(--tg-accent);
}
.media-doc-icon svg { width: 22px; height: 22px; fill: white; }
.media-doc-name { font-weight: 500; font-size: 14.5px; word-break: break-all; }
.media-doc-meta { font-size: 12px; color: var(--tg-text-3); }

.media-audio {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 10px; align-items: center;
  min-width: 240px;
}
.media-audio audio { display: none; }  /* hidden — we drive playback ourselves */
.media-audio .tg-player {
  display: flex; flex-direction: column; gap: 4px;
  min-width: 0;
}
.media-audio .tg-player-row {
  display: flex; align-items: center; gap: 8px;
}
.media-audio .tg-player-progress {
  flex: 1; height: 3px; background: rgba(255,255,255,.18);
  border-radius: 2px; position: relative; cursor: pointer;
  min-width: 80px;
}
.media-audio .tg-player-fill {
  position: absolute; top: 0; left: 0; height: 100%;
  background: var(--tg-accent); border-radius: 2px;
  transition: width .15s linear;
}
.msg.out .media-audio .tg-player-fill { background: #fff; }
.media-audio .tg-player-time {
  font-size: 12px; color: var(--tg-text-2);
  font-variant-numeric: tabular-nums;
  min-width: 38px;
}
.msg.out .media-audio .tg-player-time { color: rgba(255,255,255,.78); }
.media-audio .tg-player-name {
  font-size: 13.5px; font-weight: 500; color: var(--tg-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.media-audio .media-doc-icon { cursor: pointer; }
.media-audio .media-doc-icon.playing svg { transform: scale(.9); }
.media-audio .media-doc-icon svg.icon-pause,
.media-audio .media-doc-icon.playing svg.icon-play { display: none; }
.media-audio .media-doc-icon.playing svg.icon-pause { display: block; }

.media-voice {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 10px; align-items: center;
  min-width: 220px;
}

.sticker {
  display: block;
  max-width: 200px; max-height: 200px;
  background: transparent;
  user-select: none;
  pointer-events: auto;
  cursor: zoom-in;
}
.bubble.sticker-only {
  background: transparent !important;
  box-shadow: none;
  padding: 2px;
}
.bubble.sticker-only::before { display: none; }
.bubble.sticker-only .bubble-meta { color: var(--tg-text-3); }

/* Custom premium emoji */
.cemoji {
  display: inline-block;
  vertical-align: text-bottom;
  width: 1.2em; height: 1.2em;
  background-position: center; background-size: contain; background-repeat: no-repeat;
}
.cemoji video { width: 100%; height: 100%; object-fit: contain; }

/* Polls */
.poll {
  min-width: 240px; max-width: 360px;
  display: flex; flex-direction: column; gap: 4px;
}
.poll-q { font-weight: 600; margin-bottom: 4px; word-break: break-word; }
.poll-meta { font-size: 12px; color: var(--tg-text-3); margin-bottom: 4px; }
.poll-opt {
  display: grid; grid-template-columns: auto 1fr auto; gap: 8px;
  align-items: center;
  cursor: pointer;
  padding: 6px 0;
  border-radius: 6px;
  position: relative;
}
.poll-opt:hover { background: rgba(255,255,255,.04); }
.poll-opt .opt-pct { font-weight: 600; font-size: 14px; min-width: 32px; text-align: right; color: var(--tg-accent); }
.poll-opt .opt-text { font-size: 14px; }
.poll-opt .opt-bar {
  grid-column: 1 / -1;
  height: 4px;
  background: rgba(100,186,240,.22);
  border-radius: 2px;
  overflow: hidden;
  margin-top: 4px;
}
.poll-opt .opt-bar-fill {
  height: 100%; background: var(--tg-accent);
  border-radius: 2px;
  transition: width .3s;
}
.poll-opt .opt-marker {
  width: 18px; height: 18px;
  border: 2px solid var(--tg-text-3);
  border-radius: 50%;
  flex-shrink: 0;
}
.poll-opt.checked .opt-marker { border-color: var(--tg-accent); background: var(--tg-accent); }
.poll-opt.correct .opt-pct { color: var(--tg-online); }
.poll-opt.correct .opt-bar-fill { background: var(--tg-online); }
.poll-voters-btn {
  margin-top: 6px;
  font-size: 13px;
  color: var(--tg-accent);
  text-align: center;
  padding: 6px;
  border-radius: 6px;
  border-top: 1px solid rgba(255,255,255,.06);
  cursor: pointer;
}
.poll-voters-btn:hover { background: rgba(255,255,255,.04); }

/* Reactions on message */
.reactions {
  display: flex; flex-wrap: wrap; gap: 4px;
  margin-top: 4px;
}
.reaction {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 1px 8px 1px 6px;
  background: rgba(100,186,240,.18);
  border-radius: 11px;
  font-size: 13px;
  color: var(--tg-accent);
  cursor: pointer;
  transition: background .12s;
}
.reaction:hover { background: rgba(100,186,240,.28); }
.reaction.me { background: var(--tg-accent); color: white; }
.reaction .em { font-size: 14px; }

/* Inline keyboard */
.ikb {
  display: grid; gap: 4px;
  margin-top: 4px;
  width: 100%;
}
.ikb-row { display: grid; gap: 4px; grid-auto-flow: column; }
.ikb-btn {
  background: rgba(255,255,255,.08);
  color: var(--tg-text);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13.5px;
  text-align: center;
  cursor: pointer;
  transition: background .12s;
  word-break: break-word;
}
.ikb-btn:hover { background: rgba(255,255,255,.14); }
.ikb-btn .ext { opacity: .55; margin-left: 4px; font-size: 11px; }

/* System message (service) */
.svc {
  align-self: center;
  padding: 4px 12px;
  background: var(--tg-system-bubble);
  color: rgba(255,255,255,.92);
  border-radius: 14px;
  font-size: 13px;
  margin: 4px 0;
  max-width: 80%;
  text-align: center;
}

/* Swipe to reply visual */
.msg { transition: transform .12s ease-out; }
.swipe-reply-indicator {
  position: absolute;
  top: 50%; left: -34px;
  transform: translateY(-50%) scale(0);
  width: 28px; height: 28px;
  border-radius: 50%;
  background: var(--tg-accent);
  display: grid; place-items: center;
  opacity: 0;
  transition: transform .12s, opacity .12s;
}
.swipe-reply-indicator svg { width: 16px; height: 16px; fill: white; }
.msg.out .swipe-reply-indicator { left: auto; right: -34px; }

/* Composer */
.composer-wrap {
  background: var(--tg-side);
  border-top: 1px solid var(--tg-divider);
  padding-bottom: var(--safe-b);
}
.composer {
  display: flex; align-items: flex-end; gap: 6px;
  padding: 6px 10px;
  max-width: 720px; margin: 0 auto;
}
.composer-input {
  flex: 1;
  background: var(--tg-bg-2);
  border-radius: 18px;
  padding: 9px 14px;
  min-height: 36px;
  max-height: 130px;
  overflow-y: auto;
  font-size: 15px;
  outline: none;
  word-break: break-word;
}
.composer-input:empty::before {
  content: attr(data-placeholder);
  color: var(--tg-text-3);
}
.composer-send {
  width: 44px; height: 44px;
  border-radius: 50%;
  background: var(--tg-accent);
  display: grid; place-items: center;
  color: white;
  transition: opacity .12s, transform .12s;
}
.composer-send:active { transform: scale(.92); }
.composer-send svg { width: 22px; height: 22px; fill: white; }

.composer-reply-bar, .composer-edit-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 14px; background: var(--tg-bg-2);
  border-top: 1px solid var(--tg-divider);
}
.composer-reply-bar .stripe {
  width: 3px; height: 32px; background: var(--tg-accent); border-radius: 2px;
}
.composer-reply-bar .meta { flex: 1; min-width: 0; overflow: hidden; }
.composer-reply-bar .title { font-size: 13px; color: var(--tg-accent); font-weight: 600; }
.composer-reply-bar .text { font-size: 13px; color: var(--tg-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Attachment popover */
.attach-pop {
  position: absolute;
  bottom: calc(var(--composer-h) + 12px);
  left: 12px;
  background: var(--tg-side);
  border-radius: 12px;
  padding: 10px;
  box-shadow: var(--tg-shadow);
  display: grid;
  grid-template-columns: repeat(2, 130px);
  gap: 4px;
  z-index: 10;
  transform-origin: bottom left;
  animation: pop .12s ease-out;
}
.attach-pop button {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 8px;
  color: var(--tg-text); font-size: 14px;
  transition: background .1s;
  cursor: pointer;
  text-align: left;
}
.attach-pop button:hover { background: rgba(255,255,255,.06); }
.attach-pop button svg { width: 22px; height: 22px; fill: var(--tg-accent); }

@keyframes pop {
  from { transform: scale(.9); opacity: 0; }
  to   { transform: scale(1); opacity: 1; }
}

/* ---------- Context menu ------------------------------------------------- */
.ctx-menu {
  position: fixed;
  background: #2c3645;
  border-radius: 10px;
  padding: 6px;
  box-shadow: var(--tg-shadow);
  z-index: 200;
  min-width: 180px;
  animation: pop .12s ease-out;
}
.ctx-menu button {
  display: flex; align-items: center; gap: 12px;
  width: 100%; padding: 8px 12px;
  color: var(--tg-text); font-size: 14px;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
}
.ctx-menu button:hover { background: rgba(255,255,255,.08); }
.ctx-menu button.danger { color: var(--tg-error); }
.ctx-menu svg { width: 18px; height: 18px; fill: currentColor; }
.ctx-menu hr { border: 0; border-top: 1px solid var(--tg-divider-2); margin: 4px 0; }

.ctx-react-bar {
  display: flex; gap: 4px;
  padding: 8px;
  background: #2c3645;
  border-radius: 22px;
  margin-bottom: 6px;
  width: max-content;
  max-width: 350px;
  overflow-x: auto;
  scrollbar-width: none;
}
.ctx-react-bar::-webkit-scrollbar { display: none; }
.ctx-react-bar button {
  width: 36px; height: 36px;
  border-radius: 50%;
  display: grid; place-items: center;
  font-size: 22px;
  cursor: pointer;
  transition: transform .1s, background .1s;
}
.ctx-react-bar button:hover { transform: scale(1.15); background: rgba(255,255,255,.06); }

/* ---------- Right drawer (chat info / profile) -------------------------- */
.drawer {
  position: fixed;
  top: 0; right: 0;
  width: 360px;
  height: 100vh;
  height: 100dvh;
  background: var(--tg-side);
  border-left: 1px solid var(--tg-divider);
  z-index: 30;
  transform: translateX(100%);
  transition: transform .22s cubic-bezier(.4,0,.2,1);
  display: grid;
  grid-template-rows: auto 1fr;
  padding-right: var(--safe-r);
}
.drawer.open { transform: translateX(0); }
.drawer-header {
  height: calc(var(--header-h) + var(--safe-t));
  padding-top: var(--safe-t);
  padding-left: 8px; padding-right: 8px;
  display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid var(--tg-divider);
  background: var(--tg-side);
}
.drawer-header .title { flex: 1; font-weight: 500; font-size: 16px; }
.drawer-body { overflow-y: auto; }
.profile-cover {
  position: relative; height: 320px;
  display: grid; place-items: end center;
  background: linear-gradient(135deg, #4ea4e5, #2b5278);
  overflow: hidden;
}
.profile-cover img.full {
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  object-fit: cover;
}
.profile-cover .gradient {
  position: absolute; left: 0; right: 0; bottom: 0;
  height: 60%;
  background: linear-gradient(180deg, transparent, rgba(0,0,0,.7));
}
.profile-cover .ident {
  position: relative; z-index: 2;
  padding: 16px;
  color: white; text-shadow: 0 1px 4px rgba(0,0,0,.6);
}
.profile-cover .name {
  font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 6px;
}
.profile-cover .status { font-size: 13.5px; color: rgba(255,255,255,.86); }

.profile-photos-dots {
  position: absolute; top: 8px; left: 8px; right: 8px;
  z-index: 3;
  display: flex; gap: 4px;
}
.profile-photos-dots .seg { flex: 1; height: 3px; border-radius: 2px; background: rgba(255,255,255,.32); }
.profile-photos-dots .seg.active { background: white; }

.profile-row {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
}
.profile-row:hover { background: rgba(255,255,255,.04); }
.profile-row svg { width: 22px; height: 22px; fill: var(--tg-accent); }
.profile-row .pri { font-size: 15px; }
.profile-row .sec { font-size: 13px; color: var(--tg-text-3); }
.profile-section-title {
  padding: 12px 16px 6px;
  font-size: 13px; color: var(--tg-accent);
  font-weight: 500;
}
.profile-divider { height: 8px; background: var(--tg-bg-2); }

/* ---------- Modal -------------------------------------------------------- */
.modal-back {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.6);
  z-index: 100;
  display: grid; place-items: center;
  padding: 16px;
  animation: fadein .12s;
}
@keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
.modal {
  background: var(--tg-side);
  border-radius: 12px;
  padding: 18px;
  width: min(440px, 100%);
  max-height: 80vh;
  overflow-y: auto;
  box-shadow: var(--tg-shadow);
  animation: modal-in .14s ease-out;
}
@keyframes modal-in {
  from { transform: translateY(8px) scale(.98); opacity: 0; }
  to   { transform: none; opacity: 1; }
}
.modal h3 { margin: 0 0 14px; font-size: 17px; font-weight: 500; }
.modal label { display: block; font-size: 13.5px; color: var(--tg-text-2); margin: 10px 0 6px; }
.modal input, .modal textarea, .modal select {
  width: 100%;
  background: var(--tg-bg-2);
  color: var(--tg-text);
  border: 1px solid var(--tg-divider-2);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 15px;
}
.modal input:focus, .modal textarea:focus, .modal select:focus { border-color: var(--tg-accent); }
.modal textarea { min-height: 80px; resize: vertical; }
.modal .actions {
  display: flex; justify-content: flex-end; gap: 8px;
  margin-top: 16px;
}
.modal .actions button {
  padding: 9px 16px; border-radius: 8px;
  background: rgba(255,255,255,.05);
  color: var(--tg-accent);
  font-size: 14.5px; font-weight: 500;
  transition: background .1s;
}
.modal .actions button:hover { background: rgba(255,255,255,.1); }
.modal .actions button.primary { background: var(--tg-accent); color: white; }
.modal .actions button.primary:hover { background: #5aa9dd; }
.modal .actions button.danger { color: var(--tg-error); }
.modal-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; }
.modal-row .flex { flex: 1; }
.switch {
  position: relative; width: 38px; height: 22px;
  background: #4a5763; border-radius: 11px;
  cursor: pointer; transition: background .15s;
  flex-shrink: 0;
}
.switch::after {
  content: ''; position: absolute; top: 2px; left: 2px;
  width: 18px; height: 18px; border-radius: 50%;
  background: white; transition: transform .15s;
}
.switch.on { background: var(--tg-accent); }
.switch.on::after { transform: translateX(16px); }

/* ---------- Toast -------------------------------------------------------- */
.toast {
  position: fixed; left: 50%; bottom: calc(20px + var(--safe-b));
  transform: translateX(-50%);
  background: rgba(0,0,0,.85);
  color: white;
  padding: 10px 18px;
  border-radius: 10px;
  font-size: 14px;
  z-index: 300;
  pointer-events: none;
  animation: toast-in .2s ease-out;
}
@keyframes toast-in {
  from { transform: translate(-50%, 20px); opacity: 0; }
  to   { transform: translate(-50%, 0);     opacity: 1; }
}

/* ---------- Image viewer (pinch-zoom) ------------------------------------ */
.viewer {
  position: fixed; inset: 0; z-index: 400;
  background: rgba(0,0,0,.92);
  display: grid; place-items: center;
  touch-action: none;
}
.viewer-img {
  max-width: 100vw; max-height: 100vh;
  transform-origin: center center;
  transition: transform .15s;
  user-select: none;
  pointer-events: auto;
}
.viewer-close {
  position: fixed; top: calc(12px + var(--safe-t)); right: 16px;
  width: 40px; height: 40px;
  border-radius: 50%;
  background: rgba(0,0,0,.5);
  color: white;
  display: grid; place-items: center;
  z-index: 401;
}
.viewer-close svg { width: 24px; height: 24px; fill: white; }

/* ---------- Pages (Profile, Moderation, AI, Apps, Settings) -------------- */
.page {
  display: none;
  position: absolute; inset: 0;
  background: var(--tg-chat-bg);
  z-index: 5;
  flex-direction: column;
}
.page.active { display: flex; }
.page-header {
  height: calc(var(--header-h) + var(--safe-t));
  padding-top: var(--safe-t);
  display: flex; align-items: center; gap: 12px;
  padding-left: 14px; padding-right: 10px;
  background: var(--tg-side);
  border-bottom: 1px solid var(--tg-divider);
  flex-shrink: 0;
}
.page-header .title { flex: 1; font-weight: 500; font-size: 16px; }
.page-body { flex: 1; overflow-y: auto; padding: 16px; max-width: 720px; width: 100%; margin: 0 auto; }
.page-section {
  background: var(--tg-side);
  border-radius: 10px;
  padding: 10px 16px;
  margin-bottom: 16px;
}
.page-section h4 { margin: 8px 0 12px; color: var(--tg-accent); font-size: 13px; font-weight: 500; }
.page-row {
  display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid rgba(255,255,255,.05);
}
.page-row:last-child { border-bottom: 0; }
.page-row label { font-size: 14.5px; color: var(--tg-text); }
.page-row .desc { font-size: 12.5px; color: var(--tg-text-3); margin-top: 2px; }

.btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  background: var(--tg-bg-2);
  color: var(--tg-text);
  padding: 8px 14px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  transition: background .12s;
}
.btn:hover { background: rgba(255,255,255,.08); }
.btn.primary { background: var(--tg-accent); color: white; }
.btn.primary:hover { background: #5aa9dd; }
.btn.danger { background: rgba(229,57,53,.18); color: var(--tg-error); }
.btn.danger:hover { background: rgba(229,57,53,.3); }

/* AI tab */
.ai-msgs { display: flex; flex-direction: column; gap: 6px; padding-bottom: 16px; }
.ai-msg-user, .ai-msg-asst {
  padding: 10px 14px; border-radius: 12px; max-width: 80%;
  word-break: break-word; white-space: pre-wrap; line-height: 1.4;
}
.ai-msg-user { align-self: flex-end; background: var(--tg-bubble-out); }
.ai-msg-asst { align-self: flex-start; background: var(--tg-bubble-in); }
.ai-msg-asst img { max-width: 100%; border-radius: 8px; margin-top: 6px; display: block; }

/* ---------- Mobile ------------------------------------------------------- */
@media (max-width: 768px) {
  .app { grid-template-columns: 1fr; }
  .side {
    position: absolute; inset: 0; z-index: 4;
    transition: transform .24s cubic-bezier(.4,0,.2,1);
  }
  .main {
    position: absolute; inset: 0; z-index: 5;
    transform: translateX(100%);
    transition: transform .24s cubic-bezier(.4,0,.2,1);
  }
  body.in-chat .main { transform: translateX(0); }
  body.in-chat .side { transform: translateX(-30%); }
  .drawer { width: 100%; }
  .msg-stack { max-width: 86%; }
  .attach-pop { left: 10px; right: 10px; grid-template-columns: 1fr 1fr; }
  .icon-btn { width: 40px; height: 40px; }

  /* page covers full screen */
  .page { z-index: 6; }
}

/* Slide page transitions */
.page {
  transform: translateX(100%);
  transition: transform .24s cubic-bezier(.4,0,.2,1);
}
.page.active { transform: translateX(0); display: flex; }

/* No-scroll for mobile body */
@media (max-width: 768px) {
  body { padding-bottom: var(--safe-b); }
}

/* Spinner */
.spinner {
  width: 24px; height: 24px;
  border: 2.5px solid rgba(255,255,255,.18);
  border-top-color: var(--tg-accent);
  border-radius: 50%;
  animation: spin 0.75s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Skeleton */
.skel {
  background: linear-gradient(90deg, rgba(255,255,255,.04), rgba(255,255,255,.1), rgba(255,255,255,.04));
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
  border-radius: 8px;
}
@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

/* Settings page tabs (vertical for desktop, top for mobile) */
.settings-grid {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 12px;
}
.settings-tabs {
  display: flex; flex-direction: column; gap: 4px;
}
.settings-tabs button {
  text-align: left;
  padding: 10px 14px;
  border-radius: 8px;
  color: var(--tg-text); font-size: 14.5px;
  background: transparent;
  cursor: pointer;
}
.settings-tabs button.active { background: var(--tg-side-item-active); color: white; }
.settings-tabs button:hover:not(.active) { background: rgba(255,255,255,.06); }
@media (max-width: 768px) {
  .settings-grid { grid-template-columns: 1fr; }
  .settings-tabs { flex-direction: row; flex-wrap: wrap; overflow-x: auto; }
}

/* Forward / share modal user list */
.user-list { display: flex; flex-direction: column; max-height: 50vh; overflow-y: auto; }
.user-list .item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 4px; border-radius: 8px; cursor: pointer;
}
.user-list .item:hover { background: rgba(255,255,255,.04); }
.user-list .item.checked { background: var(--tg-side-item-active); color: white; }
</style>
</head>
<body>
<div id="gate" class="gate" hidden>
  <div class="gate-card">
    <div class="gate-logo">
      <svg viewBox="0 0 240 240"><path d="M120 0C53.7 0 0 53.7 0 120s53.7 120 120 120 120-53.7 120-120S186.3 0 120 0zm59.1 76.8l-19.7 93.6c-1.5 6.8-5.4 8.4-11 5.2l-30.4-22.6-14.7 14.3c-1.6 1.6-3 3-6.2 3l2.2-31.1 56.2-50.8c2.4-2.2-.5-3.4-3.8-1.2L82 117.5l-30.6-9.6c-6.7-2.1-6.8-6.7 1.4-9.9l119.7-46.2c5.5-2 10.4 1.4 8.6 8z"/></svg>
    </div>
    <h1>TG Studio</h1>
    <p class="gate-sub">Это пароль <b>WEB_ACCESS_TOKEN</b> из файла <code>.env</code> рядом с <code>app.py</code>.<br><span class="gate-warn">Это НЕ токен бота из @BotFather.</span></p>
    <input id="tokInp" type="password" placeholder="WEB_ACCESS_TOKEN из .env" autocomplete="off">
    <div id="tokErr" class="gate-err" hidden></div>
    <button class="primary" id="tokBtn">Войти</button>
    <p class="gate-hint">При запуске программа печатает в консоль полную ссылку с уже подставленным токеном — её можно просто открыть.</p>
  </div>
</div>

<div id="app" class="app" hidden>
  <aside class="side">
    <header class="side-header">
      <button class="icon-btn" id="menuBtn" title="Меню">
        <svg viewBox="0 0 24 24"><path d="M3 6h18v2H3zM3 11h18v2H3zM3 16h18v2H3z"/></svg>
      </button>
      <div class="search-wrap">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.8l-.3-.3a6.5 6.5 0 1 0-.7.7l.3.3v.8l5 4.99L20.49 19l-5-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/></svg>
        <input id="searchInp" type="search" placeholder="Поиск">
      </div>
    </header>
    <div class="side-tabs" id="sideTabs">
      <button class="side-tab active" data-tab="all">Все</button>
      <button class="side-tab" data-tab="private">Личные</button>
      <button class="side-tab" data-tab="group">Группы</button>
      <button class="side-tab" data-tab="channel">Каналы</button>
    </div>
    <div class="side-body" id="chatList"></div>
  </aside>

  <main class="main placeholder" id="main">
    <div class="empty-state">
      <div style="font-size:48px; margin-bottom:12px;">💬</div>
      <div style="font-size:15px; color:var(--tg-text);">Выберите чат, чтобы начать общение</div>
      <div style="margin-top:6px;">Или откройте меню → Профиль / Авто-модерация / AI / Мини-апы / Настройки</div>
    </div>
  </main>

  <aside class="drawer" id="drawer"></aside>

  <!-- Pages -->
  <div class="page" id="page-profile"></div>
  <div class="page" id="page-moderation"></div>
  <div class="page" id="page-ai"></div>
  <div class="page" id="page-mini"></div>
  <div class="page" id="page-settings"></div>
  <div class="page" id="page-audit"></div>
</div>

<script>/* =========================================================================
   TG Studio — frontend (Telegram-style messenger UI as a native-app shell)

   Sections:
     0.   Globals + small DOM/format utils
     1.   Auth (gate)
     2.   API client
     3.   WebSocket
     4.   State store (chats, messages, users, ...)
     5.   Sidebar (chat list + search + tabs)
     6.   Chat view (header, messages, composer)
     7.   Message renderers (text/photo/video/sticker/poll/doc/audio/...)
     8.   Context menu + reactions
     9.   Gestures (long-press, swipe-to-reply, edge-swipe back)
     10.  Drawers (right side: chat info / user profile)
     11.  Pages (profile-bot / moderation / AI / mini-apps / settings / audit)
     12.  Image viewer (pinch-zoom)
     13.  Service worker
   ========================================================================= */

'use strict';

// ============= 0. Globals + utils ==========================================

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const TOKEN_KEY = 'tgstudio.token';
// Boot order for the token:
//  1. ?token=... in the URL (after a successful boot we strip it),
//  2. localStorage value from a previous session.
let TOKEN = (() => {
  try {
    const u = new URL(location.href);
    const fromUrl = u.searchParams.get('token');
    if (fromUrl) {
      localStorage.setItem(TOKEN_KEY, fromUrl);
      u.searchParams.delete('token');
      history.replaceState(null, '', u.pathname + (u.search ? u.search : '') + u.hash);
      return fromUrl;
    }
  } catch (e) { /* noop */ }
  return localStorage.getItem(TOKEN_KEY) || '';
})();

function ce(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'html') el.innerHTML = v;
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) c.forEach(x => x != null && el.appendChild(typeof x === 'string' ? document.createTextNode(x) : x));
    else el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

function esc(s) {
  return (s ?? '').toString()
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function avInitial(name) {
  const n = (name || '?').trim();
  if (!n) return '?';
  const parts = n.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return n.slice(0, 2).toUpperCase();
}

function avColor(id) {
  return 'av-c' + nameColorIdx(id);
}
function nameColor(id) {
  return 'nc-' + nameColorIdx(id);
}
function nameColorIdx(id) {
  const n = (typeof id === 'number') ? id : (id ? id.toString().split('').reduce((a, c) => a + c.charCodeAt(0), 0) : 0);
  return ((n % 8) + 8) % 8 + 1;
}

// In-memory cache for avatar URLs we've already verified exist.
// Maps "user:<id>"/"chat:<id>" -> url or null (404 / no photo).
const _avatarCache = new Map();

/**
 * Render a Telegram-style avatar.
 *   id     — entity id (negative for groups/channels, positive for users)
 *   name   — display name (used for initial)
 *   size   — '', 's32', 's40', 's96', 's120'
 *   type   — 'user' | 'chat'  (default 'user'; chats use /api/chats/.../photo)
 * Returns a div containing initials + gradient bg. Asynchronously fetches the
 * real photo and swaps in an <img> when ready.
 */
function renderAvatar(id, name, size, type, extraOpts) {
  type = type || 'user';
  size = size || '';
  const cls = 'avatar ' + (size ? size + ' ' : '') + avColor(id);
  const div = ce('div', Object.assign({ class: cls }, extraOpts || {}), avInitial(name));
  if (!id) return div;
  const key = type + ':' + id;
  // From cache
  if (_avatarCache.has(key)) {
    const url = _avatarCache.get(key);
    if (url) {
      const img = new Image();
      img.src = url;
      img.onload = () => { div.textContent = ''; div.appendChild(img); };
    }
    return div;
  }
  // Negative chat_id needs proper URL encoding.
  const base = type === 'chat'
    ? `/api/chats/${encodeURIComponent(id)}/photo`
    : `/api/users/${encodeURIComponent(id)}/photo`;
  const url = base + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : '');
  const probe = new Image();
  probe.onload = () => {
    _avatarCache.set(key, url);
    div.textContent = '';
    div.appendChild(probe);
  };
  probe.onerror = () => { _avatarCache.set(key, null); };
  probe.src = url;
  return div;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
  }
  const y = today.getFullYear();
  const sameYear = d.getFullYear() === y;
  if (sameYear) {
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
  }
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function fmtFullTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function fmtDateHeading(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const yest = new Date(today); yest.setDate(today.getDate() - 1);
  const dd = new Date(d); dd.setHours(0, 0, 0, 0);
  if (+dd === +today) return 'Сегодня';
  if (+dd === +yest) return 'Вчера';
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: dd.getFullYear() !== today.getFullYear() ? 'numeric' : undefined });
}

function fmtBytes(b) {
  if (!b) return '';
  if (b < 1024) return b + ' Б';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' КБ';
  if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' МБ';
  return (b / 1024 / 1024 / 1024).toFixed(2) + ' ГБ';
}

function fmtDur(s) {
  if (!s) return '0:00';
  const m = Math.floor(s / 60), sec = s % 60;
  return m + ':' + String(sec).padStart(2, '0');
}

function toast(text, ms = 2400) {
  const t = ce('div', { class: 'toast' }, text);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function debounce(fn, ms) {
  let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

// SVG icon helper
const SVG = {
  back:    '<svg viewBox="0 0 24 24"><path d="M19 11H7.83l4.88-4.88c.39-.39.39-1.03 0-1.42-.39-.39-1.02-.39-1.41 0L4.71 11.29c-.39.39-.39 1.02 0 1.41l6.59 6.59c.39.39 1.02.39 1.41 0 .39-.39.39-1.02 0-1.41L7.83 13H19c.55 0 1-.45 1-1s-.45-1-1-1z"/></svg>',
  send:    '<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>',
  more:    '<svg viewBox="0 0 24 24"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg>',
  search:  '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/></svg>',
  attach:  '<svg viewBox="0 0 24 24"><path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5a2.5 2.5 0 1 1 5 0v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5a2.5 2.5 0 0 0 5 0V5a4 4 0 1 0-8 0v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z"/></svg>',
  smile:   '<svg viewBox="0 0 24 24"><path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm3.5-9c.83 0 1.5-.67 1.5-1.5S16.33 8 15.5 8 14 8.67 14 9.5s.67 1.5 1.5 1.5zm-7 0c.83 0 1.5-.67 1.5-1.5S9.33 8 8.5 8 7 8.67 7 9.5 7.67 11 8.5 11zm3.5 6.5c2.33 0 4.31-1.46 5.11-3.5H6.89c.8 2.04 2.78 3.5 5.11 3.5z"/></svg>',
  reply:   '<svg viewBox="0 0 24 24"><path d="M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z"/></svg>',
  edit:    '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>',
  copy:    '<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>',
  trash:   '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>',
  pin:     '<svg viewBox="0 0 24 24"><path d="M16 9V4l1-1V2H7v1l1 1v5L6 12v2h4v6l1 1 1-1v-6h4v-2l-2-3z"/></svg>',
  close:   '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>',
  forward: '<svg viewBox="0 0 24 24"><path d="M12 8V4l8 8-8 8v-4H4V8z"/></svg>',
  download:'<svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>',
  doc:     '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zM6 20V4h7v5h5v11H6z"/></svg>',
  play:    '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  pause:   '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
  mic:     '<svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5a3 3 0 0 0-6 0v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.49 6-3.31 6-6.72h-1.7z"/></svg>',
  poll:    '<svg viewBox="0 0 24 24"><path d="M19 5v14H5V5h14m0-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM7 10h2v7H7zm4-3h2v10h-2zm4 6h2v4h-2z"/></svg>',
  photo:   '<svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>',
  sticker: '<svg viewBox="0 0 24 24"><path d="M19 2H5c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h11l6-6V4c0-1.1-.9-2-2-2zm-6 14v-3h3l-3 3z"/></svg>',
  user:    '<svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>',
  bot:     '<svg viewBox="0 0 24 24"><path d="M20 9V7c0-1.1-.9-2-2-2h-3V3c0-.55-.45-1-1-1h-4c-.55 0-1 .45-1 1v2H6c-1.1 0-2 .9-2 2v2H3c-.55 0-1 .45-1 1v4c0 .55.45 1 1 1h1v2c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-2h1c.55 0 1-.45 1-1v-4c0-.55-.45-1-1-1h-1zM7.5 11.5a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0zm9 0a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0zM8 16h8v1H8z"/></svg>',
  settings:'<svg viewBox="0 0 24 24"><path d="M19.43 12.98c.04-.32.07-.64.07-.98s-.03-.66-.07-.98l2.11-1.65a.51.51 0 0 0 .12-.64l-2-3.46a.5.5 0 0 0-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.5.5 0 0 0 14 2h-4a.5.5 0 0 0-.5.42l-.38 2.65c-.61.25-1.17.58-1.69.98l-2.49-1a.5.5 0 0 0-.61.22l-2 3.46a.5.5 0 0 0 .12.64L4.57 11c-.04.32-.07.65-.07.98s.03.66.07.98L2.46 14.62a.51.51 0 0 0-.12.64l2 3.46a.5.5 0 0 0 .61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.07.25.27.42.5.42h4c.25 0 .45-.17.5-.42l.38-2.65c.61-.25 1.17-.58 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46a.5.5 0 0 0-.12-.64l-2.11-1.66zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"/></svg>',
  shield:  '<svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/></svg>',
  spark:   '<svg viewBox="0 0 24 24"><path d="M19 5v3l3-3-3-3v3H7c-2.21 0-4 1.79-4 4s1.79 4 4 4h10c1.1 0 2 .9 2 2s-.9 2-2 2H5v-3l-3 3 3 3v-3h12c2.21 0 4-1.79 4-4s-1.79-4-4-4H7c-1.1 0-2-.9-2-2s.9-2 2-2h12z"/></svg>',
  cube:    '<svg viewBox="0 0 24 24"><path d="M12 2 4 6v12l8 4 8-4V6l-8-4zm0 2.3L17.85 7 12 9.7 6.15 7 12 4.3zM6 8.45l5 2.3v8.8l-5-2.5V8.45zm12 0v8.6l-5 2.5v-8.8l5-2.3z"/></svg>',
  bell:    '<svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6V11c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>',
  ban:     '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zM4 12c0-4.42 3.58-8 8-8 1.85 0 3.55.63 4.9 1.69L5.69 16.9C4.63 15.55 4 13.85 4 12zm8 8c-1.85 0-3.55-.63-4.9-1.69L18.31 7.1C19.37 8.45 20 10.15 20 12c0 4.42-3.58 8-8 8z"/></svg>',
  link:    '<svg viewBox="0 0 24 24"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z"/></svg>',
};

// ============= 1. Auth gate ================================================

async function showGate() {
  // First: try a no-token auth probe. Server returns ok:true if:
  //  - AUTH_ENABLED is false in .env (default), or
  //  - request is from loopback.
  // In either case we skip the gate entirely.
  try {
    const probe = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (probe.ok) {
      const j = await probe.json().catch(() => ({}));
      if (j && j.ok) {
        TOKEN = j.token || '';
        if (TOKEN) localStorage.setItem(TOKEN_KEY, TOKEN);
        $('#gate').hidden = true;
        $('#app').hidden = false;
        return;
      }
    }
  } catch (e) { /* fall through to interactive gate */ }

  $('#gate').hidden = false;
  $('#app').hidden = true;
  const err = $('#tokErr');
  if (err) { err.hidden = true; err.textContent = ''; }
  $('#tokInp').value = '';
  $('#tokInp').focus();
  return new Promise(resolve => {
    const submit = async () => {
      const v = $('#tokInp').value.trim();
      if (!v) return;
      if (err) { err.hidden = true; err.textContent = ''; }
      try {
        const r = await fetch('/api/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: v }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          let msg = 'Неверный пароль админ-панели.';
          if (j && j.hint) msg = j.hint;
          if (err) { err.textContent = msg; err.hidden = false; }
          return;
        }
        TOKEN = (j && j.token) || v;
        localStorage.setItem(TOKEN_KEY, TOKEN);
        $('#gate').hidden = true;
        $('#app').hidden = false;
        resolve();
      } catch (e) {
        if (err) { err.textContent = 'Ошибка сети. Попробуйте ещё раз.'; err.hidden = false; }
      }
    };
    $('#tokBtn').onclick = submit;
    $('#tokInp').onkeydown = e => { if (e.key === 'Enter') submit(); };
  });
}

// ============= 2. API client ==============================================

async function api(path, opts = {}) {
  const url = new URL(path, location.href);
  url.searchParams.set('token', TOKEN);
  const init = { ...opts };
  if (init.body && typeof init.body === 'object' && !(init.body instanceof FormData)) {
    init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (r.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    let msg = await r.text().catch(() => '');
    try { msg = JSON.parse(msg).error || msg; } catch {}
    throw new Error(msg || ('HTTP ' + r.status));
  }
  return r.json();
}

// ============= 3. WebSocket ===============================================

let ws = null;
let wsBackoff = 1000;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { wsBackoff = 1000; };
  ws.onmessage = ev => {
    let data; try { data = JSON.parse(ev.data); } catch { return; }
    handleEvent(data);
  };
  ws.onclose = () => {
    setTimeout(connectWS, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 15000);
  };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ============= 4. State store =============================================

const state = {
  me: null,           // bot info
  chats: new Map(),   // chat_id -> chat dict
  messages: new Map(),// chat_id -> Map(message_id -> msg)
  users: new Map(),   // user_id -> user dict
  current: null,      // current chat id
  tab: 'all',         // 'all' | 'private' | 'group' | 'channel'
  search: '',
  reply: null,        // {chat_id, message_id, text, name}
  edit: null,         // {chat_id, message_id, text}
  customEmoji: new Map(), // emoji_id -> meta
  page: null,         // open settings page
  drawerKind: null,   // 'chat' | 'user'
};

function getMsgMap(chatId) {
  if (!state.messages.has(chatId)) state.messages.set(chatId, new Map());
  return state.messages.get(chatId);
}

// ============= Event handling =============================================

function handleEvent(data) {
  switch (data.type) {
    case 'message':
    case 'message_edit': {
      const p = data.data || data;
      if (p && p.chat_id) {
        const map = getMsgMap(p.chat_id);
        // Pre-parse json fields if present
        for (const f of ['entities_json', 'poll_json', 'reply_markup_json', 'reactions_json']) {
          if (p[f] && typeof p[f] === 'string') {
            try { p[f.replace('_json', '')] = JSON.parse(p[f]); } catch {}
          }
        }
        map.set(p.message_id, { ...map.get(p.message_id), ...p });
        const c = state.chats.get(p.chat_id);
        if (c) {
          c.last_message = { text: p.text, caption: p.caption, media_type: p.media_type, date: p.date, sender_id: p.sender_id, message_id: p.message_id, deleted: p.deleted };
          c.last_message_at = p.date;
          renderChatList();
        } else {
          loadChats();
        }
        if (state.current === p.chat_id) {
          const s = $('#msgsScroll');
          // Only auto-scroll to bottom if user was already near the bottom.
          const wasAtBottom = !s || (s.scrollHeight - s.scrollTop - s.clientHeight < 80);
          renderMessages();
          if (wasAtBottom && s) requestAnimationFrame(() => { s.scrollTop = s.scrollHeight; });
        }
      }
      break;
    }
    case 'delete': {
      const m = getMsgMap(data.chat_id).get(data.message_id);
      if (m) m.deleted = 1;
      if (state.current === data.chat_id) renderMessages();
      break;
    }
    case 'reaction': {
      const m = getMsgMap(data.chat_id).get(data.message_id);
      if (m) {
        m.reactions = data.reactions;
        if (state.current === data.chat_id) renderMessages();
      }
      break;
    }
    case 'poll': {
      // Update poll inside all messages with this poll_id
      for (const [chatId, msgs] of state.messages) {
        for (const m of msgs.values()) {
          if (m.poll && m.poll.id === data.poll_id) {
            m.poll = data.poll;
            if (chatId === state.current) renderMessages();
          }
        }
      }
      break;
    }
    case 'poll_vote': {
      // Refresh voters of current open poll modal
      if (state._pollVoterModal && state._pollVoterModal.poll_id === data.poll_id) {
        loadPollVoters(data.poll_id);
      }
      break;
    }
    case 'chat_member':
    case 'member': {
      loadChats();
      break;
    }
  }
}

// ============= 5. Sidebar =================================================

let _loadChatsInflight = null;
let _loadChatsQueued = false;
async function loadChats() {
  // Coalesce — many WS events can call us in a tight loop while the bot's
  // initial chat list arrives. One in-flight + one queued is enough.
  if (_loadChatsInflight) { _loadChatsQueued = true; return _loadChatsInflight; }
  _loadChatsInflight = (async () => {
    try {
      const chats = await api('/api/chats');
      state.chats.clear();
      for (const c of chats) state.chats.set(c.id, c);
      renderChatList();
    } catch (e) {
      console.error(e);
    } finally {
      _loadChatsInflight = null;
      if (_loadChatsQueued) {
        _loadChatsQueued = false;
        setTimeout(loadChats, 300);
      }
    }
  })();
  return _loadChatsInflight;
}

function chatRowPreview(c) {
  const lm = c.last_message;
  if (!lm) return ce('span', { class: 'muted' }, 'Нет сообщений');
  if (lm.deleted) return ce('span', {}, ce('i', {}, 'Удалено'));
  const mt = lm.media_type;
  if (mt === 'photo')     return '📷 ' + (lm.caption || 'Фото');
  if (mt === 'video')     return '🎬 ' + (lm.caption || 'Видео');
  if (mt === 'animation') return '🎞 ' + (lm.caption || 'GIF');
  if (mt === 'sticker')   return '🎟 Стикер';
  if (mt === 'voice')     return '🎤 Голосовое';
  if (mt === 'audio')     return '🎵 ' + (lm.caption || 'Аудио');
  if (mt === 'document')  return '📎 ' + (lm.caption || 'Файл');
  if (mt === 'video_note')return '🟢 Кружок';
  if (mt === 'poll')      return '📊 Опрос';
  return (lm.text || lm.caption || '').replace(/\s+/g, ' ').slice(0, 80);
}

function chatTags(c) {
  const out = [];
  if (c.type === 'private' && state.users.get(c.id)?.is_bot) out.push(ce('span', { class: 'tag-bot' }, 'BOT'));
  if (c.type === 'private' && state.users.get(c.id)?.is_premium) out.push(ce('span', { class: 'badge-star', title: 'Premium' }, '★'));
  if (c.type === 'channel') out.push(ce('span', { class: 'tag-channel' }, 'КАНАЛ'));
  if (c.type === 'supergroup' || c.type === 'group') out.push(ce('span', { class: 'tag-group' }, 'ГРУППА'));
  return out;
}

function renderChatList() {
  const list = $('#chatList');
  list.innerHTML = '';
  const arr = Array.from(state.chats.values());
  const filtered = arr.filter(c => {
    if (state.tab === 'private' && c.type !== 'private') return false;
    if (state.tab === 'group' && c.type !== 'group' && c.type !== 'supergroup') return false;
    if (state.tab === 'channel' && c.type !== 'channel') return false;
    if (state.search) {
      const q = state.search.toLowerCase();
      const t = (c.title || '').toLowerCase();
      const u = (c.username || '').toLowerCase();
      if (!t.includes(q) && !u.includes(q)) return false;
    }
    return true;
  });
  filtered.sort((a, b) => (b.last_message_at || 0) - (a.last_message_at || 0));
  for (const c of filtered) {
    const row = ce('div', {
      class: 'chat-row' + (state.current === c.id ? ' active' : ''),
      onclick: () => openChat(c.id),
    });
    row.appendChild(renderAvatar(c.id, c.title || c.username || String(c.id), '', c.type === 'private' ? 'user' : 'chat'));
    const top = ce('div', { class: 'chat-row-top' });
    top.appendChild(ce('div', { class: 'chat-row-title' }, ce('span', {}, c.title || c.username || String(c.id)), ...chatTags(c)));
    row.appendChild(top);
    if (c.last_message_at) row.appendChild(ce('div', { class: 'chat-row-time' }, fmtTime(c.last_message_at)));
    const prev = ce('div', { class: 'chat-row-preview' });
    const pv = chatRowPreview(c);
    if (typeof pv === 'string') prev.textContent = pv;
    else prev.appendChild(pv);
    row.appendChild(prev);
    list.appendChild(row);
  }
  if (!filtered.length) {
    list.appendChild(ce('div', { style: 'padding:24px; text-align:center; color:var(--tg-text-3); font-size:13px' },
      state.search ? 'Ничего не найдено' : 'Бот ещё не получал сообщений — добавьте его в группу или напишите ему в личку.'));
  }
}

function setupSidebar() {
  $$('.side-tab').forEach(t => t.onclick = () => {
    state.tab = t.dataset.tab;
    $$('.side-tab').forEach(x => x.classList.toggle('active', x === t));
    renderChatList();
  });
  $('#searchInp').oninput = debounce(e => {
    state.search = e.target.value;
    renderChatList();
  }, 120);
  $('#menuBtn').onclick = openSideMenu;
}

function openSideMenu(ev) {
  closeContextMenu();
  const menu = ce('div', { class: 'ctx-menu', style: { left: '10px', top: '60px' } });
  const item = (icon, label, fn) => ce('button', { onclick: () => { menu.remove(); fn(); } }, ce('span', { class: 'icon', html: icon }), label);
  menu.append(
    item(SVG.bot, 'Профиль бота', () => openPage('profile')),
    item(SVG.shield, 'Авто-модерация', () => openPage('moderation')),
    item(SVG.spark, 'AI-ассистент', () => openPage('ai')),
    item(SVG.cube, 'Мини-апы', () => openPage('mini')),
    item(SVG.settings, 'Настройки', () => openPage('settings')),
    document.createElement('hr'),
    item(SVG.doc, 'Журнал событий', () => openPage('audit')),
    item(SVG.close, 'Выйти', () => { localStorage.removeItem(TOKEN_KEY); location.reload(); }),
  );
  document.body.appendChild(menu);
  const closeNow = ev2 => { if (!menu.contains(ev2.target)) { menu.remove(); document.removeEventListener('click', closeNow, true); } };
  setTimeout(() => document.addEventListener('click', closeNow, true), 0);
}

// ============= 6. Chat view ===============================================

async function openChat(chatId) {
  state.current = chatId;
  document.body.classList.add('in-chat');
  renderChatList();
  await loadMessages(chatId);
  renderChat();
}

function closeChat() {
  state.current = null;
  document.body.classList.remove('in-chat');
  state.reply = null;
  state.edit = null;
  $('#main').className = 'main placeholder';
  $('#main').innerHTML = `<div class="empty-state">
    <div style="font-size:48px;margin-bottom:12px;">💬</div>
    <div style="font-size:15px;color:var(--tg-text);">Выберите чат, чтобы начать общение</div>
  </div>`;
  renderChatList();
}

async function loadMessages(chatId) {
  try {
    const r = await api(`/api/chats/${chatId}/messages?limit=200`);
    const map = getMsgMap(chatId);
    map.clear();
    for (const m of r.messages || []) map.set(m.message_id, m);
    for (const [uid, u] of Object.entries(r.senders || {})) state.users.set(+uid, u);
  } catch (e) { console.error(e); }
}

function renderChat() {
  const chat = state.chats.get(state.current);
  if (!chat) return;
  const main = $('#main');
  main.className = 'main';
  main.innerHTML = '';

  // Header
  const header = ce('header', { class: 'chat-header' });
  const backBtn = ce('button', { class: 'icon-btn', html: SVG.back, onclick: e => { e.stopPropagation(); closeChat(); } });
  if (window.innerWidth <= 768) header.appendChild(backBtn);
  header.appendChild(renderAvatar(chat.id, chat.title || String(chat.id), 's40', chat.type === 'private' ? 'user' : 'chat'));
  const meta = ce('div', { class: 'chat-header-meta' });
  meta.appendChild(ce('div', { class: 'chat-header-title' }, ce('span', {}, chat.title || String(chat.id)), ...chatTags(chat)));
  const statusText = chatStatus(chat);
  meta.appendChild(ce('div', { class: 'chat-header-status' }, statusText));
  header.appendChild(meta);
  const actions = ce('div', { class: 'chat-header-actions' });
  actions.appendChild(ce('button', { class: 'icon-btn', html: SVG.search, onclick: e => { e.stopPropagation(); /* TODO: in-chat search */ } }));
  actions.appendChild(ce('button', { class: 'icon-btn', html: SVG.more, onclick: e => { e.stopPropagation(); openChatMenu(e); } }));
  header.appendChild(actions);
  header.onclick = () => openChatDrawer(chat);
  main.appendChild(header);

  // Messages
  const scroll = ce('div', { class: 'msgs-scroll', id: 'msgsScroll' });
  const inner = ce('div', { class: 'msgs-inner', id: 'msgsInner' });
  scroll.appendChild(inner);
  main.appendChild(scroll);

  // Composer
  main.appendChild(renderComposer());

  renderMessages();
  setTimeout(() => { scroll.scrollTop = scroll.scrollHeight; }, 0);
}

function chatStatus(c) {
  if (c.type === 'private') {
    const u = state.users.get(c.id);
    if (u && u.is_bot) return 'бот';
    return 'был(а) недавно';
  }
  if (c.members_count) return c.members_count + ' участников';
  if (c.type === 'channel') return 'канал';
  if (c.type === 'supergroup' || c.type === 'group') return 'группа';
  return '';
}

function openChatMenu(ev) {
  closeContextMenu();
  const rect = ev.currentTarget.getBoundingClientRect();
  const menu = ce('div', { class: 'ctx-menu', style: { right: '10px', top: (rect.bottom + 4) + 'px' } });
  const it = (icon, label, fn, danger) => ce('button', { class: danger ? 'danger' : '', onclick: () => { menu.remove(); fn(); } }, ce('span', { html: icon }), label);
  const c = state.chats.get(state.current);
  menu.append(
    it(SVG.user, 'Информация о чате', () => openChatDrawer(c)),
    it(SVG.shield, 'Авто-модерация', () => openModerationFor(c.id)),
    it(SVG.copy, 'Скопировать ID', () => { navigator.clipboard.writeText(String(c.id)); toast('ID скопирован'); }),
    it(SVG.ban, 'Выйти из чата', () => leaveChat(c.id), true),
  );
  document.body.appendChild(menu);
  const closeNow = ev2 => { if (!menu.contains(ev2.target)) { menu.remove(); document.removeEventListener('click', closeNow, true); } };
  setTimeout(() => document.addEventListener('click', closeNow, true), 0);
}

async function leaveChat(chatId) {
  if (!confirm('Бот выйдет из этого чата. Подтвердить?')) return;
  try {
    await api('/api/bot/settings', { method: 'POST', body: { op: 'leave_chat', chat_id: chatId } });
    state.chats.delete(chatId);
    closeChat();
    renderChatList();
    toast('Бот покинул чат');
  } catch (e) { toast('Ошибка: ' + e.message); }
}

// ============= 7. Message renderers =======================================

function renderMessages() {
  const inner = $('#msgsInner'); if (!inner) return;
  inner.innerHTML = '';
  const map = getMsgMap(state.current);
  // Ordered ascending by date — oldest at top, newest at bottom (normal chat order).
  const arr = Array.from(map.values()).sort((a, b) => a.date - b.date);

  // Group by media_group_id
  const groups = new Map();
  for (const m of arr) {
    if (m.media_group_id) {
      if (!groups.has(m.media_group_id)) groups.set(m.media_group_id, []);
      groups.get(m.media_group_id).push(m);
    }
  }
  const renderedGroup = new Set();

  let prevDayKey = null;
  for (let i = 0; i < arr.length; i++) {
    const m = arr[i];
    // Day separator BEFORE first message of each day.
    const dayKey = new Date(m.date * 1000).toDateString();
    if (dayKey !== prevDayKey) {
      inner.appendChild(ce('div', { class: 'date-sep' }, fmtDateHeading(m.date)));
      prevDayKey = dayKey;
    }
    if (m.media_group_id && renderedGroup.has(m.media_group_id)) continue;
    if (m.media_group_id) {
      renderedGroup.add(m.media_group_id);
      const all = groups.get(m.media_group_id).sort((a, b) => a.message_id - b.message_id);
      inner.appendChild(renderMessageGroup(all));
    } else {
      const prev = arr[i - 1];
      const next = arr[i + 1];
      inner.appendChild(renderSingleMessage(m, prev, next));
    }
  }
}

function renderSingleMessage(m, prev, next) {
  if (m.deleted) {
    return ce('div', { class: 'svc' }, 'Сообщение удалено');
  }
  const me = state.me?.id;
  const out = m.sender_id === me;
  const sameSender = prev && prev.sender_id === m.sender_id && !prev.media_group_id && (m.date - prev.date) < 300;
  const sameSenderNext = next && next.sender_id === m.sender_id && !next.media_group_id && (next.date - m.date) < 300;

  let cls = 'msg ' + (out ? 'out' : 'in');
  if (!sameSender) cls += ' first-of-stack';
  if (!sameSenderNext) cls += ' last-of-stack';
  const row = ce('div', { class: cls, dataset: { mid: m.message_id } });
  row._msg = m;

  if (!out && !sameSenderNext) {
    const sender = state.users.get(m.sender_id) || {};
    row.appendChild(renderAvatar(m.sender_id, sender.first_name || sender.username || '?', 's32', 'user', {
      onclick: e => { e.stopPropagation(); openUserDrawer(m.sender_id); }
    }));
  } else if (!out) {
    row.appendChild(ce('div', { class: 'avatar s32', style: { visibility: 'hidden' } }));
  }

  const stack = ce('div', { class: 'msg-stack' });
  stack.appendChild(renderBubble(m, { out, sameSender, sameSenderNext }));
  row.appendChild(stack);

  // Swipe-to-reply indicator
  row.appendChild(ce('div', { class: 'swipe-reply-indicator', html: SVG.reply }));

  attachGestures(row, m);
  return row;
}

function renderMessageGroup(msgs) {
  const m = msgs[0];
  const me = state.me?.id;
  const out = m.sender_id === me;
  const row = ce('div', { class: 'msg ' + (out ? 'out' : 'in') });
  if (!out) {
    const sender = state.users.get(m.sender_id) || {};
    row.appendChild(renderAvatar(m.sender_id, sender.first_name || sender.username || '?', 's32', 'user', {
      onclick: e => { e.stopPropagation(); openUserDrawer(m.sender_id); }
    }));
  }
  const stack = ce('div', { class: 'msg-stack' });
  const bubble = ce('div', { class: 'bubble' + (msgs.some(x => x.media_type === 'document') ? '' : ' media-only') });
  if (!out && (msgs[0].caption || msgs[0].text)) {
    // sender label
    const sender = state.users.get(m.sender_id) || {};
    const nameRow = ce('div', { class: 'bubble-name ' + nameColor(m.sender_id) }, sender.first_name || sender.username || '');
    if (sender.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
    bubble.appendChild(nameRow);
  }

  // Media grid
  const n = msgs.length;
  const cols = n === 1 ? 1 : n === 2 ? 2 : n <= 4 ? 2 : 3;
  const grid = ce('div', { class: 'media-group cols-' + cols });
  for (const mm of msgs) grid.appendChild(renderMediaGridItem(mm));
  bubble.appendChild(grid);

  // Caption from first message that has one
  const caption = msgs.map(x => x.caption || '').find(Boolean) || '';
  if (caption) bubble.appendChild(ce('div', { class: 'bubble-caption', html: linkify(esc(caption)) }));

  bubble.appendChild(renderBubbleMeta(m, out));
  stack.appendChild(bubble);
  row.appendChild(stack);
  row.appendChild(ce('div', { class: 'swipe-reply-indicator', html: SVG.reply }));
  attachGestures(row, m);
  return row;
}

function renderMediaGridItem(m) {
  const item = ce('div', { class: 'mg-item' });
  if (m.media_type === 'photo') {
    item.appendChild(ce('img', { src: '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN), loading: 'lazy', onclick: () => openImageViewer(m.file_id) }));
  } else if (m.media_type === 'video' || m.media_type === 'animation') {
    item.appendChild(ce('video', {
      src: '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN),
      muted: true, loop: true, playsinline: true, preload: 'metadata',
      onclick: () => openImageViewer(m.file_id, 'video'),
    }));
  } else if (m.media_type === 'document') {
    item.appendChild(renderDocBlock(m));
  }
  return item;
}

function renderBubble(m, { out, sameSender, sameSenderNext }) {
  const isSticker = m.media_type === 'sticker';
  const isMedia = ['photo', 'video', 'animation'].includes(m.media_type);
  const bubble = ce('div', { class: 'bubble' });
  if (isSticker) bubble.classList.add('sticker-only');
  if (isMedia && !(m.text || m.caption)) bubble.classList.add('media-only');
  if (sameSender) bubble.classList.add('same-prev');
  if (sameSenderNext) bubble.classList.add('same-next');
  if (!sameSenderNext) bubble.classList.add(out ? 'last-out' : 'last-in');

  // Sender name (groups only, not for own messages, only on top of stack)
  if (!out && !sameSender) {
    const c = state.chats.get(state.current);
    if (c && c.type !== 'private') {
      const sender = state.users.get(m.sender_id) || {};
      const nameRow = ce('div', { class: 'bubble-name ' + nameColor(m.sender_id) }, sender.first_name || sender.username || '');
      if (sender.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
      bubble.appendChild(nameRow);
    }
  }

  // Reply
  if (m.reply_to_message_id) {
    const rep = getMsgMap(state.current).get(m.reply_to_message_id);
    const repSender = rep ? state.users.get(rep.sender_id) : null;
    bubble.appendChild(ce('div', {
      class: 'bubble-reply',
      onclick: e => { e.stopPropagation(); scrollToMessage(m.reply_to_message_id); }
    },
      ce('div', { class: 'bubble-reply-name ' + nameColor(repSender ? (repSender.id || rep?.sender_id || 0) : (rep?.sender_id || 0)) }, repSender ? (repSender.first_name || repSender.username || '') : 'Сообщение'),
      ce('div', { class: 'bubble-reply-text' }, rep ? ((rep.text || rep.caption || mediaLabel(rep) || '').slice(0, 80)) : '...'),
    ));
  }

  // Media (single, not group)
  if (!m.media_group_id && m.media_type) {
    const mediaEl = renderMedia(m);
    if (mediaEl) bubble.appendChild(mediaEl);
  }

  // Text
  const txt = m.text || m.caption || '';
  if (txt && !isSticker) {
    const div = ce('div', { class: (m.media_type ? 'bubble-caption' : 'bubble-text'), html: linkify(applyEntities(txt, m.entities)) });
    bubble.appendChild(div);
    bindCustomEmojiObserver(div);
    bindSpoilers(div);
  }

  // Poll
  if (m.poll) bubble.appendChild(renderPoll(m));

  // Inline keyboard
  if (m.reply_markup && m.reply_markup.inline_keyboard) {
    bubble.appendChild(renderInlineKeyboard(m));
  }

  // Reactions
  if (m.reactions && m.reactions.length) {
    bubble.appendChild(renderReactions(m));
  }

  bubble.appendChild(renderBubbleMeta(m, out));
  bubble.classList.add('has-meta');
  return bubble;
}

function renderBubbleMeta(m, out) {
  const meta = ce('span', { class: 'bubble-meta' });
  if (m.edit_date) meta.appendChild(ce('span', { class: 'edited' }, 'изм.'));
  meta.appendChild(document.createTextNode(fmtTime(m.date)));
  if (out) {
    const tick = ce('span', { class: 'tick tick-2', title: 'Доставлено' });
    meta.appendChild(tick);
  }
  return meta;
}

function mediaLabel(m) {
  const map = { photo: '📷 Фото', video: '🎬 Видео', animation: '🎞 GIF', sticker: '🎟 Стикер',
                voice: '🎤 Голосовое', audio: '🎵 Аудио', document: '📎 Файл',
                video_note: '🟢 Видео-кружок', poll: '📊 Опрос' };
  return map[m.media_type] || '';
}

function renderMedia(m) {
  const url = '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN);
  if (m.media_type === 'photo') {
    return ce('img', {
      class: 'media-photo', src: url, loading: 'lazy',
      onclick: () => openImageViewer(m.file_id),
    });
  }
  if (m.media_type === 'video' || m.media_type === 'animation') {
    return ce('video', {
      class: 'media-video', src: url, controls: m.media_type === 'video' ? 'controls' : false,
      autoplay: m.media_type === 'animation', loop: m.media_type === 'animation',
      muted: m.media_type === 'animation', playsinline: true, preload: 'metadata',
      onclick: () => { if (m.media_type === 'animation') openImageViewer(m.file_id, 'video'); },
    });
  }
  if (m.media_type === 'sticker') {
    // .webp / .webm / .tgs
    if (m.sticker_is_video) {
      return ce('video', {
        class: 'sticker', src: url, autoplay: true, loop: true, muted: true, playsinline: true,
        onclick: () => openImageViewer(m.file_id, 'video'),
      });
    }
    if (m.sticker_is_animated) {
      // Lottie .tgs — we can't easily decode; fall back to thumbnail
      const thumb = m.thumb_file_id ? '/file/' + m.thumb_file_id + '?token=' + encodeURIComponent(TOKEN) : url;
      return ce('img', { class: 'sticker', src: thumb, alt: m.sticker_emoji || '' });
    }
    return ce('img', { class: 'sticker', src: url, alt: m.sticker_emoji || '' });
  }
  if (m.media_type === 'document') return renderDocBlock(m);
  if (m.media_type === 'voice' || m.media_type === 'audio') return renderAudioBlock(m, url);
  if (m.media_type === 'video_note') {
    return ce('video', {
      class: 'sticker',
      style: { borderRadius: '50%', width: '200px', height: '200px', objectFit: 'cover' },
      src: url, controls: 'controls', playsinline: true, preload: 'metadata',
    });
  }
  return null;
}

function renderDocBlock(m) {
  const url = '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN);
  const wrap = ce('div', { class: 'media-doc', onclick: e => { e.stopPropagation(); window.open(url, '_blank'); } });
  wrap.appendChild(ce('div', { class: 'media-doc-icon', html: SVG.doc }));
  const info = ce('div');
  info.appendChild(ce('div', { class: 'media-doc-name' }, m.file_name || 'Файл'));
  info.appendChild(ce('div', { class: 'media-doc-meta' }, fmtBytes(m.file_size)));
  wrap.appendChild(info);
  return wrap;
}

function renderAudioBlock(m, url) {
  const wrap = ce('div', { class: 'media-audio' });
  const icon = ce('div', { class: 'media-doc-icon' });
  // Two SVGs; CSS toggles which one is visible based on .playing.
  const playSvg = document.createElement('span');
  playSvg.innerHTML = SVG.play; playSvg.firstChild.classList.add('icon-play');
  const pauseSvg = document.createElement('span');
  pauseSvg.innerHTML = SVG.pause; pauseSvg.firstChild.classList.add('icon-pause');
  icon.appendChild(playSvg.firstChild);
  icon.appendChild(pauseSvg.firstChild);

  const player = ce('div', { class: 'tg-player' });
  if (m.media_type === 'audio' && m.file_name) {
    player.appendChild(ce('div', { class: 'tg-player-name' }, m.file_name));
  } else if (m.media_type === 'voice') {
    player.appendChild(ce('div', { class: 'tg-player-name' }, 'Голосовое сообщение'));
  }
  const row = ce('div', { class: 'tg-player-row' });
  const progress = ce('div', { class: 'tg-player-progress' });
  const fill = ce('div', { class: 'tg-player-fill', style: { width: '0%' } });
  progress.appendChild(fill);
  const time = ce('div', { class: 'tg-player-time' }, '0:00');
  row.append(progress, time);
  player.appendChild(row);

  const audio = ce('audio', { src: url, preload: 'metadata' });
  wrap.append(icon, player, audio);

  const fmt = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60), x = Math.floor(s % 60);
    return m + ':' + String(x).padStart(2, '0');
  };
  const toggle = (e) => {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    // Pause any other audio currently playing.
    document.querySelectorAll('.media-audio audio').forEach(a => { if (a !== audio && !a.paused) a.pause(); });
    if (audio.paused) audio.play().catch(() => {}); else audio.pause();
  };
  icon.onclick = toggle;
  audio.onplay = () => icon.classList.add('playing');
  audio.onpause = () => icon.classList.remove('playing');
  audio.onended = () => { icon.classList.remove('playing'); fill.style.width = '0%'; time.textContent = fmt(audio.duration || m.duration || 0); };
  audio.onloadedmetadata = () => { time.textContent = fmt(audio.duration || m.duration || 0); };
  audio.ontimeupdate = () => {
    const d = audio.duration || m.duration || 0;
    fill.style.width = (d > 0 ? (audio.currentTime / d * 100) : 0) + '%';
    time.textContent = fmt(d > 0 ? d - audio.currentTime : 0);
  };
  progress.onclick = (e) => {
    const rect = progress.getBoundingClientRect();
    const r = (e.clientX - rect.left) / rect.width;
    const d = audio.duration || 0;
    if (d > 0) audio.currentTime = r * d;
  };
  // Replace play icon with mic for voice messages.
  if (m.media_type === 'voice') {
    icon.querySelector('.icon-play').innerHTML = SVG.mic.match(/<path[^/]*\/>/)[0];
  }
  return wrap;
}

function renderPoll(m) {
  const p = m.poll;
  const wrap = ce('div', { class: 'poll' });
  wrap.appendChild(ce('div', { class: 'poll-q' }, p.question));
  const total = (p.options || []).reduce((a, o) => a + (o.voter_count || 0), 0);
  wrap.appendChild(ce('div', { class: 'poll-meta' },
    p.is_anonymous ? 'Анонимный опрос · ' : 'Открытый опрос · ',
    total + ' голос' + (total % 10 === 1 && total % 100 !== 11 ? '' : total % 10 >= 2 && total % 10 <= 4 && (total % 100 < 12 || total % 100 > 14) ? 'а' : 'ов'),
    p.is_closed ? ' · завершён' : '',
  ));
  for (let i = 0; i < (p.options || []).length; i++) {
    const o = p.options[i];
    const pct = total ? Math.round((o.voter_count || 0) * 100 / total) : 0;
    const opt = ce('div', { class: 'poll-opt' + (o.is_chosen ? ' checked' : '') });
    opt.appendChild(ce('div', { class: 'opt-marker' }));
    opt.appendChild(ce('div', { class: 'opt-text' }, o.text));
    opt.appendChild(ce('div', { class: 'opt-pct' }, pct + '%'));
    const bar = ce('div', { class: 'opt-bar' });
    bar.appendChild(ce('div', { class: 'opt-bar-fill', style: { width: pct + '%' } }));
    opt.appendChild(bar);
    wrap.appendChild(opt);
  }
  if (!p.is_anonymous) {
    wrap.appendChild(ce('div', {
      class: 'poll-voters-btn',
      onclick: e => { e.stopPropagation(); openPollVoters(p.id); },
    }, 'Кто голосовал →'));
  }
  return wrap;
}

function renderInlineKeyboard(m) {
  const ikb = ce('div', { class: 'ikb' });
  for (const row of m.reply_markup.inline_keyboard) {
    const rowEl = ce('div', { class: 'ikb-row' });
    for (const btn of row) {
      const b = ce('div', {
        class: 'ikb-btn',
        onclick: e => {
          e.stopPropagation();
          if (btn.url) window.open(btn.url, '_blank');
          else if (btn.callback_data) toast('Это кнопка callback_data — нажатие исполнит обработчик в боте');
          else if (btn.switch_inline_query) toast('Inline-кнопка');
        },
      }, btn.text);
      if (btn.url) b.appendChild(ce('span', { class: 'ext' }, '↗'));
      rowEl.appendChild(b);
    }
    ikb.appendChild(rowEl);
  }
  return ikb;
}

function renderReactions(m) {
  const wrap = ce('div', { class: 'reactions' });
  // Aggregate by emoji. Reactions array may contain a mix of per-user entries
  // (one row per reactor) and aggregated entries (one row with a count).
  const counts = {};
  const customByEmoji = {};
  let myEmoji = null;
  for (const r of m.reactions || []) {
    let em;
    if (r.type === 'emoji') em = r.emoji;
    else if (r.type === 'custom' || r.type === 'custom_emoji') {
      em = '⭐';  // placeholder, lazy-replaced by getCustomEmojiStickers
      if (r.custom_emoji_id) customByEmoji[em] = r.custom_emoji_id;
    } else em = '?';
    const inc = r.agg ? (r.count || 1) : 1;
    counts[em] = (counts[em] || 0) + inc;
    if (!r.agg && r.user_id === state.me?.id) myEmoji = em;
  }
  for (const [em, n] of Object.entries(counts)) {
    wrap.appendChild(ce('div', {
      class: 'reaction' + (em === myEmoji ? ' me' : ''),
      onclick: e => { e.stopPropagation(); setReaction(m, em === myEmoji ? null : em); },
    }, ce('span', { class: 'em' }, em), n > 0 ? String(n) : ''));
  }
  return wrap;
}

// Text entities (links, bold, italic, mentions, code, spoilers, custom emoji)
function applyEntities(text, entities) {
  if (!entities || !entities.length) return esc(text);
  // entities are byte-offsets in UTF-16, that's how Telegram returns them
  const arr = Array.from(text);
  const events = [];
  for (const e of entities) {
    events.push({ pos: e.offset, type: 'open', e });
    events.push({ pos: e.offset + e.length, type: 'close', e });
  }
  events.sort((a, b) => a.pos - b.pos || (a.type === 'open' ? 1 : -1));
  let out = '';
  let cursor = 0;
  const open = (e) => {
    switch (e.type) {
      case 'bold': return '<b>';
      case 'italic': return '<i>';
      case 'underline': return '<u>';
      case 'strikethrough': return '<s>';
      case 'code': return '<code>';
      case 'pre': return '<pre>';
      case 'spoiler': return '<span class="spoiler">';
      case 'url': return `<a href="${esc(text.substr(e.offset, e.length))}" target="_blank">`;
      case 'text_link': return `<a href="${esc(e.url)}" target="_blank">`;
      case 'mention': return `<span class="mention">`;
      case 'text_mention': return `<span class="mention" data-uid="${e.user?.id || ''}">`;
      case 'custom_emoji': return `<span class="cemoji" data-eid="${esc(e.custom_emoji_id || '')}">`;
      case 'hashtag': case 'cashtag': case 'bot_command': case 'email': case 'phone_number':
        return '<span class="mention">';
      default: return '';
    }
  };
  const close = (e) => {
    switch (e.type) {
      case 'bold': return '</b>';
      case 'italic': return '</i>';
      case 'underline': return '</u>';
      case 'strikethrough': return '</s>';
      case 'code': return '</code>';
      case 'pre': return '</pre>';
      case 'spoiler': return '</span>';
      case 'url': case 'text_link': return '</a>';
      case 'mention': case 'text_mention': return '</span>';
      case 'custom_emoji': return '</span>';
      case 'hashtag': case 'cashtag': case 'bot_command': case 'email': case 'phone_number':
        return '</span>';
      default: return '';
    }
  };
  for (const ev of events) {
    out += esc(arr.slice(cursor, ev.pos).join(''));
    cursor = ev.pos;
    out += ev.type === 'open' ? open(ev.e) : close(ev.e);
  }
  out += esc(arr.slice(cursor).join(''));
  return out;
}

function linkify(html) {
  // Cheap fallback for plain URLs that weren't in entities
  return html.replace(/(^|[\s>])(https?:\/\/[^\s<]+)/g, (m, p, url) =>
    p + `<a href="${esc(url)}" target="_blank">${esc(url)}</a>`);
}

function bindSpoilers(root) {
  $$('.spoiler', root).forEach(s => s.onclick = () => s.classList.add('revealed'));
}

// Custom emoji rendering — fetch their file_ids lazily
const emojiObserver = new IntersectionObserver(async entries => {
  const ids = entries.filter(e => e.isIntersecting).map(e => e.target.dataset.eid).filter(Boolean);
  if (!ids.length) return;
  for (const e of entries) if (e.isIntersecting) emojiObserver.unobserve(e.target);
  const missing = ids.filter(id => !state.customEmoji.has(id));
  if (missing.length) {
    try {
      const r = await api('/api/custom_emoji', { method: 'POST', body: { ids: missing } });
      for (const [id, meta] of Object.entries(r)) state.customEmoji.set(id, meta);
    } catch {}
  }
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const eid = e.target.dataset.eid;
    const meta = state.customEmoji.get(eid);
    if (!meta) continue;
    const url = '/file/' + meta.file_id + '?token=' + encodeURIComponent(TOKEN);
    if (meta.is_video) {
      e.target.innerHTML = '';
      e.target.appendChild(ce('video', { src: url, autoplay: true, loop: true, muted: true, playsinline: true }));
    } else if (!meta.is_animated) {
      e.target.style.backgroundImage = `url('${url}')`;
    } else {
      // Static thumb fallback for lottie
      e.target.style.backgroundImage = `url('${url}')`;
    }
  }
}, { rootMargin: '200px' });

function bindCustomEmojiObserver(root) {
  $$('.cemoji', root).forEach(el => emojiObserver.observe(el));
}

function scrollToMessage(mid) {
  const el = $$('.msg').find(x => +x.dataset.mid === +mid);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.transition = 'background .3s';
    el.style.background = 'rgba(100,186,240,.18)';
    setTimeout(() => { el.style.background = ''; }, 1500);
  }
}

// ============= 8. Context menu + reactions ================================

let _ctxMenuEl = null;
function closeContextMenu() {
  if (_ctxMenuEl) { _ctxMenuEl.remove(); _ctxMenuEl = null; }
}

const QUICK_REACTS = ['👍','❤️','🔥','🥰','👏','😁','🤔','😢','😱','🎉','💯','🤯'];

function showMessageContextMenu(ev, m) {
  closeContextMenu();
  ev.preventDefault();
  const wrap = ce('div', { style: { position: 'fixed', left: 0, top: 0, zIndex: 200 } });
  const bar = ce('div', { class: 'ctx-react-bar' });
  for (const em of QUICK_REACTS) {
    bar.appendChild(ce('button', { onclick: () => { closeContextMenu(); setReaction(m, em); } }, em));
  }
  wrap.appendChild(bar);

  const menu = ce('div', { class: 'ctx-menu', style: { position: 'static' } });
  const it = (icon, label, fn, danger) => ce('button', { class: danger ? 'danger' : '', onclick: () => { closeContextMenu(); fn(); } }, ce('span', { html: icon }), label);
  menu.append(
    it(SVG.reply, 'Ответить', () => startReply(m)),
    it(SVG.copy, 'Скопировать текст', () => { navigator.clipboard.writeText(m.text || m.caption || ''); toast('Скопировано'); }),
    it(SVG.forward, 'Переслать', () => openForwardModal(m)),
    it(SVG.edit, 'Редактировать', () => startEdit(m)),
    it(SVG.pin, 'Закрепить', () => pinMessage(m)),
    document.createElement('hr'),
    it(SVG.trash, 'Удалить', () => deleteMessage(m), true),
  );
  wrap.appendChild(menu);
  document.body.appendChild(wrap);
  _ctxMenuEl = wrap;

  // Position
  const x = ev.clientX || (ev.touches && ev.touches[0].clientX) || 100;
  const y = ev.clientY || (ev.touches && ev.touches[0].clientY) || 100;
  const w = wrap.getBoundingClientRect().width;
  const h = wrap.getBoundingClientRect().height;
  wrap.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
  wrap.style.top  = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';

  const closeNow = ev2 => { if (!wrap.contains(ev2.target)) closeContextMenu(); };
  setTimeout(() => document.addEventListener('click', closeNow, { capture: true, once: true }), 0);
}

async function setReaction(m, emoji) {
  try {
    await api('/api/reaction', { method: 'POST', body: {
      chat_id: m.chat_id, message_id: m.message_id, emoji: emoji,
    }});
  } catch (e) { toast('Ошибка реакции: ' + e.message); }
}

async function deleteMessage(m) {
  if (!confirm('Удалить сообщение?')) return;
  try {
    await api('/api/delete', { method: 'POST', body: { chat_id: m.chat_id, message_id: m.message_id } });
  } catch (e) { toast('Не получилось удалить: ' + e.message); }
}

async function pinMessage(m) {
  try {
    await api('/api/bot/settings', { method: 'POST', body: { op: 'pin', chat_id: m.chat_id, message_id: m.message_id, silent: false } });
    toast('Закреплено');
  } catch (e) { toast('Ошибка: ' + e.message); }
}

function startReply(m) {
  state.reply = m;
  state.edit = null;
  renderComposerBars();
  $('#composerInp')?.focus();
}

function startEdit(m) {
  state.edit = m;
  state.reply = null;
  $('#composerInp').textContent = m.text || m.caption || '';
  renderComposerBars();
  $('#composerInp')?.focus();
}

function openForwardModal(m) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Переслать сообщение'));
  const list = ce('div', { class: 'user-list' });
  const arr = Array.from(state.chats.values()).sort((a, b) => (b.last_message_at || 0) - (a.last_message_at || 0));
  for (const c of arr) {
    const item = ce('div', { class: 'item', onclick: async () => {
      try {
        await api('/api/forward', { method: 'POST', body: { from_chat_id: m.chat_id, to_chat_id: c.id, message_id: m.message_id } });
        toast('Переслано');
      } catch (e) { toast('Ошибка: ' + e.message); }
      back.remove();
    }});
    item.appendChild(renderAvatar(c.id, c.title || String(c.id), 's40', c.type === 'private' ? 'user' : 'chat'));
    item.appendChild(ce('div', {}, ce('div', { style: { fontSize: '14px' } }, c.title || String(c.id)), ce('div', { style: { fontSize: '12px', color: 'var(--tg-text-3)' } }, chatStatus(c))));
    list.appendChild(item);
  }
  modal.appendChild(list);
  modal.appendChild(ce('div', { class: 'actions' }, ce('button', { onclick: () => back.remove() }, 'Отмена')));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

// ============= 9. Gestures ================================================

function attachGestures(row, m) {
  let startX = 0, startY = 0, dx = 0, dragging = false, longPress = null, longPressed = false;

  const pdown = e => {
    longPressed = false;
    if (e.button === 2) return;
    const p = e.touches ? e.touches[0] : e;
    startX = p.clientX; startY = p.clientY; dx = 0; dragging = false;
    longPress = setTimeout(() => {
      longPressed = true;
      if (navigator.vibrate) try { navigator.vibrate(20); } catch {}
      showMessageContextMenu(p, m);
    }, 500);
  };
  const pmove = e => {
    const p = e.touches ? e.touches[0] : e;
    const ddx = p.clientX - startX;
    const ddy = p.clientY - startY;
    if (Math.abs(ddx) > 8 || Math.abs(ddy) > 8) {
      if (longPress) { clearTimeout(longPress); longPress = null; }
    }
    if (!dragging && Math.abs(ddx) > 16 && Math.abs(ddx) > Math.abs(ddy)) dragging = true;
    if (dragging) {
      const me = state.me?.id;
      const out = m.sender_id === me;
      // Swipe direction: in messages, swipe-left for own, swipe-right for incoming
      const allowDir = out ? -1 : 1;
      dx = Math.max(-90, Math.min(90, ddx));
      if (Math.sign(dx) !== allowDir && Math.abs(dx) > 4) dx = 0;
      row.style.transform = `translateX(${dx}px)`;
      const ind = row.querySelector('.swipe-reply-indicator');
      if (ind) {
        const k = Math.min(1, Math.abs(dx) / 60);
        ind.style.transform = `translateY(-50%) scale(${k})`;
        ind.style.opacity = k;
      }
    }
  };
  const pup = () => {
    if (longPress) { clearTimeout(longPress); longPress = null; }
    if (dragging && Math.abs(dx) > 60) startReply(m);
    row.style.transform = '';
    const ind = row.querySelector('.swipe-reply-indicator');
    if (ind) { ind.style.transform = ''; ind.style.opacity = ''; }
    dragging = false;
  };
  row.addEventListener('pointerdown', pdown);
  row.addEventListener('pointermove', pmove);
  row.addEventListener('pointerup',   pup);
  row.addEventListener('pointercancel', pup);
  row.addEventListener('contextmenu', e => showMessageContextMenu(e, m));
  row.addEventListener('dblclick', e => {
    e.preventDefault();
    setReaction(m, '❤️');
  });
}

// Edge-swipe-back (mobile)
let edgeStartX = 0, edgeDragging = false;
document.addEventListener('touchstart', e => {
  if (window.innerWidth > 768) return;
  if (!state.current && !state.page) return;
  const t = e.touches[0];
  if (t.clientX < 16) {
    edgeStartX = t.clientX;
    edgeDragging = true;
  }
}, { passive: true });
document.addEventListener('touchmove', e => {
  if (!edgeDragging) return;
  const t = e.touches[0];
  const dx = t.clientX - edgeStartX;
  if (dx > 60) {
    edgeDragging = false;
    if (state.page) closePage();
    else closeChat();
  }
}, { passive: true });
document.addEventListener('touchend', () => { edgeDragging = false; }, { passive: true });

// Prevent native context menu globally except on selectable surfaces
document.addEventListener('contextmenu', e => {
  let t = e.target;
  while (t && t !== document.body) {
    const tag = t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) return;
    if (t.classList && (t.classList.contains('bubble-text') || t.classList.contains('bubble-caption') || t.classList.contains('selectable'))) return;
    t = t.parentNode;
  }
  e.preventDefault();
});

// Suppress double-tap zoom on iOS Safari
let _lastTouch = 0;
document.addEventListener('touchend', e => {
  const now = Date.now();
  if (now - _lastTouch < 320) {
    let t = e.target;
    while (t && t !== document.body) {
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable) return;
      t = t.parentNode;
    }
    e.preventDefault();
  }
  _lastTouch = now;
}, { passive: false });

// Suppress pinch zoom via gesture events
document.addEventListener('gesturestart', e => e.preventDefault());
document.addEventListener('gesturechange', e => e.preventDefault());
document.addEventListener('gestureend', e => e.preventDefault());

// ============= 10. Drawers (chat info / user profile) =====================

function openChatDrawer(chat) {
  state.drawerKind = 'chat';
  const d = $('#drawer');
  d.innerHTML = '';
  d.appendChild(buildDrawerHeader('Информация о чате'));
  const body = ce('div', { class: 'drawer-body' });
  const cover = ce('div', { class: 'profile-cover ' + avColor(chat.id) });
  const ident = ce('div', { class: 'ident' });
  const nameRow = ce('div', { class: 'name' }, chat.title || String(chat.id), ...chatTags(chat));
  ident.appendChild(nameRow);
  ident.appendChild(ce('div', { class: 'status' }, chatStatus(chat)));
  cover.appendChild(ce('div', { class: 'gradient' }));
  cover.appendChild(ident);
  body.appendChild(cover);
  if (chat.username) body.appendChild(profileRow(SVG.link, '@' + chat.username, 'Юзернейм'));
  body.appendChild(profileRow(SVG.copy, String(chat.id), 'ID чата'));
  body.appendChild(profileDivider());
  body.appendChild(ce('div', { class: 'profile-section-title' }, 'Действия'));
  body.appendChild(profileRow(SVG.shield, 'Авто-модерация', '', () => openModerationFor(chat.id)));
  if (chat.type !== 'private') {
    body.appendChild(profileRow(SVG.user, 'Администраторы', '', () => openAdminsList(chat.id)));
  }
  body.appendChild(profileRow(SVG.photo, 'Сменить аватар', 'Канал', () => uploadChannelPhoto(chat.id)));
  body.appendChild(profileRow(SVG.ban, 'Бот выходит из чата', '', () => leaveChat(chat.id)));
  d.appendChild(body);
  d.classList.add('open');
}

async function openUserDrawer(userId) {
  state.drawerKind = 'user';
  const d = $('#drawer');
  d.innerHTML = '';
  d.appendChild(buildDrawerHeader('Профиль пользователя'));
  const body = ce('div', { class: 'drawer-body' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  d.appendChild(body);
  d.classList.add('open');

  try {
    const u = await api(`/api/users/${userId}/profile`);
    state.users.set(userId, u);
    body.innerHTML = '';
    const cover = ce('div', { class: 'profile-cover ' + avColor(u.id) });
    // Photos carousel
    if (u.photos && u.photos.length) {
      cover.appendChild(ce('img', { class: 'full', src: '/file/' + u.photos[0].file_id + '?token=' + encodeURIComponent(TOKEN) }));
      if (u.photos.length > 1) {
        const segs = ce('div', { class: 'profile-photos-dots' });
        for (let i = 0; i < u.photos.length; i++) segs.appendChild(ce('div', { class: 'seg' + (i === 0 ? ' active' : '') }));
        cover.appendChild(segs);
        // Tap left/right
        cover.addEventListener('click', ev => {
          const rect = cover.getBoundingClientRect();
          const x = (ev.clientX - rect.left) / rect.width;
          const dir = x < 0.5 ? -1 : 1;
          const segArr = $$('.profile-photos-dots .seg', cover);
          let idx = segArr.findIndex(s => s.classList.contains('active'));
          idx = (idx + dir + u.photos.length) % u.photos.length;
          segArr.forEach((s, i) => s.classList.toggle('active', i === idx));
          cover.querySelector('img.full').src = '/file/' + u.photos[idx].file_id + '?token=' + encodeURIComponent(TOKEN);
        });
      }
    }
    cover.appendChild(ce('div', { class: 'gradient' }));
    const ident = ce('div', { class: 'ident' });
    const nameRow = ce('div', { class: 'name' }, (u.first_name || '') + ' ' + (u.last_name || ''));
    if (u.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
    if (u.is_bot) nameRow.appendChild(ce('span', { class: 'tag-bot' }, 'BOT'));
    ident.appendChild(nameRow);
    ident.appendChild(ce('div', { class: 'status' }, u.username ? '@' + u.username : 'ID ' + u.id));
    cover.appendChild(ident);
    body.appendChild(cover);
    if (u.username) body.appendChild(profileRow(SVG.link, '@' + u.username, 'Юзернейм'));
    body.appendChild(profileRow(SVG.copy, String(u.id), 'ID пользователя'));
    if (u.is_premium) body.appendChild(profileRow('<svg viewBox="0 0 24 24"><path fill="var(--tg-premium)" d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 22 12 18.27 5.82 22 7 14.14l-5-4.87 6.91-1.01z"/></svg>', 'Telegram Premium', 'Премиум-аккаунт'));
    body.appendChild(profileDivider());
    body.appendChild(ce('div', { class: 'profile-section-title' }, 'Действия'));
    body.appendChild(profileRow(SVG.send, 'Написать в личку', '', () => {
      const exists = state.chats.get(userId);
      if (exists) { closeDrawer(); openChat(userId); }
      else toast('Бот не может сам инициировать диалог. Попросите пользователя написать боту первым.');
    }));
  } catch (e) {
    body.innerHTML = '<div style="padding:24px;color:var(--tg-text-3)">Ошибка: ' + esc(e.message) + '</div>';
  }
}

function buildDrawerHeader(title) {
  const h = ce('header', { class: 'drawer-header' });
  h.appendChild(ce('button', { class: 'icon-btn', html: SVG.back, onclick: closeDrawer }));
  h.appendChild(ce('div', { class: 'title' }, title));
  return h;
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
}

function profileRow(iconHtml, primary, secondary, onclick) {
  const row = ce('div', { class: 'profile-row' });
  row.appendChild(ce('div', { html: iconHtml }));
  const txt = ce('div');
  txt.appendChild(ce('div', { class: 'pri selectable' }, primary));
  if (secondary) txt.appendChild(ce('div', { class: 'sec' }, secondary));
  row.appendChild(txt);
  if (onclick) row.onclick = onclick;
  return row;
}

function profileDivider() { return ce('div', { class: 'profile-divider' }); }

// ============= Composer (send / edit / reply) =============================

function renderComposer() {
  const wrap = ce('div', { class: 'composer-wrap' });
  wrap.appendChild(ce('div', { id: 'composerBars' }));
  const comp = ce('div', { class: 'composer' });

  const attachBtn = ce('button', { class: 'icon-btn', html: SVG.attach, onclick: () => openAttachPopover() });
  const inp = ce('div', {
    class: 'composer-input',
    id: 'composerInp',
    contenteditable: 'true',
    'data-placeholder': 'Сообщение',
    onkeydown: e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendComposer();
      }
    },
  });
  // Send chat_action typing as the user types
  let typingT = 0;
  inp.oninput = () => {
    const now = Date.now();
    if (now - typingT > 4500) {
      typingT = now;
      api('/api/bot/settings', { method: 'POST', body: { op: 'send_chat_action', chat_id: state.current, action: 'typing' } }).catch(() => {});
    }
  };
  const sendBtn = ce('button', { class: 'composer-send', html: SVG.send, onclick: sendComposer });
  comp.appendChild(attachBtn);
  comp.appendChild(inp);
  comp.appendChild(sendBtn);
  wrap.appendChild(comp);
  return wrap;
}

function renderComposerBars() {
  const bars = $('#composerBars');
  if (!bars) return;
  bars.innerHTML = '';
  if (state.reply) {
    const r = state.reply;
    const sender = state.users.get(r.sender_id) || {};
    const bar = ce('div', { class: 'composer-reply-bar' });
    bar.appendChild(ce('div', { class: 'stripe' }));
    bar.appendChild(ce('div', { class: 'meta' },
      ce('div', { class: 'title' }, sender.first_name || sender.username || 'Сообщение'),
      ce('div', { class: 'text' }, r.text || r.caption || mediaLabel(r) || '...')));
    bar.appendChild(ce('button', { class: 'icon-btn small', html: SVG.close, onclick: () => { state.reply = null; renderComposerBars(); } }));
    bars.appendChild(bar);
  }
  if (state.edit) {
    const e = state.edit;
    const bar = ce('div', { class: 'composer-reply-bar' });
    bar.appendChild(ce('div', { class: 'stripe' }));
    bar.appendChild(ce('div', { class: 'meta' },
      ce('div', { class: 'title' }, 'Редактирование'),
      ce('div', { class: 'text' }, e.text || e.caption || '...')));
    bar.appendChild(ce('button', { class: 'icon-btn small', html: SVG.close, onclick: () => { state.edit = null; $('#composerInp').textContent = ''; renderComposerBars(); } }));
    bars.appendChild(bar);
  }
}

async function sendComposer() {
  const inp = $('#composerInp');
  const text = (inp.textContent || '').trim();
  if (!text) return;
  if (state.edit) {
    try {
      await api('/api/edit', { method: 'POST', body: { chat_id: state.edit.chat_id, message_id: state.edit.message_id, text } });
      state.edit = null;
      inp.textContent = '';
      renderComposerBars();
    } catch (err) { toast('Ошибка: ' + err.message); }
    return;
  }
  try {
    await api('/api/send', {
      method: 'POST', body: {
        chat_id: state.current, text,
        reply_to_message_id: state.reply?.message_id || null,
      },
    });
    inp.textContent = '';
    state.reply = null;
    renderComposerBars();
    setTimeout(() => { const s = $('#msgsScroll'); if (s) s.scrollTop = s.scrollHeight; }, 50);
  } catch (e) { toast('Ошибка: ' + e.message); }
}

function openAttachPopover() {
  closeAttachPopover();
  const pop = ce('div', { class: 'attach-pop', id: 'attachPop' });
  const item = (icon, label, fn) => ce('button', { onclick: () => { closeAttachPopover(); fn(); } }, ce('span', { html: icon }), label);
  pop.append(
    item(SVG.photo, 'Фото / Видео', () => pickFiles('image/*,video/*', true)),
    item(SVG.doc, 'Документ', () => pickFiles('*/*', false)),
    item(SVG.poll, 'Опрос', () => openPollComposer()),
    item(SVG.sticker, 'Стикер', () => toast('Из бота нельзя отправить произвольный стикер — нужен file_id. (TODO: загрузка из набора)')),
  );
  document.body.appendChild(pop);
  const closeNow = ev2 => { if (!pop.contains(ev2.target)) closeAttachPopover(); };
  setTimeout(() => document.addEventListener('click', closeNow, { capture: true, once: true }), 0);
}
function closeAttachPopover() { $('#attachPop')?.remove(); }

function pickFiles(accept, asMedia) {
  const f = ce('input', { type: 'file', accept, multiple: 'multiple', style: { display: 'none' } });
  f.onchange = async () => {
    const files = Array.from(f.files || []);
    if (!files.length) return;
    const fd = new FormData();
    fd.append('chat_id', state.current);
    if (state.reply?.message_id) fd.append('reply_to_message_id', state.reply.message_id);
    files.forEach((file, i) => fd.append('file' + i, file, file.name));
    try {
      if (files.length === 1) {
        fd.set('file', files[0], files[0].name);
        await api('/api/send_photo', { method: 'POST', body: fd });
      } else {
        await api('/api/send_media_group', { method: 'POST', body: fd });
      }
      state.reply = null; renderComposerBars();
    } catch (e) { toast('Ошибка отправки: ' + e.message); }
  };
  document.body.appendChild(f);
  f.click();
  setTimeout(() => f.remove(), 1000);
}

function openPollComposer() {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Новый опрос'));
  modal.appendChild(ce('label', {}, 'Вопрос'));
  const q = ce('input', { type: 'text', placeholder: 'Ваш вопрос' });
  modal.appendChild(q);
  modal.appendChild(ce('label', {}, 'Варианты (с новой строки)'));
  const opts = ce('textarea', { placeholder: 'Вариант 1\nВариант 2' });
  modal.appendChild(opts);
  const anon = ce('div', { class: 'modal-row' });
  anon.appendChild(ce('div', { class: 'flex' }, 'Анонимный'));
  const anonSw = ce('div', { class: 'switch on', onclick: () => anonSw.classList.toggle('on') });
  anon.appendChild(anonSw);
  modal.appendChild(anon);
  const multi = ce('div', { class: 'modal-row' });
  multi.appendChild(ce('div', { class: 'flex' }, 'Можно выбрать несколько'));
  const multiSw = ce('div', { class: 'switch', onclick: () => multiSw.classList.toggle('on') });
  multi.appendChild(multiSw);
  modal.appendChild(multi);
  modal.appendChild(ce('div', { class: 'actions' },
    ce('button', { onclick: () => back.remove() }, 'Отмена'),
    ce('button', {
      class: 'primary',
      onclick: async () => {
        const question = q.value.trim();
        const optionList = opts.value.split('\n').map(s => s.trim()).filter(Boolean);
        if (!question || optionList.length < 2) { toast('Минимум 2 варианта'); return; }
        try {
          await api('/api/send_poll', { method: 'POST', body: {
            chat_id: state.current, question, options: optionList,
            is_anonymous: anonSw.classList.contains('on'),
            allows_multiple_answers: multiSw.classList.contains('on'),
          }});
          back.remove();
        } catch (e) { toast('Ошибка: ' + e.message); }
      },
    }, 'Создать'),
  ));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

// ============= 11. Pages ==================================================

function openPage(kind) {
  closePage();
  state.page = kind;
  let page = $('#page-' + kind);
  if (!page) return;
  page.innerHTML = '';
  page.appendChild(buildPageShell(kind));
  page.classList.add('active');
  if (kind === 'profile') loadProfilePage(page);
  else if (kind === 'moderation') loadModerationPage(page);
  else if (kind === 'ai') loadAIPage(page);
  else if (kind === 'mini') loadMiniPage(page);
  else if (kind === 'settings') loadSettingsPage(page);
  else if (kind === 'audit') loadAuditPage(page);
}

function closePage() {
  if (!state.page) return;
  const page = $('#page-' + state.page);
  if (page) page.classList.remove('active');
  state.page = null;
}

function buildPageShell(kind) {
  const titles = { profile: 'Профиль бота', moderation: 'Авто-модерация', ai: 'AI-ассистент', mini: 'Мини-апы', settings: 'Настройки', audit: 'Журнал событий' };
  const h = ce('header', { class: 'page-header' });
  h.appendChild(ce('button', { class: 'icon-btn', html: SVG.back, onclick: closePage }));
  h.appendChild(ce('div', { class: 'title' }, titles[kind] || kind));
  return h;
}

async function loadProfilePage(page) {
  const body = ce('div', { class: 'page-body', id: 'profileBody' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  page.appendChild(body);
  try {
    const me = await api('/api/me');
    state.me = me;
    body.innerHTML = '';
    if (!me.bot_online) {
      body.appendChild(ce('div', { class: 'page-section' },
        ce('div', { style: 'color:var(--tg-text-2)' }, me.error || 'Бот не подключён.'),
        ce('div', { class: 'desc', style: 'margin-top:8px' }, 'Заполните TG_BOT_TOKEN в .env и перезапустите приложение.')));
      return;
    }
    body.appendChild(ce('div', { class: 'page-section' },
      ce('div', { style: 'display:flex; align-items:center; gap:14px; padding:8px 0' },
        renderAvatar(me.id, me.first_name || '?', 's96', 'user'),
        ce('div', {},
          ce('div', { style: 'font-size:18px; font-weight:600' }, me.first_name + ' ' + (me.last_name || ''),
            ce('span', { class: 'tag-bot' }, 'BOT')),
          ce('div', { class: 'desc', style: 'margin-top:2px' }, '@' + me.username + ' · ID ' + me.id),
        ),
      )));

    const profileBlock = ce('div', { class: 'page-section' });
    profileBlock.appendChild(ce('h4', {}, 'Профиль'));
    const nameInp = ce('input', { type: 'text', value: me.first_name || '', placeholder: 'Имя бота' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Имя'), nameInp));
    const descTa = ce('textarea', { placeholder: 'Видно при входе в чат с ботом' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Описание'), descTa));
    const sdescTa = ce('textarea', { placeholder: 'Видно в подсказках' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Краткое описание'), sdescTa));
    const cmdsTa = ce('textarea', { placeholder: 'start - Начать\\nhelp - Справка' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Команды'), cmdsTa));
    profileBlock.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:12px' },
      ce('button', { class: 'btn primary', onclick: async () => {
        const cmds = (cmdsTa.value || '').split('\n').map(l => l.trim()).filter(Boolean).map(l => {
          const i = l.indexOf('-');
          return { command: l.slice(0, i).trim().replace(/^\//, ''), description: l.slice(i + 1).trim() };
        });
        try {
          await api('/api/profile', { method: 'POST', body: {
            name: nameInp.value, description: descTa.value, short_description: sdescTa.value, commands: cmds,
          }});
          toast('Сохранено');
        } catch (e) { toast('Ошибка: ' + e.message); }
      }}, 'Сохранить'),
    ));
    body.appendChild(profileBlock);

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Меню-кнопка (Menu Button)'),
      ce('div', { class: 'desc', style: 'margin-bottom:10px' }, 'Кнопка слева от поля ввода в личке с ботом.'),
      ce('div', { class: 'btn-row' },
        ce('button', { class: 'btn', onclick: () => api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'commands' }}).then(() => toast('Команды')) }, 'Команды'),
        ce('button', { class: 'btn', onclick: () => api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'default' }}).then(() => toast('По умолчанию')) }, 'Скрыть'),
        ce('button', { class: 'btn', onclick: () => {
          const url = prompt('URL веб-приложения (https://...)');
          if (!url) return;
          api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'webapp', url, text: 'Открыть' }}).then(() => toast('WebApp кнопка установлена'));
        }}, 'WebApp...'),
      ),
    ));

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Права администратора по умолчанию (для добавления в группы)'),
      buildAdminRightsForm(false),
    ));
    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Права администратора по умолчанию (для каналов)'),
      buildAdminRightsForm(true),
    ));

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Что нельзя через Bot API'),
      ce('div', { class: 'desc' },
        '• Аватарка бота — меняется только через @BotFather (BotFather → /mybots → выбрать бота → Edit Bot → Edit Botpic).',
        ce('br'),
        '• Запретить пользователю писать боту нельзя — бот может только перестать отвечать.',
        ce('br'),
        '• Чтобы бот видел ВСЕ сообщения в группе, выключите Privacy Mode: BotFather → /mybots → Bot Settings → Group Privacy → Turn off.',
      ),
    ));
  } catch (e) {
    body.innerHTML = '<div style="padding:24px;color:var(--tg-error)">' + esc(e.message) + '</div>';
  }
}

function buildAdminRightsForm(forChannels) {
  const f = ce('div');
  const checks = forChannels ? [
    ['can_manage_chat', 'Управление чатом'],
    ['can_delete_messages', 'Удалять сообщения'],
    ['can_restrict_members', 'Ограничивать участников'],
    ['can_promote_members', 'Назначать админов'],
    ['can_change_info', 'Менять инфо'],
    ['can_invite_users', 'Добавлять'],
    ['can_post_messages', 'Публиковать'],
    ['can_edit_messages', 'Редактировать'],
  ] : [
    ['can_manage_chat', 'Управление чатом'],
    ['can_delete_messages', 'Удалять сообщения'],
    ['can_restrict_members', 'Ограничивать участников'],
    ['can_promote_members', 'Назначать админов'],
    ['can_change_info', 'Менять инфо'],
    ['can_invite_users', 'Добавлять'],
    ['can_pin_messages', 'Закреплять'],
    ['can_manage_topics', 'Темы'],
  ];
  const switches = {};
  for (const [k, label] of checks) {
    const row = ce('div', { class: 'modal-row' });
    row.appendChild(ce('div', { class: 'flex' }, label));
    const sw = ce('div', { class: 'switch', onclick: () => sw.classList.toggle('on') });
    row.appendChild(sw);
    switches[k] = sw;
    f.appendChild(row);
  }
  f.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:12px' },
    ce('button', { class: 'btn primary', onclick: async () => {
      const body = { op: 'set_default_admin_rights', for_channels: forChannels };
      for (const [k, sw] of Object.entries(switches)) body[k] = sw.classList.contains('on');
      try { await api('/api/bot/settings', { method: 'POST', body }); toast('Сохранено'); }
      catch (e) { toast('Ошибка: ' + e.message); }
    }}, 'Применить'),
  ));
  return f;
}

// Moderation
function openModerationFor(chatId) {
  openPage('moderation');
  setTimeout(() => loadModerationPage($('#page-moderation'), chatId), 30);
}

async function loadModerationPage(page, focusChatId) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('div', { class: 'desc' }, 'Включите модерацию для каждого чата отдельно. Бот должен быть админом с правом удалять/ограничивать.'),
  ));
  const chatsArr = Array.from(state.chats.values()).filter(c => c.type !== 'private');
  for (const c of chatsArr) {
    const sect = ce('div', { class: 'page-section' });
    sect.appendChild(ce('h4', {}, c.title));
    sect.appendChild(ce('div', { id: 'mod-' + c.id }, '...'));
    body.appendChild(sect);
    loadModForChat(c.id);
  }
  if (!chatsArr.length) body.appendChild(ce('div', { class: 'page-section' }, ce('div', { class: 'desc' }, 'Бот ещё не в группах/каналах.')));
  page.appendChild(body);
}

async function loadModForChat(chatId) {
  try {
    const conf = await api('/api/moderation/' + chatId);
    const cont = $('#mod-' + chatId);
    if (!cont) return;
    cont.innerHTML = '';
    const rows = [
      ['enabled', 'Включить модерацию', 'switch'],
      ['antiflood', 'Антифлуд', 'switch'],
      ['antilinks', 'Антиссылки', 'switch'],
      ['anticaps', 'Антикапс', 'switch'],
      ['ai_moderation', 'AI модерация', 'switch'],
    ];
    for (const [k, label, kind] of rows) {
      const r = ce('div', { class: 'modal-row' });
      r.appendChild(ce('div', { class: 'flex' }, label));
      const sw = ce('div', { class: 'switch' + (conf[k] ? ' on' : ''), onclick: () => sw.classList.toggle('on') });
      sw.dataset.key = k;
      r.appendChild(sw);
      cont.appendChild(r);
    }
    const banwordsRow = ce('div');
    banwordsRow.appendChild(ce('label', {}, 'Бан-слова (через запятую)'));
    const bw = ce('input', { type: 'text', value: (JSON.parse(conf.banwords_json || '[]')).join(', ') });
    banwordsRow.appendChild(bw);
    cont.appendChild(banwordsRow);
    const actionRow = ce('div');
    actionRow.appendChild(ce('label', {}, 'Действие при нарушении'));
    const act = ce('select');
    for (const o of ['delete', 'warn', 'mute', 'ban']) {
      const opt = ce('option', { value: o }, o);
      if (conf.action === o) opt.selected = true;
      act.appendChild(opt);
    }
    actionRow.appendChild(act);
    cont.appendChild(actionRow);

    cont.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:8px' },
      ce('button', { class: 'btn primary', onclick: async () => {
        const body = { chat_id: chatId };
        $$('.switch[data-key]', cont).forEach(s => body[s.dataset.key] = s.classList.contains('on') ? 1 : 0);
        body.banwords = bw.value.split(',').map(s => s.trim()).filter(Boolean);
        body.action = act.value;
        try { await api('/api/moderation', { method: 'POST', body }); toast('Сохранено'); }
        catch (e) { toast('Ошибка: ' + e.message); }
      }}, 'Сохранить'),
    ));
  } catch (e) {
    $('#mod-' + chatId).textContent = 'Ошибка: ' + e.message;
  }
}

// AI page
async function loadAIPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.style.display = 'flex';
  body.style.flexDirection = 'column';
  const topBar = ce('div', { style: 'display:flex; gap:8px; margin-bottom:10px' });
  const sel = ce('select', { id: 'aiModelSel', style: 'flex:1; padding:8px; background:var(--tg-bg-2); color:white; border:1px solid var(--tg-divider-2); border-radius:8px' });
  body.appendChild(topBar);
  topBar.appendChild(sel);
  const msgs = ce('div', { class: 'ai-msgs', id: 'aiMsgs' });
  body.appendChild(msgs);
  const composer = ce('div', { style: 'display:flex; gap:6px; padding-top:10px; border-top:1px solid var(--tg-divider)' });
  const inp = ce('textarea', { placeholder: 'Сообщение модели...', style: 'flex:1; background:var(--tg-bg-2); color:white; border:1px solid var(--tg-divider-2); border-radius:10px; padding:10px; resize:none; min-height:40px; max-height:140px' });
  const sendBtn = ce('button', { class: 'btn primary', onclick: async () => {
    const text = inp.value.trim(); if (!text) return;
    inp.value = '';
    msgs.appendChild(ce('div', { class: 'ai-msg-user' }, text));
    const loading = ce('div', { class: 'ai-msg-asst' }, ce('div', { class: 'spinner' }));
    msgs.appendChild(loading);
    try {
      const r = await api('/api/ai/chat', { method: 'POST', body: { model: sel.value, messages: [{ role: 'user', content: text }] } });
      loading.remove();
      msgs.appendChild(ce('div', { class: 'ai-msg-asst', html: linkify(esc(r.content || JSON.stringify(r))) }));
    } catch (e) { loading.remove(); msgs.appendChild(ce('div', { class: 'ai-msg-asst', style: 'color:var(--tg-error)' }, 'Ошибка: ' + e.message)); }
  }}, 'Отправить');
  composer.appendChild(inp); composer.appendChild(sendBtn);
  body.appendChild(composer);
  page.appendChild(body);

  try {
    const me = await api('/api/me');
    const models = me.models || [];
    if (!models.length) sel.innerHTML = '<option>(нет OPENROUTER ключей в .env)</option>';
    for (const m of models) sel.appendChild(ce('option', { value: m.id }, m.id));
  } catch {}
}

// Mini apps page (basic CRUD)
async function loadMiniPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Ваши мини-апы'),
    ce('div', { id: 'miniList' }, '...'),
    ce('div', { class: 'btn-row', style: 'margin-top:12px' },
      ce('button', { class: 'btn primary', onclick: () => openMiniEditor() }, '＋ Создать'),
    ),
  ));
  page.appendChild(body);
  refreshMiniList();
}
async function refreshMiniList() {
  const list = $('#miniList');
  if (!list) return;
  try {
    const apps = await api('/api/mini_apps');
    list.innerHTML = '';
    if (!apps.length) { list.appendChild(ce('div', { class: 'desc' }, 'Пока пусто.')); return; }
    for (const a of apps) {
      const row = ce('div', { class: 'page-row' });
      row.appendChild(ce('div', {},
        ce('div', { style: 'font-size:14.5px' }, a.name),
        ce('div', { class: 'desc' }, a.description || ''),
      ));
      const actions = ce('div', { class: 'btn-row' });
      actions.appendChild(ce('button', { class: 'btn', onclick: () => window.open('/mini/' + a.id + '?token=' + encodeURIComponent(TOKEN), '_blank') }, 'Открыть'));
      actions.appendChild(ce('button', { class: 'btn', onclick: () => openMiniEditor(a) }, 'Редактор'));
      actions.appendChild(ce('button', { class: 'btn danger', onclick: async () => {
        if (!confirm('Удалить ' + a.name + '?')) return;
        await api('/api/mini_apps/' + a.id, { method: 'DELETE' });
        refreshMiniList();
      }}, 'Удалить'));
      row.appendChild(actions);
      list.appendChild(row);
    }
  } catch (e) { list.textContent = 'Ошибка: ' + e.message; }
}
function openMiniEditor(app) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, app ? 'Редактировать мини-ап' : 'Новый мини-ап'));
  modal.appendChild(ce('label', {}, 'Имя'));
  const name = ce('input', { type: 'text', value: app?.name || '' }); modal.appendChild(name);
  modal.appendChild(ce('label', {}, 'Описание'));
  const desc = ce('input', { type: 'text', value: app?.description || '' }); modal.appendChild(desc);
  modal.appendChild(ce('label', {}, 'HTML (полная страница)'));
  const html = ce('textarea', { style: 'min-height:240px; font-family:monospace; font-size:12px' });
  html.value = app?.html || '<!doctype html>\n<html>\n<body style="background:#0e1621;color:white;font-family:system-ui;padding:20px">\n<h1>Привет!</h1>\n<p>Это новый мини-ап.</p>\n</body>\n</html>';
  modal.appendChild(html);
  modal.appendChild(ce('div', { class: 'actions' },
    ce('button', { onclick: () => back.remove() }, 'Отмена'),
    ce('button', { class: 'primary', onclick: async () => {
      try {
        await api('/api/mini_apps', { method: 'POST', body: { id: app?.id, name: name.value, description: desc.value, html: html.value }});
        back.remove(); refreshMiniList();
      } catch (e) { toast('Ошибка: ' + e.message); }
    }}, 'Сохранить'),
  ));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

async function loadSettingsPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Веб-приложение'),
    ce('div', { class: 'page-row' },
      ce('div', { class: 'flex' }, ce('label', {}, 'Сменить токен доступа'), ce('div', { class: 'desc' }, 'Выйдет, и придётся войти заново')),
      ce('button', { class: 'btn danger', onclick: () => { localStorage.removeItem(TOKEN_KEY); location.reload(); } }, 'Выйти'),
    ),
  ));
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Версия'),
    ce('div', { class: 'desc' }, 'TG Studio. Все настройки бота — на странице «Профиль бота».'),
  ));
  page.appendChild(body);
}

async function loadAuditPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  page.appendChild(body);
  try {
    const list = await api('/api/audit');
    body.innerHTML = '';
    const sect = ce('div', { class: 'page-section' });
    for (const r of list) {
      const item = ce('div', { class: 'page-row' });
      item.appendChild(ce('div', {},
        ce('div', { style: 'font-size:13.5px' }, r.kind + (r.message ? ': ' + r.message : '')),
        ce('div', { class: 'desc' }, fmtFullTime(r.ts) + ' · chat=' + (r.chat_id || '-') + ' · user=' + (r.user_id || '-')),
      ));
      sect.appendChild(item);
    }
    body.appendChild(sect);
  } catch (e) { body.innerHTML = 'Ошибка: ' + esc(e.message); }
}

// ============= 12. Image viewer (pinch-zoom) ==============================

function openImageViewer(fileId, kind) {
  const back = ce('div', { class: 'viewer' });
  const isVid = kind === 'video';
  let el;
  if (isVid) {
    el = ce('video', {
      class: 'viewer-img',
      src: '/file/' + fileId + '?token=' + encodeURIComponent(TOKEN),
      controls: 'controls', autoplay: 'autoplay', loop: 'loop', playsinline: 'playsinline',
    });
  } else {
    el = ce('img', { class: 'viewer-img', src: '/file/' + fileId + '?token=' + encodeURIComponent(TOKEN) });
  }
  back.appendChild(el);
  const close = ce('div', { class: 'viewer-close', html: SVG.close, onclick: () => back.remove() });
  back.appendChild(close);
  back.onclick = e => { if (e.target === back) back.remove(); };
  // Pinch zoom
  let scale = 1, lastDist = 0, tx = 0, ty = 0;
  back.addEventListener('touchstart', e => {
    if (e.touches.length === 2) {
      lastDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    }
  });
  back.addEventListener('touchmove', e => {
    if (e.touches.length === 2 && lastDist) {
      e.preventDefault();
      const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      scale = Math.max(1, Math.min(5, scale * (d / lastDist)));
      lastDist = d;
      el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
    }
  }, { passive: false });
  back.addEventListener('touchend', () => { lastDist = 0; });
  // Mouse wheel
  back.addEventListener('wheel', e => {
    e.preventDefault();
    scale = Math.max(1, Math.min(5, scale + (e.deltaY < 0 ? 0.15 : -0.15)));
    el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
  document.body.appendChild(back);
}

// ============= Poll voters modal ==========================================

let _voterModal = null;
function openPollVoters(pollId) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Голосовавшие'));
  const list = ce('div', { class: 'user-list', id: 'voterList' }, ce('div', { class: 'spinner', style: 'margin:24px auto' }));
  modal.appendChild(list);
  modal.appendChild(ce('div', { class: 'actions' }, ce('button', { class: 'primary', onclick: () => { back.remove(); state._pollVoterModal = null; } }, 'Закрыть')));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) { back.remove(); state._pollVoterModal = null; } };
  document.body.appendChild(back);
  state._pollVoterModal = { poll_id: pollId, modal: back };
  loadPollVoters(pollId);
}
async function loadPollVoters(pollId) {
  try {
    const voters = await api('/api/poll/' + pollId + '/voters');
    const list = $('#voterList'); if (!list) return;
    list.innerHTML = '';
    if (!voters.length) { list.appendChild(ce('div', { class: 'desc', style: 'padding:14px' }, 'Пока никто не голосовал, либо опрос анонимный и API не отдаёт имена.')); return; }
    for (const v of voters) {
      const it = ce('div', { class: 'item' });
      it.appendChild(renderAvatar(v.user_id, v.first_name || v.username || '?', 's40', 'user'));
      const meta = ce('div', { class: 'flex' });
      meta.appendChild(ce('div', { style: 'font-size:14.5px; display:flex; align-items:center; gap:4px' },
        (v.first_name || '') + ' ' + (v.last_name || ''),
        v.is_premium ? ce('span', { class: 'badge-star' }, '★') : null,
        v.is_bot ? ce('span', { class: 'tag-bot' }, 'BOT') : null,
      ));
      meta.appendChild(ce('div', { class: 'desc' }, (v.username ? '@' + v.username + ' · ' : '') + 'вариант' + (v.option_ids.length > 1 ? 'ы' : '') + ' ' + v.option_ids.map(x => x + 1).join(', ')));
      it.appendChild(meta);
      list.appendChild(it);
    }
  } catch (e) { $('#voterList').innerHTML = 'Ошибка: ' + esc(e.message); }
}

// ============= Channel photo upload =======================================

async function uploadChannelPhoto(chatId) {
  const f = ce('input', { type: 'file', accept: 'image/*', style: { display: 'none' } });
  f.onchange = async () => {
    const file = f.files?.[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        await api('/api/bot/settings', { method: 'POST', body: { op: 'set_channel_photo', chat_id: chatId, image_base64: reader.result } });
        toast('Аватарка обновлена');
      } catch (e) { toast('Ошибка: ' + e.message); }
    };
    reader.readAsDataURL(file);
  };
  document.body.appendChild(f);
  f.click();
  setTimeout(() => f.remove(), 1000);
}

async function openAdminsList(chatId) {
  try {
    const admins = await api('/api/chats/' + chatId + '/admins');
    const back = ce('div', { class: 'modal-back' });
    const modal = ce('div', { class: 'modal' });
    modal.appendChild(ce('h3', {}, 'Администраторы'));
    const list = ce('div', { class: 'user-list' });
    for (const a of admins) {
      const it = ce('div', { class: 'item', onclick: () => { back.remove(); openUserDrawer(a.user_id); } });
      it.appendChild(renderAvatar(a.user_id, a.first_name || a.username || '?', 's40', 'user'));
      const meta = ce('div', {});
      meta.appendChild(ce('div', { style: 'font-size:14.5px; display:flex; align-items:center; gap:4px' },
        (a.first_name || '') + ' ' + (a.last_name || ''),
        a.is_premium ? ce('span', { class: 'badge-star' }, '★') : null,
        a.is_bot ? ce('span', { class: 'tag-bot' }, 'BOT') : null,
        a.custom_title ? ce('span', { class: 'tag-verified' }, a.custom_title) : null,
      ));
      meta.appendChild(ce('div', { class: 'desc' }, a.status + (a.username ? ' · @' + a.username : '')));
      it.appendChild(meta);
      list.appendChild(it);
    }
    modal.appendChild(list);
    modal.appendChild(ce('div', { class: 'actions' }, ce('button', { class: 'primary', onclick: () => back.remove() }, 'OK')));
    back.appendChild(modal);
    back.onclick = e => { if (e.target === back) back.remove(); };
    document.body.appendChild(back);
  } catch (e) { toast('Ошибка: ' + e.message); }
}

// ============= 13. Service worker =========================================

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// ============= Boot =======================================================

async function boot() {
  if (!TOKEN) await showGate();
  else {
    try { await api('/api/me'); $('#gate').hidden = true; $('#app').hidden = false; }
    catch { localStorage.removeItem(TOKEN_KEY); TOKEN = ''; await showGate(); }
  }
  document.title = 'TG Studio';
  $('#gate').hidden = true;
  $('#app').hidden = false;
  setupSidebar();
  try { state.me = await api('/api/me'); } catch {}
  await loadChats();
  connectWS();
}

document.addEventListener('DOMContentLoaded', boot);
</script>
</body>
</html>
"""
SERVICE_WORKER_JS = r"""/* TG Studio service worker — minimal offline shell */
const CACHE = "tgstudio-v1";
const SHELL = ["/", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Don't cache API/WS/file traffic
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws") || url.pathname.startsWith("/file/")) return;
  e.respondWith(
    caches.match(e.request).then(hit => {
      const net = fetch(e.request).then(res => {
        if (res && res.status === 200 && res.type === "basic") {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone)).catch(() => {});
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
"""
MINI_APP_WRAPPER = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=yes">
<title>{{TITLE}}</title>
<style>
html,body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
</style>
</head>
<body>
<script>
/* Sandbox helpers exposed to mini apps */
window.TGStudio = {
  appId: "{{ID}}",
  storage: {
    get: k => JSON.parse(localStorage.getItem("ma:{{ID}}:" + k) || "null"),
    set: (k, v) => localStorage.setItem("ma:{{ID}}:" + k, JSON.stringify(v)),
    del: k => localStorage.removeItem("ma:{{ID}}:" + k),
    keys: () => Object.keys(localStorage).filter(x => x.startsWith("ma:{{ID}}:")).map(x => x.slice(("ma:{{ID}}:").length))
  },
  close: () => parent.postMessage({ type: "tg-mini-close", id: "{{ID}}" }, "*"),
  notify: msg => parent.postMessage({ type: "tg-mini-notify", id: "{{ID}}", message: msg }, "*"),
};
</script>
{{BODY}}
</body>
</html>
"""


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
