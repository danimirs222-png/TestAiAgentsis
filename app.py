#!/usr/bin/env python3
"""TG Studio - single-file Telegram bot manager + messenger UI.

Backend: python-telegram-bot 20.7 + aiohttp + SQLite.
Frontend: embedded HTML/CSS/JS served from this same file.

See README.md for usage. Never commit your .env.
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


def load_env() -> None:
    """Minimal .env loader (so we don't need python-dotenv)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_env()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_OWNER_ID = int(env("TG_OWNER_ID", "0") or 0)
TG_CHANNEL_ID = int(env("TG_CHANNEL_ID", "0") or 0)
TG_OWNER_USERNAME = env("TG_OWNER_USERNAME")
WEB_HOST = env("WEB_HOST", "0.0.0.0")
WEB_PORT = int(env("WEB_PORT", "8080"))
WEB_ACCESS_TOKEN = env("WEB_ACCESS_TOKEN", "changeme-please")
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
    sticker_emoji TEXT,
    sticker_set TEXT,
    poll_json TEXT,
    reply_to_message_id INTEGER,
    forward_from_id INTEGER,
    reply_markup_json TEXT,
    reactions_json TEXT,
    deleted INTEGER DEFAULT 0,
    via_bot_id INTEGER,
    raw_json TEXT,
    PRIMARY KEY(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(chat_id, date);

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
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = asyncio.Lock()

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


def detect_media(m: Any) -> tuple[str, str, str]:
    """Return (media_type, file_id, file_unique_id) for a Message."""
    if not m:
        return ("", "", "")
    if m.photo:
        ph = m.photo[-1]
        return ("photo", ph.file_id, ph.file_unique_id)
    if m.video:
        return ("video", m.video.file_id, m.video.file_unique_id)
    if m.animation:
        return ("animation", m.animation.file_id, m.animation.file_unique_id)
    if m.voice:
        return ("voice", m.voice.file_id, m.voice.file_unique_id)
    if m.audio:
        return ("audio", m.audio.file_id, m.audio.file_unique_id)
    if m.video_note:
        return ("video_note", m.video_note.file_id, m.video_note.file_unique_id)
    if m.sticker:
        return ("sticker", m.sticker.file_id, m.sticker.file_unique_id)
    if m.document:
        return ("document", m.document.file_id, m.document.file_unique_id)
    return ("", "", "")


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
    media_type, file_id, file_unique_id = detect_media(msg)
    sender = msg.from_user
    sender_chat = msg.sender_chat
    text = msg.text or ""
    caption = msg.caption or ""
    entities = serialize_entities(msg.entities or msg.caption_entities)
    reply_markup = serialize_reply_markup(msg.reply_markup)
    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    poll = serialize_poll(msg.poll) if msg.poll else None
    sticker_emoji = ""
    sticker_set = ""
    if msg.sticker:
        sticker_emoji = msg.sticker.emoji or ""
        sticker_set = msg.sticker.set_name or ""

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
        "entities": entities,
        "reply_markup": reply_markup,
        "poll": poll,
        "reply_to": reply_to,
        "sticker_emoji": sticker_emoji,
        "sticker_set": sticker_set,
    }

    db.exec(
        "INSERT OR REPLACE INTO messages(chat_id, message_id, sender_id, sender_chat_id, date, edit_date,"
        " text, caption, entities_json, media_type, file_id, file_unique_id, sticker_emoji, sticker_set,"
        " poll_json, reply_to_message_id, forward_from_id, reply_markup_json, reactions_json, deleted, via_bot_id, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT reactions_json FROM messages WHERE chat_id=? AND message_id=?), '[]'),?,?,?)",
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
            sticker_emoji,
            sticker_set,
            json.dumps(poll, ensure_ascii=False) if poll else None,
            reply_to,
            (msg.forward_from.id if getattr(msg, "forward_from", None) else None),
            json.dumps(reply_markup, ensure_ascii=False) if reply_markup else None,
            msg.chat.id,
            msg.message_id,
            deleted,
            (msg.via_bot.id if getattr(msg, "via_bot", None) else None),
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
        "sticker_emoji": sticker_emoji,
        "sticker_set": sticker_set,
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
    existing = [e for e in existing if e.get("user_id") != user_id]
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


@web.middleware
async def auth_mw(request: web.Request, handler: Any) -> web.StreamResponse:
    path = request.path
    # Allow static assets, root index, ws upgrade (handled inside)
    if path.startswith(("/api/auth", "/ws", "/file/", "/static/", "/favicon", "/manifest", "/sw.js")) or path == "/" or path == "/index.html":
        return await handler(request)
    token = request.cookies.get("token") or request.headers.get("X-Token") or request.query.get("token", "")
    if token != WEB_ACCESS_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def get_bot(request: web.Request) -> Bot:
    return request.app["bot"]


async def api_auth(request: web.Request) -> web.Response:
    data = await request.json()
    if data.get("token") != WEB_ACCESS_TOKEN:
        return web.json_response({"ok": False, "error": "Bad token"}, status=401)
    resp = web.json_response({"ok": True})
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


async def api_audit(request: web.Request) -> web.Response:
    rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")
    return web.json_response([row_to_dict(r) for r in rows])


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    token = request.query.get("token", "")
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
    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
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
    web_app.router.add_get("/file/{file_id}", file_handler)
    web_app.router.add_get("/api/audit", api_audit)
    web_app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    log.info("Web UI on http://%s:%s/?token=%s", WEB_HOST, WEB_PORT, WEB_ACCESS_TOKEN)

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

INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="theme-color" content="#17212b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest">
<title>TG Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#17212b; --bg2:#232e3c; --bg3:#2b5278; --bg4:#182533;
  --panel:#212d3b; --panel2:#1c2733; --line:#0e1621;
  --text:#e3e8eb; --muted:#708499; --accent:#64baf0; --accent2:#229ed9;
  --out:#2b5278; --inc:#182533; --danger:#e53935; --ok:#4caf50;
  --sticker-bg:transparent;
  --safe-bottom:env(safe-area-inset-bottom,0px);
  --safe-top:env(safe-area-inset-top,0px);
}
html,body{margin:0;padding:0;height:100%;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,"Noto Color Emoji","Apple Color Emoji",sans-serif;
  font-size:14px;overflow:hidden;overscroll-behavior:none;
  -webkit-user-select:none;user-select:none;
}
button,input,textarea,select{font:inherit;color:inherit}
button{background:none;border:0;cursor:pointer;color:inherit;padding:6px 10px;border-radius:8px}
button:hover{background:rgba(255,255,255,.06)}
button.primary{background:var(--accent2);color:#fff;padding:8px 14px;border-radius:10px;font-weight:600}
button.primary:hover{background:#1a8ec1}
button.danger{color:#ff7373}
input[type=text],input[type=password],input[type=number],textarea,select{
  background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--text);width:100%;
  -webkit-user-select:text;user-select:text;
}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#3a4a5e;border-radius:8px}
::-webkit-scrollbar-track{background:transparent}

/* Auth screen */
#auth{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:14px;
  background:var(--bg);z-index:100}
#auth .card{background:var(--panel);padding:24px;border-radius:14px;width:min(360px,90vw);display:flex;flex-direction:column;gap:12px}
#auth h1{margin:0;font-size:20px}
#auth .hint{color:var(--muted);font-size:12px}

/* App layout */
#app{display:none;height:100%;width:100%;flex-direction:column}
#app.ready{display:flex}

.topbar{height:48px;background:var(--bg2);display:flex;align-items:center;padding:0 10px;gap:8px;border-bottom:1px solid var(--line);
  padding-top:var(--safe-top);height:calc(48px + var(--safe-top))}
.topbar .title{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topbar .tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
.topbar .tabs::-webkit-scrollbar{display:none}
.topbar .tab{padding:6px 10px;border-radius:8px;color:var(--muted);font-size:13px;cursor:pointer;white-space:nowrap}
.topbar .tab.active{background:var(--bg3);color:#fff}
.topbar .tab:hover:not(.active){background:rgba(255,255,255,.04);color:var(--text)}

.body{flex:1;display:flex;min-height:0;position:relative}

/* Sidebar (chat list) */
.sidebar{width:340px;background:var(--bg2);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.sidebar .search{padding:8px}
.sidebar .search input{background:var(--panel2)}
.sidebar .list{flex:1;overflow-y:auto;overscroll-behavior:contain}
.chat-row{display:flex;gap:10px;padding:10px;border-radius:10px;margin:2px 6px;cursor:pointer;align-items:center;position:relative}
.chat-row:hover{background:rgba(255,255,255,.04)}
.chat-row.active{background:var(--bg3)}
.chat-row .avatar{width:46px;height:46px;border-radius:50%;flex-shrink:0;background:linear-gradient(135deg,#6dadf0,#229ed9);
  display:flex;align-items:center;justify-content:center;font-weight:600;color:#fff;font-size:18px;overflow:hidden}
.chat-row .avatar img{width:100%;height:100%;object-fit:cover}
.chat-row .meta{flex:1;min-width:0}
.chat-row .meta .name{display:flex;justify-content:space-between;gap:8px;align-items:center}
.chat-row .meta .name b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.chat-row .meta .name .time{font-size:11px;color:var(--muted)}
.chat-row .meta .preview{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.chat-row .badge{background:var(--accent2);color:#fff;border-radius:10px;padding:1px 6px;font-size:11px;min-width:18px;text-align:center}
.chat-row.private .avatar{background:linear-gradient(135deg,#7dcfff,#229ed9)}
.chat-row.group .avatar{background:linear-gradient(135deg,#ffb74d,#fb8c00)}
.chat-row.channel .avatar{background:linear-gradient(135deg,#ce93d8,#8e24aa)}

/* Main chat area */
.main{flex:1;display:flex;flex-direction:column;min-width:0;background:
  radial-gradient(at 30% 20%, rgba(255,255,255,.02), transparent 50%), var(--bg);position:relative}
.chat-head{height:54px;background:var(--bg2);border-bottom:1px solid var(--line);padding:0 14px;display:flex;align-items:center;gap:10px}
.chat-head .avatar{width:38px;height:38px;border-radius:50%;background:#444;overflow:hidden;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600}
.chat-head .avatar img{width:100%;height:100%;object-fit:cover}
.chat-head .info{flex:1;min-width:0}
.chat-head .info .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-head .info .s{color:var(--muted);font-size:12px}
.chat-head .actions{display:flex;gap:4px}

.messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:6px;overscroll-behavior:contain}
.day{align-self:center;background:rgba(0,0,0,.3);padding:4px 10px;border-radius:14px;font-size:12px;color:#e3e8eb;margin:8px 0}

.msg-row{display:flex;gap:8px;align-items:flex-end;position:relative;touch-action:pan-y;transition:transform .15s ease}
.msg-row.out{justify-content:flex-end}
.msg-row .av{width:32px;height:32px;border-radius:50%;background:#666;flex-shrink:0;overflow:hidden;align-self:flex-end}
.msg-row .av img{width:100%;height:100%;object-fit:cover}
.msg-row.out .av{display:none}
.msg-row.grp .av{visibility:hidden}
.bubble{max-width:min(560px,72%);background:var(--inc);padding:8px 12px 6px;border-radius:14px 14px 14px 4px;position:relative;
  -webkit-user-select:text;user-select:text;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.msg-row.out .bubble{background:var(--out);border-radius:14px 14px 4px 14px}
.bubble .sender{font-size:12px;color:var(--accent);font-weight:600;margin-bottom:2px}
.bubble .reply{border-left:3px solid var(--accent);padding:4px 8px;background:rgba(0,0,0,.2);border-radius:8px;margin-bottom:4px;font-size:12px;color:var(--muted);cursor:pointer}
.bubble .reply .reply-name{color:var(--accent);font-weight:600}
.bubble .ts{font-size:10px;color:rgba(255,255,255,.55);float:right;margin-left:6px;margin-top:4px;user-select:none}
.bubble .edited{font-size:10px;color:rgba(255,255,255,.45);margin-right:4px}
.bubble img.attach,.bubble video.attach{max-width:100%;border-radius:8px;display:block;margin:-4px -8px 4px;cursor:zoom-in}
.bubble .sticker{width:160px;height:160px;background:var(--sticker-bg)}
.bubble.sticker-only{background:transparent !important;padding:0;border-radius:0;box-shadow:none}
.bubble.sticker-only .sticker{width:200px;height:200px}
.bubble .caption{margin-top:4px}
.bubble .doc{display:flex;gap:10px;align-items:center;background:rgba(0,0,0,.25);padding:8px;border-radius:10px;margin:2px 0}
.bubble .doc .ic{width:36px;height:36px;border-radius:50%;background:var(--accent2);display:flex;align-items:center;justify-content:center;font-weight:600;color:#fff}
.bubble .poll{background:rgba(0,0,0,.25);padding:8px 10px;border-radius:10px;min-width:240px}
.bubble .poll h4{margin:0 0 8px;font-size:14px}
.bubble .poll .opt{padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);display:flex;justify-content:space-between;gap:8px}
.bubble .ikb{display:flex;flex-direction:column;gap:4px;margin-top:6px;margin-left:-12px;margin-right:-12px;margin-bottom:-6px}
.bubble .ikb .row{display:flex;gap:4px}
.bubble .ikb button{flex:1;background:rgba(0,0,0,.25);border-radius:6px;padding:6px 10px;color:#fff;font-size:12px;cursor:pointer}
.bubble .ikb button:hover{background:rgba(0,0,0,.4)}
.bubble .reactions{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.reaction{background:rgba(0,0,0,.35);padding:2px 8px;border-radius:12px;font-size:12px;cursor:pointer;display:inline-flex;gap:4px;align-items:center}
.reaction:hover{background:rgba(0,0,0,.55)}
.reaction.me{background:var(--accent2)}
.msg-row .swipe-icon{position:absolute;left:-44px;top:50%;transform:translateY(-50%);width:36px;height:36px;border-radius:50%;
  background:var(--bg3);display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .15s}
.msg-row.swiping .swipe-icon{opacity:1}

/* Composer */
.composer{padding:8px;border-top:1px solid var(--line);background:var(--bg2);display:flex;flex-direction:column;gap:6px;
  padding-bottom:calc(8px + var(--safe-bottom))}
.composer .reply-bar{display:flex;align-items:center;gap:8px;background:var(--panel2);padding:6px 10px;border-radius:8px;font-size:12px}
.composer .reply-bar .name{color:var(--accent);font-weight:600}
.composer .row{display:flex;gap:8px;align-items:flex-end}
.composer textarea{resize:none;min-height:38px;max-height:160px;padding:10px;border-radius:18px;background:var(--panel2);border:1px solid var(--line)}
.composer .send{background:var(--accent2);color:#fff;border-radius:50%;width:38px;height:38px;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:18px}
.composer .icon-btn{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.composer .icon-btn:hover{background:rgba(255,255,255,.05)}
.composer .drop-overlay{position:absolute;inset:0;background:rgba(34,158,217,.15);border:2px dashed var(--accent);display:none;align-items:center;justify-content:center;font-size:18px;border-radius:16px;pointer-events:none;z-index:20}
.composer.drag .drop-overlay{display:flex}

/* Context menu */
.ctx-menu{position:fixed;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px;min-width:180px;z-index:200;box-shadow:0 10px 30px rgba(0,0,0,.5)}
.ctx-menu .item{padding:8px 12px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:8px}
.ctx-menu .item:hover{background:rgba(255,255,255,.06)}
.ctx-menu .sep{height:1px;background:var(--line);margin:4px 0}
.ctx-menu .react-row{display:flex;gap:4px;padding:4px;flex-wrap:wrap}
.ctx-menu .react-row .e{font-size:20px;padding:4px 6px;border-radius:6px;cursor:pointer}
.ctx-menu .react-row .e:hover{background:rgba(255,255,255,.08)}

/* Modal */
.modal-back{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:300;display:flex;align-items:center;justify-content:center;padding:16px;animation:fade .15s}
.modal{background:var(--panel);border-radius:14px;max-width:520px;width:100%;max-height:90vh;display:flex;flex-direction:column;overflow:hidden;animation:popin .2s}
.modal header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.modal header h3{margin:0;flex:1}
.modal .content{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
.modal footer{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes popin{from{transform:scale(.95);opacity:.5}to{transform:scale(1);opacity:1}}

/* Settings pages */
.page{padding:20px;overflow-y:auto;flex:1}
.page h2{margin:0 0 14px;font-size:20px}
.section{background:var(--panel);border-radius:12px;padding:16px;margin-bottom:14px;display:flex;flex-direction:column;gap:10px}
.section h3{margin:0 0 6px;font-size:15px;color:var(--accent)}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:12px;color:var(--muted)}
.row-h{display:flex;gap:8px;align-items:center}
.row-h.between{justify-content:space-between}
.switch{position:relative;display:inline-block;width:42px;height:24px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.switch .slider{position:absolute;cursor:pointer;inset:0;background:#555;border-radius:24px;transition:.2s}
.switch .slider::before{content:"";position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked + .slider{background:var(--accent2)}
.switch input:checked + .slider::before{transform:translateX(18px)}
.chip{display:inline-flex;align-items:center;gap:4px;background:var(--bg3);color:#fff;padding:3px 10px;border-radius:12px;font-size:12px}
.chip .x{cursor:pointer;opacity:.7}
.chip .x:hover{opacity:1}

.list-row{display:flex;justify-content:space-between;align-items:center;padding:10px;border-radius:8px}
.list-row:hover{background:rgba(255,255,255,.03)}

/* AI assistant */
.ai-pane{display:flex;flex:1;min-height:0}
.ai-side{width:260px;background:var(--bg2);border-right:1px solid var(--line);overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:6px}
.ai-side .ai-chat{padding:8px;border-radius:8px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between}
.ai-side .ai-chat:hover{background:rgba(255,255,255,.05)}
.ai-side .ai-chat.active{background:var(--bg3)}
.ai-main{flex:1;display:flex;flex-direction:column;min-width:0}
.ai-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.ai-msg{padding:10px 14px;border-radius:14px;max-width:80%;white-space:pre-wrap;line-height:1.45;
  -webkit-user-select:text;user-select:text}
.ai-msg.user{background:var(--out);align-self:flex-end}
.ai-msg.assistant{background:var(--panel);align-self:flex-start}
.ai-msg img{max-width:100%;border-radius:8px}
.ai-compose{padding:8px;border-top:1px solid var(--line);background:var(--bg2);display:flex;gap:8px;align-items:flex-end}
.ai-compose select{max-width:200px;flex-shrink:0}
.ai-compose textarea{flex:1;resize:none;min-height:40px;max-height:160px}

/* Mini apps */
.apps-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;padding:20px;overflow-y:auto}
.app-tile{background:var(--panel);border-radius:14px;padding:14px;display:flex;flex-direction:column;align-items:center;gap:8px;cursor:pointer;transition:transform .1s}
.app-tile:hover{transform:translateY(-2px);background:var(--bg3)}
.app-tile .icon{font-size:36px}
.app-tile .name{font-weight:600;text-align:center}
.app-tile .desc{font-size:11px;color:var(--muted);text-align:center}
.app-frame{position:fixed;inset:0;background:var(--bg);z-index:250;display:flex;flex-direction:column}
.app-frame iframe{flex:1;border:0;background:#fff}
.app-frame .app-head{height:48px;background:var(--bg2);display:flex;align-items:center;gap:8px;padding:0 10px;border-bottom:1px solid var(--line)}

/* Image viewer */
.viewer{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:400;display:flex;align-items:center;justify-content:center;touch-action:none}
.viewer img{max-width:95%;max-height:95%;transform-origin:center;transition:transform .05s linear}
.viewer .close{position:absolute;top:14px;right:14px;color:#fff;font-size:28px;cursor:pointer}

/* Mobile responsive */
@media (max-width:768px){
  .sidebar{position:absolute;inset:0;z-index:5;width:100%;border:0;transition:transform .25s}
  .sidebar.hidden{transform:translateX(-100%)}
  .main{position:absolute;inset:0;z-index:4;transition:transform .25s}
  .main.hidden{transform:translateX(100%)}
  .bubble{max-width:86%}
  .ai-side{position:absolute;width:80%;height:100%;z-index:5;transition:transform .25s}
  .ai-side.hidden{transform:translateX(-100%)}
}

/* Selection */
::selection{background:rgba(100,186,240,.4)}

/* Typing indicator */
.typing-dots{display:inline-flex;gap:3px;align-items:center}
.typing-dots span{width:6px;height:6px;background:var(--muted);border-radius:50%;animation:blink 1.4s infinite both}
.typing-dots span:nth-child(2){animation-delay:.2s}
.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.3}40%{opacity:1}}

/* Loading spinner */
.spinner{width:24px;height:24px;border:3px solid rgba(255,255,255,.15);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--panel);padding:10px 16px;border-radius:10px;
  border:1px solid var(--line);z-index:500;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90%}
#toast.show{opacity:1}
#toast.error{background:#5b1f1f}
#toast.ok{background:#1f5b2c}

.empty-state{display:flex;flex:1;align-items:center;justify-content:center;color:var(--muted);font-size:14px;flex-direction:column;gap:8px;padding:24px;text-align:center}
.empty-state .e{font-size:64px}
</style>
</head>
<body>

<div id="auth">
  <div class="card">
    <h1>TG Studio</h1>
    <div class="hint">Введите ваш WEB_ACCESS_TOKEN из .env</div>
    <input id="auth-token" type="password" placeholder="токен доступа" autocomplete="off">
    <button class="primary" id="auth-go">Войти</button>
    <div class="hint">Совет: добавьте <code>?token=ВАШ_ТОКЕН</code> к URL чтобы пропустить этот экран.</div>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="title" id="me-title">TG Studio</div>
    <div class="tabs" id="tabs">
      <div class="tab active" data-tab="chats">💬 Чаты</div>
      <div class="tab" data-tab="profile">🤖 Профиль</div>
      <div class="tab" data-tab="moderation">🛡️ Модерация</div>
      <div class="tab" data-tab="ai">✨ AI</div>
      <div class="tab" data-tab="apps">🧩 Мини-апы</div>
      <div class="tab" data-tab="audit">📜 Лог</div>
      <div class="tab" data-tab="settings">⚙️</div>
    </div>
    <div id="ws-status" title="WebSocket" style="color:var(--muted);font-size:11px">●</div>
  </div>
  <div class="body" id="body"></div>
</div>

<div id="toast"></div>

<script>"use strict";
/* TG Studio frontend — single-file SPA */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  token: "",
  me: null,
  chats: [],
  activeChat: null,
  messages: {}, // chatId -> array
  senders: {}, // chatId -> {userId -> user}
  replyTo: null,
  editTarget: null,
  tab: "chats",
  ws: null,
  wsReady: false,
  models: [],
  aiChats: [],
  aiActive: null,
  miniApps: [],
};

// ---------- helpers ----------
function escapeHTML(s) {
  return (s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function fmtDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const diff = (now - d) / 86400000;
  if (diff < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString();
}
function fmtDay(t) {
  const d = new Date(t * 1000);
  return d.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
}
function avatarColor(id) {
  const palette = ["#e17076", "#7bc862", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"];
  return palette[Math.abs(id || 0) % palette.length];
}
function avatarHTML(name, id, src) {
  const init = (name || "?").trim().split(/\s+/).slice(0, 2).map(x => x[0] || "").join("").toUpperCase();
  const bg = avatarColor(id);
  if (src) return `<div class="avatar" style="background:${bg}"><img src="${escapeHTML(src)}" loading="lazy" onerror="this.remove()" alt=""></div>`;
  return `<div class="avatar" style="background:${bg}">${escapeHTML(init || "?")}</div>`;
}
function toast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show " + (kind || "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), 2400);
}
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "X-Token": state.token, "Content-Type": "application/json" }, opts.headers || {});
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
    opts.body = JSON.stringify(opts.body);
  }
  if (opts.body instanceof FormData) delete opts.headers["Content-Type"];
  const r = await fetch(path, opts);
  if (r.status === 401) {
    showAuth();
    throw new Error("unauthorized");
  }
  if (!r.ok) {
    let err = r.statusText;
    try { err = (await r.json()).error || err; } catch {}
    throw new Error(err);
  }
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("json")) return r.json();
  return r.text();
}

// ---------- auth ----------
function showAuth() {
  $("#auth").style.display = "flex";
  $("#app").classList.remove("ready");
}
function hideAuth() {
  $("#auth").style.display = "none";
  $("#app").classList.add("ready");
}
async function tryAuth(token) {
  try {
    state.token = token;
    const r = await fetch("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) throw new Error("Неверный токен");
    return true;
  } catch (e) {
    toast(e.message, "error");
    return false;
  }
}

// ---------- WebSocket ----------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onopen = () => {
    state.wsReady = true;
    $("#ws-status").style.color = "var(--ok)";
    $("#ws-status").title = "WebSocket подключён";
  };
  ws.onclose = () => {
    state.wsReady = false;
    $("#ws-status").style.color = "var(--danger)";
    $("#ws-status").title = "WebSocket отключён";
    setTimeout(connectWS, 2500);
  };
  ws.onmessage = ev => {
    try {
      const e = JSON.parse(ev.data);
      handleEvent(e);
    } catch (err) {
      console.error(err);
    }
  };
}

function handleEvent(e) {
  if (e.type === "message" || e.type === "message_edit") {
    const m = e.data;
    if (!state.messages[m.chat_id]) state.messages[m.chat_id] = [];
    const arr = state.messages[m.chat_id];
    const idx = arr.findIndex(x => x.message_id === m.message_id);
    if (idx >= 0) arr[idx] = Object.assign(arr[idx], m);
    else arr.push(m);
    if (state.activeChat === m.chat_id) renderMessages();
    refreshChatList();
  } else if (e.type === "delete") {
    const arr = state.messages[e.chat_id];
    if (arr) {
      const x = arr.find(m => m.message_id === e.message_id);
      if (x) x.deleted = 1;
    }
    if (state.activeChat === e.chat_id) renderMessages();
  } else if (e.type === "reaction") {
    const arr = state.messages[e.chat_id];
    if (arr) {
      const x = arr.find(m => m.message_id === e.message_id);
      if (x) x.reactions = e.reactions;
    }
    if (state.activeChat === e.chat_id) renderMessages();
  } else if (e.type === "moderation") {
    toast(`Модерация: удалено сообщение (${e.reason})`, "ok");
  }
}

// ---------- bootstrap ----------
async function bootstrap() {
  try {
    state.me = await api("/api/me");
    state.models = state.me.models || [];
    $("#me-title").textContent = "@" + (state.me.username || "bot") + " · TG Studio";
  } catch (e) {
    showAuth();
    return;
  }
  hideAuth();
  await loadChats();
  connectWS();
  renderTab();
}

async function loadChats() {
  try {
    state.chats = await api("/api/chats");
    refreshChatList();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- tabs ----------
$$("#tabs .tab").forEach(t => {
  t.addEventListener("click", () => {
    $$("#tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    state.tab = t.dataset.tab;
    renderTab();
  });
});

function renderTab() {
  const b = $("#body");
  b.innerHTML = "";
  if (state.tab === "chats") renderChats(b);
  else if (state.tab === "profile") renderProfile(b);
  else if (state.tab === "moderation") renderModeration(b);
  else if (state.tab === "ai") renderAI(b);
  else if (state.tab === "apps") renderApps(b);
  else if (state.tab === "audit") renderAudit(b);
  else if (state.tab === "settings") renderSettings(b);
}

// ============== CHATS TAB ==============
function renderChats(root) {
  root.innerHTML = `
    <div class="sidebar" id="sidebar">
      <div class="search"><input id="search" placeholder="🔍 Поиск чатов..."></div>
      <div class="list" id="chat-list"></div>
    </div>
    <div class="main" id="main">
      <div class="empty-state"><div class="e">💬</div><div>Выберите чат, чтобы начать общение.</div>
        <div style="font-size:11px">Только чаты, в которых бот участник или был упомянут — это ограничение Bot API.</div>
      </div>
    </div>
  `;
  refreshChatList();
  $("#search").addEventListener("input", refreshChatList);
  if (state.activeChat) openChat(state.activeChat, true);
}

function refreshChatList() {
  const list = $("#chat-list");
  if (!list) return;
  const q = ($("#search")?.value || "").toLowerCase();
  list.innerHTML = "";
  state.chats
    .filter(c => !q || (c.title || "").toLowerCase().includes(q) || (c.username || "").toLowerCase().includes(q))
    .forEach(c => {
      const lm = c.last_message || {};
      const isOut = lm.sender_id === state.me?.id;
      const preview = (lm.text || lm.caption || (lm.media_type ? `[${lm.media_type}]` : "")).slice(0, 80);
      const type = c.type === "private" ? "private" : (c.type === "channel" ? "channel" : "group");
      const row = document.createElement("div");
      row.className = `chat-row ${type}` + (state.activeChat === c.id ? " active" : "");
      row.innerHTML = `
        ${avatarHTML(c.title, c.id)}
        <div class="meta">
          <div class="name"><b>${escapeHTML(c.title || "")}</b><span class="time">${fmtDate(lm.date)}</span></div>
          <div class="preview">${isOut ? '<span style="color:var(--accent)">Вы: </span>' : ""}${escapeHTML(preview)}</div>
        </div>`;
      row.addEventListener("click", () => openChat(c.id));
      list.appendChild(row);
    });
  if (!state.chats.length) {
    list.innerHTML = `<div class="empty-state"><div>Чаты пока пусты.</div>
      <div style="font-size:11px">Напишите боту в Telegram или добавьте его в группу — они появятся здесь автоматически.</div></div>`;
  }
}

async function openChat(chatId, skipMobileTransition) {
  state.activeChat = chatId;
  state.replyTo = null;
  state.editTarget = null;
  if (!state.messages[chatId]) {
    try {
      const r = await api(`/api/chats/${chatId}/messages`);
      state.messages[chatId] = r.messages || [];
      state.senders[chatId] = r.senders || {};
    } catch (e) {
      toast(e.message, "error");
      return;
    }
  }
  refreshChatList();
  renderMain();
  if (window.innerWidth <= 768 && !skipMobileTransition) {
    $("#sidebar")?.classList.add("hidden");
    $("#main")?.classList.remove("hidden");
  }
}

function renderMain() {
  const main = $("#main");
  if (!main) return;
  const chat = state.chats.find(c => c.id === state.activeChat);
  if (!chat) {
    main.innerHTML = `<div class="empty-state"><div class="e">💬</div><div>Чат не выбран.</div></div>`;
    return;
  }
  main.innerHTML = `
    <div class="chat-head">
      <button class="icon-btn back-btn" title="Назад" style="display:none">←</button>
      ${avatarHTML(chat.title, chat.id)}
      <div class="info">
        <div class="t">${escapeHTML(chat.title || "")}</div>
        <div class="s">${chat.type} · id ${chat.id}</div>
      </div>
      <div class="actions">
        <button class="icon-btn" id="btn-info" title="Информация">ℹ️</button>
      </div>
    </div>
    <div class="messages" id="messages"></div>
    <div class="composer" id="composer">
      <div class="drop-overlay">Бросьте файл сюда</div>
      <div id="reply-bar"></div>
      <div class="row">
        <button class="icon-btn" id="btn-attach" title="Прикрепить">📎</button>
        <button class="icon-btn" id="btn-poll" title="Опрос">📊</button>
        <button class="icon-btn" id="btn-buttons" title="Inline-кнопки">🔘</button>
        <textarea id="msg-input" placeholder="Сообщение..." rows="1"></textarea>
        <button class="send" id="send-btn">➤</button>
      </div>
    </div>
    <input type="file" id="file-picker" accept="image/*,video/*,*/*" multiple style="display:none">
  `;
  renderMessages();
  setupComposer();
  if (window.innerWidth <= 768) {
    const b = $(".back-btn"); b.style.display = "inline-flex";
    b.addEventListener("click", () => {
      $("#sidebar")?.classList.remove("hidden");
      $("#main")?.classList.add("hidden");
    });
  }
}

function entityHTML(text, entities) {
  if (!entities || !entities.length) return escapeHTML(text).replace(/\n/g, "<br>");
  // Sort entities by offset asc
  const ents = entities.slice().sort((a, b) => a.offset - b.offset);
  let out = "";
  let cur = 0;
  const chars = Array.from(text);
  for (const e of ents) {
    if (e.offset > cur) out += escapeHTML(chars.slice(cur, e.offset).join(""));
    const seg = escapeHTML(chars.slice(e.offset, e.offset + e.length).join(""));
    if (e.type === "bold" || e.type === "MessageEntityType.BOLD") out += `<b>${seg}</b>`;
    else if (e.type === "italic" || e.type === "MessageEntityType.ITALIC") out += `<i>${seg}</i>`;
    else if (e.type === "underline" || e.type === "MessageEntityType.UNDERLINE") out += `<u>${seg}</u>`;
    else if (e.type === "strikethrough") out += `<s>${seg}</s>`;
    else if (e.type === "spoiler" || e.type === "MessageEntityType.SPOILER") out += `<span class="spoiler" style="background:rgba(255,255,255,.15);border-radius:4px;cursor:pointer" onclick="this.style.background='transparent'">${seg}</span>`;
    else if (e.type === "code" || e.type === "MessageEntityType.CODE") out += `<code style="background:rgba(0,0,0,.3);padding:0 4px;border-radius:4px;font-family:monospace">${seg}</code>`;
    else if (e.type === "pre" || e.type === "MessageEntityType.PRE") out += `<pre style="background:rgba(0,0,0,.3);padding:8px;border-radius:6px;font-family:monospace;overflow-x:auto;margin:4px 0">${seg}</pre>`;
    else if (e.type === "url" || e.type === "MessageEntityType.URL") out += `<a href="${seg}" target="_blank" rel="noopener" style="color:var(--accent)">${seg}</a>`;
    else if (e.type === "text_link" || e.type === "MessageEntityType.TEXT_LINK") out += `<a href="${escapeHTML(e.url || '#')}" target="_blank" rel="noopener" style="color:var(--accent)">${seg}</a>`;
    else if (e.type === "mention" || e.type === "MessageEntityType.MENTION") out += `<span style="color:var(--accent)">${seg}</span>`;
    else if (e.type === "hashtag" || e.type === "MessageEntityType.HASHTAG") out += `<span style="color:var(--accent)">${seg}</span>`;
    else if (e.type === "custom_emoji" || e.type === "MessageEntityType.CUSTOM_EMOJI") out += `<span class="custom-emoji" data-id="${e.custom_emoji_id}" title="Premium emoji ${e.custom_emoji_id}">${seg}</span>`;
    else out += seg;
    cur = e.offset + e.length;
  }
  if (cur < chars.length) out += escapeHTML(chars.slice(cur).join(""));
  return out.replace(/\n/g, "<br>");
}

function renderMessages() {
  const cont = $("#messages");
  if (!cont) return;
  const msgs = state.messages[state.activeChat] || [];
  const senders = state.senders[state.activeChat] || {};
  cont.innerHTML = "";
  let lastDay = "";
  let lastSender = null;
  for (const m of msgs) {
    if (m.deleted) continue;
    const dayKey = fmtDay(m.date);
    if (dayKey !== lastDay) {
      const d = document.createElement("div");
      d.className = "day";
      d.textContent = dayKey;
      cont.appendChild(d);
      lastDay = dayKey;
      lastSender = null;
    }
    const isOut = m.sender_id === state.me?.id;
    const sender = senders[m.sender_id] || null;
    const row = document.createElement("div");
    row.className = "msg-row " + (isOut ? "out" : "in") + (lastSender === m.sender_id ? " grp" : "");
    row.dataset.id = m.message_id;
    const avSrc = m.sender_id ? `/api/users/${m.sender_id}/photo` : null;
    const name = sender ? ((sender.first_name || "") + " " + (sender.last_name || "")).trim() || sender.username || ("id" + sender.id) : "";
    const replyHTML = m.reply_to_message_id ? renderReplyPreview(m.reply_to_message_id) : "";
    let bodyHTML = "";
    const isStickerOnly = m.media_type === "sticker" && !m.text && !m.caption;
    if (m.media_type === "photo") bodyHTML += `<img class="attach" loading="lazy" src="/file/${m.file_id}" alt="">`;
    else if (m.media_type === "video") bodyHTML += `<video class="attach" src="/file/${m.file_id}" controls></video>`;
    else if (m.media_type === "animation") bodyHTML += `<video class="attach" src="/file/${m.file_id}" autoplay muted loop></video>`;
    else if (m.media_type === "sticker") bodyHTML += `<img class="attach sticker" loading="lazy" src="/file/${m.file_id}" alt="${escapeHTML(m.sticker_emoji || '')}">`;
    else if (m.media_type === "voice" || m.media_type === "audio") bodyHTML += `<audio class="attach" src="/file/${m.file_id}" controls></audio>`;
    else if (m.media_type === "video_note") bodyHTML += `<video class="attach" src="/file/${m.file_id}" controls style="border-radius:50%;max-width:200px"></video>`;
    else if (m.media_type === "document") bodyHTML += `<div class="doc"><div class="ic">📄</div><div><div>${escapeHTML(m.file_id || "")}</div><a href="/file/${m.file_id}" target="_blank" style="color:var(--accent);font-size:12px">Скачать</a></div></div>`;
    if (m.text) bodyHTML += `<div class="text">${entityHTML(m.text, m.entities)}</div>`;
    else if (m.caption) bodyHTML += `<div class="caption">${entityHTML(m.caption, m.entities)}</div>`;
    if (m.poll) bodyHTML += renderPoll(m.poll);
    if (m.reply_markup) bodyHTML += renderInlineKeyboard(m.reply_markup);
    if (m.reactions && m.reactions.length) bodyHTML += renderReactions(m.reactions);

    const showSender = !isOut && (m.sender_id !== lastSender) && (state.chats.find(c=>c.id===state.activeChat)?.type !== "private");
    row.innerHTML = `
      ${!isOut ? avatarHTML(name, m.sender_id, avSrc) : ""}
      <div class="bubble ${isStickerOnly ? 'sticker-only' : ''}" data-id="${m.message_id}">
        ${showSender ? `<div class="sender" style="color:${avatarColor(m.sender_id)}">${escapeHTML(name)}</div>` : ""}
        ${replyHTML}
        ${bodyHTML}
        <div class="ts">${m.edit_date ? '<span class="edited">edited</span>' : ""}${fmtTime(m.date)}</div>
      </div>
      <div class="swipe-icon">↩</div>
    `;
    attachMessageGestures(row, m);
    cont.appendChild(row);
    lastSender = m.sender_id;
  }
  cont.scrollTop = cont.scrollHeight;
}

function renderReplyPreview(replyId) {
  const arr = state.messages[state.activeChat] || [];
  const r = arr.find(x => x.message_id === replyId);
  if (!r) return `<div class="reply">Сообщение #${replyId}</div>`;
  const sender = (state.senders[state.activeChat] || {})[r.sender_id] || {};
  const name = (sender.first_name || sender.username || "сообщение");
  const txt = (r.text || r.caption || `[${r.media_type || 'media'}]`).slice(0, 60);
  return `<div class="reply" data-id="${r.message_id}"><div class="reply-name">${escapeHTML(name)}</div><div>${escapeHTML(txt)}</div></div>`;
}

function renderPoll(p) {
  const opts = (p.options || []).map(o => `<div class="opt"><span>${escapeHTML(o.text)}</span><span>${o.voter_count}</span></div>`).join("");
  return `<div class="poll"><h4>${escapeHTML(p.question)}</h4>${opts}<div style="font-size:11px;color:var(--muted);margin-top:6px">Голосов: ${p.total_voter_count}${p.is_closed ? " · закрыт" : ""}</div></div>`;
}

function renderInlineKeyboard(rm) {
  const rows = (rm.inline_keyboard || []).map(row => {
    const cells = row.map(b => {
      if (b.url) return `<a href="${escapeHTML(b.url)}" target="_blank" rel="noopener" style="flex:1"><button style="width:100%">${escapeHTML(b.text)}</button></a>`;
      return `<button data-cb="${escapeHTML(b.callback_data || '')}">${escapeHTML(b.text)}</button>`;
    }).join("");
    return `<div class="row">${cells}</div>`;
  }).join("");
  return `<div class="ikb">${rows}</div>`;
}

function renderReactions(rs) {
  // Group by emoji
  const counts = {};
  for (const r of rs) {
    const key = r.type === "custom" ? `custom:${r.custom_emoji_id}` : r.emoji;
    counts[key] = (counts[key] || 0) + 1;
  }
  return `<div class="reactions">` + Object.entries(counts).map(([k, c]) =>
    `<span class="reaction" data-emoji="${escapeHTML(k.startsWith('custom:') ? '✨' : k)}">${k.startsWith('custom:') ? '✨' : escapeHTML(k)} ${c}</span>`
  ).join("") + `</div>`;
}

// ---------- gestures ----------
function attachMessageGestures(row, msg) {
  // swipe-to-reply (touch)
  let startX = 0, startY = 0, dx = 0, dragging = false;
  row.addEventListener("touchstart", e => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    dragging = true;
  }, { passive: true });
  row.addEventListener("touchmove", e => {
    if (!dragging || e.touches.length !== 1) return;
    dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if (Math.abs(dy) > Math.abs(dx)) { dragging = false; return; }
    if (dx > 0 && dx < 100) {
      row.style.transform = `translateX(${dx}px)`;
      row.classList.add("swiping");
    }
  }, { passive: true });
  row.addEventListener("touchend", () => {
    if (!dragging) return;
    if (dx > 60) {
      setReply(msg.message_id);
    }
    row.style.transform = "";
    row.classList.remove("swiping");
    dragging = false;
    dx = 0;
  });

  // long-press / right-click → context menu
  let pressTimer = null;
  const showMenu = (x, y) => {
    showContextMenu(x, y, msg);
  };
  row.addEventListener("contextmenu", e => {
    e.preventDefault();
    showMenu(e.clientX, e.clientY);
  });
  row.addEventListener("touchstart", e => {
    pressTimer = setTimeout(() => {
      const t = e.touches[0];
      if (t) showMenu(t.clientX, t.clientY);
    }, 500);
  }, { passive: true });
  row.addEventListener("touchend", () => clearTimeout(pressTimer));
  row.addEventListener("touchmove", () => clearTimeout(pressTimer));

  // double-tap to react ❤️
  let lastTap = 0;
  row.addEventListener("click", e => {
    const now = Date.now();
    if (now - lastTap < 300) {
      reactQuick(msg, "❤");
    }
    lastTap = now;
    // image zoom
    if (e.target.tagName === "IMG" && e.target.classList.contains("attach") && !e.target.classList.contains("sticker")) {
      openViewer(e.target.src);
    }
  });
}

function showContextMenu(x, y, msg) {
  closeContextMenu();
  const isMine = msg.sender_id === state.me?.id;
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  menu.innerHTML = `
    <div class="react-row">
      ${["❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🎉", "💯"].map(e =>
        `<div class="e" data-react="${e}">${e}</div>`).join("")}
    </div>
    <div class="sep"></div>
    <div class="item" data-act="reply">↩ Ответить</div>
    <div class="item" data-act="copy">📋 Копировать</div>
    ${isMine ? '<div class="item" data-act="edit">✏ Редактировать</div>' : ""}
    ${isMine ? '<div class="item danger" data-act="delete">🗑 Удалить</div>' : ""}
    <div class="item" data-act="id">🔗 Скопировать ID</div>
  `;
  document.body.appendChild(menu);
  // Position to fit viewport
  const w = 200, h = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - w - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - h - 8) + "px";

  menu.addEventListener("click", async ev => {
    const t = ev.target.closest("[data-act],[data-react]");
    if (!t) return;
    if (t.dataset.react) {
      await reactQuick(msg, t.dataset.react);
    } else if (t.dataset.act === "reply") setReply(msg.message_id);
    else if (t.dataset.act === "copy") {
      navigator.clipboard.writeText(msg.text || msg.caption || "");
      toast("Скопировано", "ok");
    } else if (t.dataset.act === "edit") startEdit(msg);
    else if (t.dataset.act === "delete") deleteMessage(msg);
    else if (t.dataset.act === "id") {
      navigator.clipboard.writeText(String(msg.message_id));
      toast("ID скопирован", "ok");
    }
    closeContextMenu();
  });
  setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
}
function closeContextMenu() {
  $$(".ctx-menu").forEach(m => m.remove());
}

async function reactQuick(msg, emoji) {
  try {
    await api("/api/reaction", { method: "POST", body: { chat_id: msg.chat_id, message_id: msg.message_id, emoji } });
  } catch (e) { toast(e.message, "error"); }
}
async function deleteMessage(msg) {
  if (!confirm("Удалить сообщение?")) return;
  try {
    await api("/api/delete", { method: "POST", body: { chat_id: msg.chat_id, message_id: msg.message_id } });
    msg.deleted = 1;
    renderMessages();
  } catch (e) { toast(e.message, "error"); }
}

function setReply(messageId) {
  state.replyTo = messageId;
  state.editTarget = null;
  drawReplyBar();
  $("#msg-input")?.focus();
}
function startEdit(msg) {
  state.editTarget = msg;
  state.replyTo = null;
  $("#msg-input").value = msg.text || msg.caption || "";
  drawReplyBar();
  $("#msg-input")?.focus();
}
function drawReplyBar() {
  const bar = $("#reply-bar");
  if (!bar) return;
  if (state.editTarget) {
    bar.innerHTML = `<div class="reply-bar"><span>✏</span><div class="name">Редактируем</div><div style="flex:1;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHTML((state.editTarget.text || state.editTarget.caption || "").slice(0,80))}</div><button onclick="cancelReplyEdit()">✕</button></div>`;
  } else if (state.replyTo) {
    const m = (state.messages[state.activeChat] || []).find(x => x.message_id === state.replyTo);
    bar.innerHTML = `<div class="reply-bar"><span>↩</span><div class="name">Ответ</div><div style="flex:1;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHTML((m?.text || m?.caption || "...").slice(0,80))}</div><button onclick="cancelReplyEdit()">✕</button></div>`;
  } else bar.innerHTML = "";
}
window.cancelReplyEdit = function() { state.replyTo = null; state.editTarget = null; $("#msg-input").value = ""; drawReplyBar(); };

// ---------- composer ----------
function setupComposer() {
  const ta = $("#msg-input");
  const send = $("#send-btn");
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(160, ta.scrollHeight) + "px";
  });
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
  send.addEventListener("click", doSend);

  $("#btn-attach").addEventListener("click", () => $("#file-picker").click());
  $("#file-picker").addEventListener("change", e => {
    for (const f of e.target.files) uploadFile(f);
    e.target.value = "";
  });
  $("#btn-poll").addEventListener("click", openPollModal);
  $("#btn-buttons").addEventListener("click", openButtonsModal);

  // drag-and-drop
  const comp = $("#composer");
  ["dragenter", "dragover"].forEach(ev => comp.addEventListener(ev, e => { e.preventDefault(); comp.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => comp.addEventListener(ev, e => { e.preventDefault(); if (ev === "drop") for (const f of e.dataTransfer.files) uploadFile(f); comp.classList.remove("drag"); }));
}

async function doSend() {
  const ta = $("#msg-input");
  const text = ta.value.trim();
  if (!text || !state.activeChat) return;
  ta.disabled = true;
  try {
    if (state.editTarget) {
      await api("/api/edit", { method: "POST", body: { chat_id: state.activeChat, message_id: state.editTarget.message_id, text } });
    } else {
      const body = { chat_id: state.activeChat, text };
      if (state.replyTo) body.reply_to_message_id = state.replyTo;
      if (window._pendingButtons) {
        body.inline_keyboard = window._pendingButtons;
        window._pendingButtons = null;
      }
      await api("/api/send", { method: "POST", body });
    }
    ta.value = "";
    state.replyTo = null;
    state.editTarget = null;
    drawReplyBar();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    ta.disabled = false;
    ta.style.height = "auto";
    ta.focus();
  }
}

async function uploadFile(f) {
  const fd = new FormData();
  fd.append("chat_id", String(state.activeChat));
  fd.append("file", f, f.name);
  if (state.replyTo) fd.append("reply_to_message_id", String(state.replyTo));
  try {
    await fetch("/api/send_photo", { method: "POST", headers: { "X-Token": state.token }, body: fd });
    toast("Отправлено", "ok");
    state.replyTo = null;
    drawReplyBar();
  } catch (e) { toast(e.message, "error"); }
}

function openPollModal() {
  modal({
    title: "Создать опрос",
    body: `
      <input id="poll-q" placeholder="Вопрос">
      <textarea id="poll-opts" placeholder="Варианты (по одному в строке)"></textarea>
      <label class="row-h"><input type="checkbox" id="poll-anon" checked> Анонимный</label>
      <label class="row-h"><input type="checkbox" id="poll-multi"> Несколько ответов</label>
    `,
    onOk: async () => {
      const q = $("#poll-q").value.trim();
      const opts = $("#poll-opts").value.split("\n").map(s => s.trim()).filter(Boolean);
      if (!q || opts.length < 2) { toast("Минимум 2 варианта", "error"); return false; }
      try {
        await api("/api/send_poll", {
          method: "POST",
          body: { chat_id: state.activeChat, question: q, options: opts, anonymous: $("#poll-anon").checked, multiple: $("#poll-multi").checked }
        });
        return true;
      } catch (e) { toast(e.message, "error"); return false; }
    }
  });
}

function openButtonsModal() {
  const cur = window._pendingButtons || [[{ text: "", url: "" }]];
  const draw = () => {
    return cur.map((row, ri) => row.map((b, ci) =>
      `<div class="row-h" style="gap:4px">
        <input data-r="${ri}" data-c="${ci}" data-f="text" placeholder="Текст" value="${escapeHTML(b.text || '')}">
        <input data-r="${ri}" data-c="${ci}" data-f="url" placeholder="URL (опц.)" value="${escapeHTML(b.url || '')}">
        <input data-r="${ri}" data-c="${ci}" data-f="callback_data" placeholder="callback" value="${escapeHTML(b.callback_data || '')}">
      </div>`).join("")).join("<hr style='border:0;border-top:1px solid var(--line)'>");
  };
  modal({
    title: "Inline-кнопки",
    body: `<div id="btns-area">${draw()}</div>
      <div class="row-h">
        <button onclick="window._addBtnRow()">+ Ряд</button>
        <button onclick="window._addBtnInRow()">+ Кнопка в ряд</button>
        <button class="danger" onclick="window._clearBtns()">Очистить</button>
      </div>
      <div class="hint" style="font-size:11px;color:var(--muted)">Кнопки будут прикреплены к следующему отправленному сообщению.</div>`,
    onOk: () => {
      const buttons = [];
      const rows = $$(".row-h", $("#btns-area")).length ? cur : cur;
      $$('#btns-area input').forEach(inp => {
        const r = +inp.dataset.r, c = +inp.dataset.c, f = inp.dataset.f;
        if (!cur[r]) cur[r] = [];
        if (!cur[r][c]) cur[r][c] = {};
        cur[r][c][f] = inp.value;
      });
      const cleaned = cur.map(r => r.filter(b => b.text && b.text.trim()).map(b => {
        const out = { text: b.text.trim() };
        if (b.url) out.url = b.url;
        if (b.callback_data) out.callback_data = b.callback_data;
        return out;
      })).filter(r => r.length);
      window._pendingButtons = cleaned.length ? cleaned : null;
      if (cleaned.length) toast(`Прикреплено ${cleaned.flat().length} кнопок`, "ok");
      return true;
    }
  });
  window._addBtnRow = () => { cur.push([{ text: "" }]); $("#btns-area").innerHTML = draw(); };
  window._addBtnInRow = () => { cur[cur.length - 1].push({ text: "" }); $("#btns-area").innerHTML = draw(); };
  window._clearBtns = () => { cur.length = 0; cur.push([{ text: "" }]); $("#btns-area").innerHTML = draw(); };
}

// ---------- modal ----------
function modal({ title, body, onOk, okText = "OK", cancelText = "Отмена" }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal">
    <header><h3>${escapeHTML(title)}</h3><button onclick="this.closest('.modal-back').remove()">✕</button></header>
    <div class="content">${body}</div>
    <footer>
      <button onclick="this.closest('.modal-back').remove()">${cancelText}</button>
      <button class="primary" id="modal-ok">${okText}</button>
    </footer>
  </div>`;
  document.body.appendChild(back);
  $("#modal-ok", back).addEventListener("click", async () => {
    const ok = onOk ? await onOk() : true;
    if (ok !== false) back.remove();
  });
  back.addEventListener("click", e => { if (e.target === back) back.remove(); });
}

// ---------- viewer (pinch zoom) ----------
function openViewer(src) {
  const v = document.createElement("div");
  v.className = "viewer";
  v.innerHTML = `<span class="close">✕</span><img src="${escapeHTML(src)}">`;
  document.body.appendChild(v);
  v.querySelector(".close").addEventListener("click", () => v.remove());
  v.addEventListener("click", e => { if (e.target === v) v.remove(); });
  const img = v.querySelector("img");
  let scale = 1, tx = 0, ty = 0;
  let initialDist = 0, initialScale = 1;
  let lastX = 0, lastY = 0, panning = false;
  img.addEventListener("touchstart", e => {
    if (e.touches.length === 2) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      initialDist = Math.hypot(dx, dy);
      initialScale = scale;
    } else if (e.touches.length === 1) {
      panning = true; lastX = e.touches[0].clientX; lastY = e.touches[0].clientY;
    }
  });
  img.addEventListener("touchmove", e => {
    if (e.touches.length === 2) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const d = Math.hypot(dx, dy);
      scale = Math.max(1, Math.min(5, initialScale * (d / initialDist)));
    } else if (e.touches.length === 1 && panning && scale > 1) {
      const x = e.touches[0].clientX, y = e.touches[0].clientY;
      tx += x - lastX; ty += y - lastY;
      lastX = x; lastY = y;
    }
    img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
  img.addEventListener("touchend", () => { panning = false; if (scale === 1) { tx = ty = 0; img.style.transform = ""; } });
  img.addEventListener("wheel", e => {
    e.preventDefault();
    scale = Math.max(1, Math.min(5, scale + (e.deltaY < 0 ? 0.2 : -0.2)));
    img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
}

// ============== PROFILE TAB ==============
async function renderProfile(root) {
  root.innerHTML = `<div class="page" id="prof"></div>`;
  const p = $("#prof");
  p.innerHTML = `<div class="spinner"></div>`;
  const me = state.me;
  p.innerHTML = `
    <h2>Профиль бота @${escapeHTML(me.username || "")}</h2>
    <div class="section">
      <h3>Имя и описание</h3>
      <div class="field"><label>Отображаемое имя (set_my_name)</label>
        <input id="p-name" value="${escapeHTML(me.first_name || '')}"></div>
      <div class="field"><label>Описание (видно до старта в боте, set_my_description)</label>
        <textarea id="p-desc" rows="3">${escapeHTML(me.description || '')}</textarea></div>
      <div class="field"><label>Краткое описание (в карточке бота, set_my_short_description)</label>
        <textarea id="p-short" rows="2">${escapeHTML(me.short_description || '')}</textarea></div>
      <button class="primary" id="p-save">Сохранить</button>
    </div>

    <div class="section">
      <h3>Команды (/help, /id ...)</h3>
      <div id="cmd-list"></div>
      <button onclick="window._addCmd()">+ Команда</button>
      <button class="primary" onclick="window._saveCmds()">Сохранить команды</button>
    </div>

    <div class="section">
      <h3>Аватарка бота</h3>
      <div style="color:var(--muted);font-size:13px">
        ⚠️ Bot API не позволяет менять аватарку самого бота напрямую — это можно сделать только через
        <a href="https://t.me/BotFather" target="_blank" style="color:var(--accent)">@BotFather</a> → /mybots → выбрать бота → Bot Settings → Edit Bot → Edit Botpic.
        Если бот — админ канала <code>${TG_CHANNEL_ID || '...'}</code>, можно менять аватарку канала:
      </div>
      <input type="file" id="ch-avatar" accept="image/*">
      <button class="primary" id="ch-avatar-go" disabled>Заменить аватарку канала</button>
    </div>

    <div class="section">
      <h3>Возможности</h3>
      <div>can_join_groups: ${me.can_join_groups ? "✅" : "❌"}</div>
      <div>can_read_all_group_messages: ${me.can_read_all_group_messages ? "✅" : "❌ (включите в BotFather → Group Privacy → Disable)"}</div>
      <div>supports_inline_queries: ${me.supports_inline_queries ? "✅" : "❌"}</div>
    </div>
  `;
  $("#p-save").addEventListener("click", async () => {
    try {
      await api("/api/profile", { method: "POST", body: {
        name: $("#p-name").value, description: $("#p-desc").value, short_description: $("#p-short").value
      }});
      toast("Сохранено", "ok");
    } catch (e) { toast(e.message, "error"); }
  });
  const commands = []; // load existing? Not available via API easily; user starts from scratch
  const drawCmds = () => {
    $("#cmd-list").innerHTML = commands.map((c, i) => `<div class="row-h" style="margin-bottom:6px">
      <input style="flex:1" value="${escapeHTML(c.command)}" oninput="window._cmd(${i}, 'command', this.value)">
      <input style="flex:2" value="${escapeHTML(c.description)}" oninput="window._cmd(${i}, 'description', this.value)">
      <button class="danger" onclick="window._delCmd(${i})">✕</button>
    </div>`).join("");
  };
  window._cmd = (i, k, v) => { commands[i][k] = v; };
  window._addCmd = () => { commands.push({ command: "start", description: "Запуск" }); drawCmds(); };
  window._delCmd = (i) => { commands.splice(i, 1); drawCmds(); };
  window._saveCmds = async () => {
    try {
      await api("/api/profile", { method: "POST", body: { commands: commands.map(c => ({ command: c.command.replace(/^\//, ''), description: c.description })) } });
      toast("Команды сохранены", "ok");
    } catch (e) { toast(e.message, "error"); }
  };
  drawCmds();
}

// ============== MODERATION TAB ==============
async function renderModeration(root) {
  root.innerHTML = `
    <div class="sidebar">
      <div class="search"><input id="mod-search" placeholder="🔍 Поиск чатов..."></div>
      <div class="list" id="mod-list"></div>
    </div>
    <div class="main"><div id="mod-page" class="page"></div></div>
  `;
  const list = $("#mod-list");
  const draw = () => {
    const q = ($("#mod-search").value || "").toLowerCase();
    list.innerHTML = "";
    state.chats.filter(c => c.type !== "private")
      .filter(c => !q || (c.title || "").toLowerCase().includes(q))
      .forEach(c => {
        const row = document.createElement("div");
        row.className = "chat-row";
        row.innerHTML = `${avatarHTML(c.title, c.id)}<div class="meta"><div class="name"><b>${escapeHTML(c.title || '')}</b></div><div class="preview">${c.type}</div></div>`;
        row.addEventListener("click", () => loadMod(c.id));
        list.appendChild(row);
      });
  };
  draw();
  $("#mod-search").addEventListener("input", draw);
  $("#mod-page").innerHTML = `<div class="empty-state"><div class="e">🛡️</div><div>Выберите чат для настройки авто-модерации.</div></div>`;
}

async function loadMod(chatId) {
  const p = $("#mod-page");
  p.innerHTML = `<div class="spinner"></div>`;
  try {
    const m = await api(`/api/moderation/${chatId}`);
    const chat = state.chats.find(c => c.id === chatId);
    p.innerHTML = `
      <h2>🛡 ${escapeHTML(chat?.title || chatId)}</h2>
      <div class="section">
        <div class="row-h between">
          <div><b>Включить авто-модерацию</b><div style="font-size:11px;color:var(--muted)">Бот должен быть админом, чтобы удалять/банить</div></div>
          <label class="switch"><input type="checkbox" id="m-enabled" ${m.enabled?"checked":""}><span class="slider"></span></label>
        </div>
        <div class="field"><label>Действие при срабатывании</label>
          <select id="m-action">
            <option value="delete" ${m.action==="delete"?"selected":""}>Удалить сообщение</option>
            <option value="warn" ${m.action==="warn"?"selected":""}>Удалить + предупреждение</option>
            <option value="mute" ${m.action==="mute"?"selected":""}>Удалить + замутить</option>
            <option value="ban" ${m.action==="ban"?"selected":""}>Удалить + бан</option>
          </select></div>
        <div class="row-h"><div class="field" style="flex:1"><label>Мут (минут)</label><input type="number" id="m-mute" value="${m.mute_minutes}"></div>
          <div class="field" style="flex:1"><label>Лимит предупреждений</label><input type="number" id="m-warn" value="${m.warn_threshold}"></div></div>
      </div>

      <div class="section"><h3>Бан-слова</h3>
        <div id="banwords">${(m.banwords || []).map(w => `<span class="chip">${escapeHTML(w)} <span class="x" onclick="this.parentElement.remove()">✕</span></span>`).join(" ")}</div>
        <div class="row-h"><input id="bw-input" placeholder="новое слово или фраза"><button onclick="window._addBw()">+</button></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>Анти-флуд</b></div><label class="switch"><input type="checkbox" id="m-flood" ${m.antiflood?"checked":""}><span class="slider"></span></label></div>
        <div class="row-h"><div class="field" style="flex:1"><label>Макс. сообщений</label><input type="number" id="m-flood-c" value="${m.antiflood_count}"></div>
          <div class="field" style="flex:1"><label>За секунд</label><input type="number" id="m-flood-s" value="${m.antiflood_seconds}"></div></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>Анти-ссылки</b><div style="font-size:11px;color:var(--muted)">Блок http(s)://, t.me/, @username</div></div><label class="switch"><input type="checkbox" id="m-links" ${m.antilinks?"checked":""}><span class="slider"></span></label></div>
      </div>
      <div class="section">
        <div class="row-h between"><div><b>Анти-капс</b></div><label class="switch"><input type="checkbox" id="m-caps" ${m.anticaps?"checked":""}><span class="slider"></span></label></div>
        <div class="field"><label>Порог % заглавных</label><input type="number" id="m-caps-t" value="${m.caps_threshold}"></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>AI-модерация</b><div style="font-size:11px;color:var(--muted)">Подозрительные сообщения проверяются через OpenRouter</div></div><label class="switch"><input type="checkbox" id="m-ai" ${m.ai_moderation?"checked":""}><span class="slider"></span></label></div>
        <div class="field"><label>Модель</label>
          <select id="m-ai-model">
            ${state.models.filter(x => x.kind==="chat").map(x => `<option value="${escapeHTML(x.id)}" ${m.ai_model===x.id?"selected":""}>${escapeHTML(x.id)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Порог токсичности (0..100)</label><input type="number" id="m-ai-t" value="${m.ai_threshold}"></div>
      </div>

      <div class="row-h"><button class="primary" id="m-save">Сохранить</button>
        <span style="color:var(--muted);font-size:12px">Изменения применяются мгновенно к новым сообщениям.</span></div>
    `;
    window._addBw = () => {
      const v = $("#bw-input").value.trim();
      if (!v) return;
      const el = document.createElement("span");
      el.className = "chip";
      el.innerHTML = `${escapeHTML(v)} <span class="x" onclick="this.parentElement.remove()">✕</span>`;
      $("#banwords").appendChild(el);
      $("#banwords").appendChild(document.createTextNode(" "));
      $("#bw-input").value = "";
    };
    $("#m-save").addEventListener("click", async () => {
      const banwords = $$("#banwords .chip").map(c => c.firstChild.textContent.trim());
      try {
        await api("/api/moderation", { method: "POST", body: {
          chat_id: chatId,
          enabled: $("#m-enabled").checked, action: $("#m-action").value,
          mute_minutes: +$("#m-mute").value, warn_threshold: +$("#m-warn").value,
          banwords, antiflood: $("#m-flood").checked,
          antiflood_count: +$("#m-flood-c").value, antiflood_seconds: +$("#m-flood-s").value,
          antilinks: $("#m-links").checked, anticaps: $("#m-caps").checked,
          caps_threshold: +$("#m-caps-t").value,
          ai_moderation: $("#m-ai").checked, ai_threshold: +$("#m-ai-t").value, ai_model: $("#m-ai-model").value
        }});
        toast("Сохранено", "ok");
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) {
    p.innerHTML = `<div class="empty-state">${escapeHTML(e.message)}</div>`;
  }
}

// ============== AI TAB ==============
function renderAI(root) {
  root.innerHTML = `<div class="ai-pane">
    <div class="ai-side" id="ai-side">
      <button class="primary" id="ai-new">+ Новый чат</button>
      <div id="ai-list"></div>
    </div>
    <div class="ai-main">
      <div class="ai-messages" id="ai-msgs"></div>
      <div class="ai-compose">
        <select id="ai-model">
          ${state.models.map(m => `<option value="${escapeHTML(m.id)}">[${m.kind}] ${escapeHTML(m.id)}</option>`).join("")}
        </select>
        <label class="row-h" style="white-space:nowrap"><input type="checkbox" id="ai-reasoning"> reasoning</label>
        <textarea id="ai-input" placeholder="Спросите что угодно..."></textarea>
        <button class="primary" id="ai-send">➤</button>
      </div>
    </div>
  </div>`;
  loadAiChats();
  $("#ai-new").addEventListener("click", () => {
    state.aiActive = { id: "tmp" + Date.now(), title: "Новый чат", model: state.models[0]?.id, messages: [] };
    state.aiChats.unshift(state.aiActive);
    drawAiList();
    drawAiMsgs();
  });
  $("#ai-send").addEventListener("click", aiSend);
  $("#ai-input").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); aiSend(); }});
  $("#ai-model").addEventListener("change", () => { if (state.aiActive) state.aiActive.model = $("#ai-model").value; });
}

function loadAiChats() {
  try {
    const raw = localStorage.getItem("ai_chats");
    state.aiChats = raw ? JSON.parse(raw) : [];
  } catch { state.aiChats = []; }
  drawAiList();
}
function saveAiChats() {
  localStorage.setItem("ai_chats", JSON.stringify(state.aiChats.slice(0, 40)));
}
function drawAiList() {
  const list = $("#ai-list");
  if (!list) return;
  list.innerHTML = state.aiChats.map(c =>
    `<div class="ai-chat ${state.aiActive?.id === c.id ? 'active' : ''}" data-id="${c.id}">
      <span>${escapeHTML(c.title || 'Чат')}</span>
      <span class="danger" data-del="${c.id}">✕</span>
    </div>`).join("");
  list.addEventListener("click", e => {
    const del = e.target.closest("[data-del]");
    if (del) { state.aiChats = state.aiChats.filter(c => c.id !== del.dataset.del); if (state.aiActive?.id === del.dataset.del) state.aiActive = null; saveAiChats(); drawAiList(); drawAiMsgs(); return; }
    const it = e.target.closest("[data-id]");
    if (it) { state.aiActive = state.aiChats.find(c => c.id === it.dataset.id); drawAiList(); drawAiMsgs(); }
  });
}
function drawAiMsgs() {
  const c = $("#ai-msgs"); if (!c) return;
  c.innerHTML = "";
  if (!state.aiActive) { c.innerHTML = `<div class="empty-state"><div class="e">✨</div><div>Создайте новый чат с AI.</div></div>`; return; }
  $("#ai-model").value = state.aiActive.model || state.models[0]?.id;
  for (const m of state.aiActive.messages) {
    const el = document.createElement("div");
    el.className = "ai-msg " + m.role;
    if (typeof m.content === "string") el.innerHTML = escapeHTML(m.content).replace(/\n/g, "<br>");
    else if (Array.isArray(m.content)) {
      el.innerHTML = m.content.map(part => {
        if (part.type === "text") return escapeHTML(part.text).replace(/\n/g, "<br>");
        if (part.type === "image_url") return `<img src="${escapeHTML(part.image_url.url)}">`;
        return "";
      }).join("");
    }
    c.appendChild(el);
  }
  c.scrollTop = c.scrollHeight;
}

async function aiSend() {
  if (!state.aiActive) {
    state.aiActive = { id: "tmp" + Date.now(), title: "Новый чат", model: $("#ai-model").value, messages: [] };
    state.aiChats.unshift(state.aiActive);
  }
  const text = $("#ai-input").value.trim();
  if (!text) return;
  state.aiActive.messages.push({ role: "user", content: text });
  if (state.aiActive.title === "Новый чат") state.aiActive.title = text.slice(0, 30);
  $("#ai-input").value = "";
  drawAiMsgs();
  state.aiActive.messages.push({ role: "assistant", content: "..." });
  drawAiMsgs();
  const model = $("#ai-model").value;
  const isImage = (state.models.find(m => m.id === model) || {}).kind === "image";
  try {
    let resp;
    if (isImage) {
      resp = await api("/api/ai/image", { method: "POST", body: { model, prompt: text } });
    } else {
      resp = await api("/api/ai/chat", { method: "POST", body: { model, messages: state.aiActive.messages.slice(0, -1), reasoning: $("#ai-reasoning").checked } });
    }
    state.aiActive.messages.pop(); // remove placeholder
    if (resp.error) {
      state.aiActive.messages.push({ role: "assistant", content: "Ошибка: " + (resp.error.message || JSON.stringify(resp.error)) });
    } else if (resp.choices && resp.choices[0]) {
      const m = resp.choices[0].message || {};
      if (m.images && m.images.length) {
        state.aiActive.messages.push({ role: "assistant", content: [{ type: "image_url", image_url: { url: m.images[0].image_url.url } }, { type: "text", text: m.content || "" }] });
      } else {
        state.aiActive.messages.push({ role: "assistant", content: m.content || "(пусто)" });
      }
    } else {
      state.aiActive.messages.push({ role: "assistant", content: JSON.stringify(resp).slice(0, 400) });
    }
  } catch (e) {
    state.aiActive.messages.pop();
    state.aiActive.messages.push({ role: "assistant", content: "Ошибка: " + e.message });
  }
  saveAiChats();
  drawAiMsgs();
}

// ============== MINI APPS TAB ==============
async function renderApps(root) {
  root.innerHTML = `<div class="page">
    <h2>🧩 Мини-апы <button class="primary" style="margin-left:8px" id="new-app">+ Создать</button></h2>
    <div class="apps-grid" id="apps-grid"></div>
  </div>`;
  $("#new-app").addEventListener("click", editApp);
  try {
    state.miniApps = await api("/api/mini_apps");
  } catch { state.miniApps = []; }
  drawApps();
}

function drawApps() {
  const g = $("#apps-grid");
  if (!g) return;
  g.innerHTML = state.miniApps.map(a => `
    <div class="app-tile" data-id="${a.id}">
      <div class="icon">${escapeHTML(a.icon || "🧩")}</div>
      <div class="name">${escapeHTML(a.name || "—")}</div>
      <div class="desc">${escapeHTML(a.description || "")}</div>
    </div>`).join("");
  const starterApps = [
    { id: "starter-calc", name: "Калькулятор", icon: "🧮", desc: "Базовый" },
    { id: "starter-notes", name: "Заметки", icon: "📝", desc: "Локальные" },
    { id: "starter-canvas", name: "Канвас", icon: "🎨", desc: "Рисование" },
  ];
  for (const sa of starterApps) {
    if (state.miniApps.find(x => x.id === sa.id)) continue;
    const div = document.createElement("div");
    div.className = "app-tile";
    div.innerHTML = `<div class="icon">${sa.icon}</div><div class="name">${sa.name}</div><div class="desc">${sa.desc} (шаблон)</div>`;
    div.addEventListener("click", () => editApp({ id: sa.id, name: sa.name, icon: sa.icon, description: sa.desc, html: STARTERS[sa.id] }));
    g.appendChild(div);
  }
  g.addEventListener("click", e => {
    const t = e.target.closest("[data-id]"); if (!t) return;
    const app = state.miniApps.find(a => a.id === t.dataset.id);
    if (!app) return;
    if (e.shiftKey || e.altKey) return editApp(app);
    openMiniApp(app.id, app.name);
  });
}

function openMiniApp(id, name) {
  const f = document.createElement("div");
  f.className = "app-frame";
  f.innerHTML = `<div class="app-head"><button onclick="this.closest('.app-frame').remove()">←</button><b>${escapeHTML(name)}</b><span style="flex:1"></span><button onclick="window.editAppById('${id}')">✏</button></div>
    <iframe sandbox="allow-scripts allow-forms allow-modals" src="/mini/${id}"></iframe>`;
  document.body.appendChild(f);
}
window.editAppById = id => editApp(state.miniApps.find(a => a.id === id));

function editApp(app) {
  app = app || { id: "", name: "Новое приложение", icon: "🧩", description: "", html: "<h1>Привет!</h1>" };
  modal({
    title: "Редактор мини-апа",
    body: `
      <div class="field"><label>Название</label><input id="ma-name" value="${escapeHTML(app.name || '')}"></div>
      <div class="row-h"><div class="field" style="flex:1"><label>Иконка (emoji)</label><input id="ma-icon" value="${escapeHTML(app.icon || '🧩')}"></div>
        <div class="field" style="flex:3"><label>Описание</label><input id="ma-desc" value="${escapeHTML(app.description || '')}"></div></div>
      <div class="field"><label>HTML (полный документ или фрагмент)</label>
        <textarea id="ma-html" rows="14" style="font-family:monospace;font-size:12px">${escapeHTML(app.html || '')}</textarea></div>
      <button class="danger" onclick="window._delMa('${app.id}')" ${app.id ? '' : 'style="display:none"'}>🗑 Удалить</button>
    `,
    onOk: async () => {
      try {
        await api("/api/mini_apps", { method: "POST", body: {
          id: app.id || undefined,
          name: $("#ma-name").value, icon: $("#ma-icon").value,
          description: $("#ma-desc").value, html: $("#ma-html").value
        }});
        state.miniApps = await api("/api/mini_apps");
        drawApps();
        return true;
      } catch (e) { toast(e.message, "error"); return false; }
    }
  });
  window._delMa = async id => {
    if (!id) return;
    if (!confirm("Удалить мини-ап?")) return;
    await api("/api/mini_apps/" + id, { method: "DELETE" });
    state.miniApps = await api("/api/mini_apps");
    drawApps();
    $$(".modal-back").forEach(m => m.remove());
  };
}

const STARTERS = {
  "starter-calc": `<!doctype html><html><body style="background:#222;color:#fff;font-family:sans-serif;padding:20px"><h2>Калькулятор</h2><input id=x style="padding:8px;font-size:18px;width:60%"><button onclick="document.getElementById('y').textContent=eval(document.getElementById('x').value)">=</button><div id=y style="font-size:32px;margin-top:14px">0</div></body></html>`,
  "starter-notes": `<!doctype html><html><body style="background:#222;color:#fff;font-family:sans-serif;padding:20px"><h2>Заметки</h2><textarea id=n style="width:100%;height:60vh;background:#333;color:#fff;border:0;padding:8px;font-family:inherit"></textarea><script>const n=document.getElementById('n');n.value=localStorage.getItem('notes')||'';n.oninput=()=>localStorage.setItem('notes',n.value);<\\/script></body></html>`,
  "starter-canvas": `<!doctype html><html><body style="margin:0"><canvas id=c style="display:block;background:#fff;width:100vw;height:100vh"></canvas><script>const c=document.getElementById('c');c.width=innerWidth;c.height=innerHeight;const x=c.getContext('2d');let d=false;c.onpointerdown=e=>{d=true;x.beginPath();x.moveTo(e.offsetX,e.offsetY)};c.onpointermove=e=>{if(d){x.lineTo(e.offsetX,e.offsetY);x.stroke()}};c.onpointerup=()=>d=false;<\\/script></body></html>`
};

// ============== AUDIT TAB ==============
async function renderAudit(root) {
  root.innerHTML = `<div class="page"><h2>📜 Лог событий</h2><div id="audit-list" style="display:flex;flex-direction:column;gap:6px"></div></div>`;
  try {
    const rows = await api("/api/audit");
    $("#audit-list").innerHTML = rows.map(r =>
      `<div class="list-row">
        <div><b>${escapeHTML(r.kind)}</b> ${r.chat_id ? `chat ${r.chat_id}` : ""} ${r.user_id ? `user ${r.user_id}` : ""}
          <div style="font-size:11px;color:var(--muted)">${escapeHTML(r.message || '')}</div></div>
        <div style="color:var(--muted);font-size:11px">${new Date(r.ts*1000).toLocaleString()}</div>
      </div>`).join("");
  } catch (e) { $("#audit-list").innerHTML = `<div>Ошибка: ${escapeHTML(e.message)}</div>`; }
}

// ============== SETTINGS TAB ==============
function renderSettings(root) {
  const me = state.me;
  root.innerHTML = `<div class="page">
    <h2>⚙️ Настройки</h2>
    <div class="section">
      <h3>Сводка</h3>
      <div>Бот: <b>@${escapeHTML(me?.username || '')}</b> (id ${me?.id})</div>
      <div>Владелец: @${escapeHTML(me?.owner_username || '')} (id ${me?.owner_id})</div>
      <div>Канал: ${me?.channel_id}</div>
      <div>Известных моделей: ${state.models.length}</div>
      <div>Открытых WebSocket: <span id="ws-info"></span></div>
    </div>
    <div class="section">
      <h3>PWA / Установка</h3>
      <div>Можно поставить TG Studio как приложение: меню браузера → «Установить приложение».</div>
      <button class="primary" id="pwa-prompt">Попробовать установить</button>
    </div>
    <div class="section">
      <h3>Сессия</h3>
      <button class="danger" id="logout">Выйти</button>
    </div>
    <div class="section" style="color:var(--muted);font-size:12px">
      <h3>Ограничения Telegram Bot API</h3>
      <ul>
        <li>Бот видит только сообщения, адресованные ему, или (в группах) — все, если Group Privacy отключён в @BotFather.</li>
        <li>Нельзя загрузить историю чата до момента, когда бот её получил через webhook/polling.</li>
        <li>Нельзя видеть список «всех личных чатов» — только тех, кто написал боту.</li>
        <li>Менять аватарку бота можно только через @BotFather.</li>
      </ul>
    </div>
  </div>`;
  $("#logout").addEventListener("click", () => { document.cookie = "token=; path=/; max-age=0"; location.reload(); });
  $("#pwa-prompt").addEventListener("click", () => {
    if (window._deferredPrompt) window._deferredPrompt.prompt();
    else toast("Браузер не предлагает установку (или уже установлено)", "ok");
  });
}

// ---------- main ----------
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); window._deferredPrompt = e; });

document.addEventListener("DOMContentLoaded", async () => {
  // Try token from URL or cookie
  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token");
  const cookieToken = (document.cookie.split("; ").find(x => x.startsWith("token=")) || "").slice(6);
  let token = urlToken || cookieToken;
  if (token) {
    if (await tryAuth(token)) {
      state.token = token;
      bootstrap();
      return;
    }
  }
  showAuth();
});

$("#auth-go").addEventListener("click", async () => {
  const t = $("#auth-token").value.trim();
  if (await tryAuth(t)) { state.token = t; bootstrap(); }
});
$("#auth-token").addEventListener("keydown", e => { if (e.key === "Enter") $("#auth-go").click(); });

// PWA service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
</script>
</body>
</html>
"""
SERVICE_WORKER_JS = """/* TG Studio service worker — minimal offline shell */
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
MINI_APP_WRAPPER = """<!doctype html>
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
