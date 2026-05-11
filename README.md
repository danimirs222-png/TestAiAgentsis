# TG Studio — Telegram Bot Manager + Messenger UI

Single-file Python application (`app.py`) that combines:

- A **python-telegram-bot 20.7** bot that captures every update it sees and stores
  it in a local SQLite database.
- A built-in **aiohttp** web server that serves a Telegram-style **PWA / app-like
  messenger UI** straight from the same `app.py` (HTML / CSS / JS are embedded
  inside the file).
- A **WebSocket** channel for real-time push (incoming messages, edits, reactions,
  member updates) so the UI feels instant.
- **Per-chat auto-moderation** (banwords, antiflood, anticaps, antilinks, optional
  AI checker through OpenRouter) with online toggles and live preview.
- An **AI Assistant** tab that talks to any of the OpenRouter models you provide
  (text, image-gen, embeddings, rerank — model registry is configurable).
- **Bot profile editor** (name / description / short description / commands /
  default chat menu) using Bot API.
- **Offline mini-apps** sandbox — write small HTML/JS apps that run in an isolated
  iframe with their own `localStorage`.
- App-like gestures: smooth **scroll**, **multitouch zoom** on images, **swipe-to-reply**,
  long-press **context menu**, **drag-and-drop** files into the composer, native
  **text selection / copy**, pull-to-refresh.

> ⚠️ **Bot API limits — read this**
>
> Telegram Bot API does **not** let bots see chats they were never added to,
> read history from before the bot joined, or be notified about messages in
> a chat without being explicitly addressed (unless privacy mode is disabled
> and the bot is admin). TG Studio shows everything the bot *can* legally see
> and stores it locally from the moment you start running it. There is **no
> way** to retroactively pull "all groups the bot is in" or "every DM" through
> the Bot API — that is a Telegram restriction, not a TG Studio limitation.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and put your real bot token + OpenRouter keys

python app.py
```

Open <http://localhost:8080> in your browser (mobile-style UI scales to phone
sizes too — try the device toolbar in DevTools, or just open the same URL on
your phone over LAN).

The first visit asks for the `WEB_ACCESS_TOKEN` from `.env` (so random people
on your LAN can't drive your bot).

## What is stored locally

`data/tgstudio.sqlite3` holds:

- every Telegram update the bot has seen (messages, edits, reactions, polls,
  member changes, callbacks),
- bot profile cache,
- per-chat moderation settings,
- mini-app source code,
- AI assistant conversations.

Downloaded photos / stickers / files are cached in `files/`.

Delete the `data/` and `files/` folders to reset state.

## Security

**Never commit `.env`.** It is already in `.gitignore`. The bot token and all
OpenRouter keys you paste into `.env` are loaded by `app.py` at startup; they
are never echoed back to the UI in plaintext, never written to logs, and never
sent over the WebSocket.

If a token has ever been pasted into chat with anyone (including AI assistants),
rotate it: open [@BotFather](https://t.me/BotFather) → `/mybots` → choose the
bot → `API Token` → `Revoke current token`. Same for OpenRouter keys — log in
to <https://openrouter.ai/keys> and rotate any exposed key.
