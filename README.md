# 📄 File Promo-Link Editor Bot

A Telegram bot that cleans **promo links out of text and code files**.

Send it a `.txt`, `.py`, `.js`, `.json`, `.html`, … file and it will:

1. **Find every link** in the content — `https://…`, `http://…`, `t.me/…` (incl. `telegram.me`/`telegram.dog`), and `@usernames`
2. **Show them as inline toggle buttons** — tap to select the ones you want to edit
3. **Remove or Replace** them — with your own promo text
4. **Send the edited file back** with the same filename

> ❌ No zips, videos, photos or binaries — **text/code files only** (≤ ~18 MB).

## ✨ Features

- 🔍 Detects `https://` / `http://` URLs, bare `t.me/...` links, and Telegram-style `@handles` (5–32 chars; emails excluded)
- ✅ Per-link selection with inline buttons — untap false positives (e.g. Python decorators like `@staticmethod`) before applying
- ✂️ **Link-only mode** — remove/replace just the link string, keep the rest of the line
- 🧹 **Whole-line mode** — drop (or swap) the entire promo line, e.g. `# Join @fake_channel for more`
- ✏️ Replace with any custom text (multi-line OK)
- 📊 Result summary showing exactly what was edited and how many times
- 🔤 Encoding-safe (UTF-8, with byte-preserving fallback) and CRLF-preserving
- 💚 Built-in **`/health`** endpoint (returns `200 ok`) for UptimeRobot / Docker healthchecks
- 🐳 **Dockerfile** included — no paid blueprints needed
- 🚫 **No database, no API keys** — just the bot token

## 🐳 Deploy with Docker (recommended)

```bash
# 1. Build
docker build -t file-editor-bot .

# 2. Run (map host port if you want external /health pings)
docker run -d \
  --name file-editor-bot \
  -e BOT_TOKEN="123456:ABC-your-token" \
  -e PORT=8080 \
  -p 8080:8080 \
  --restart unless-stopped \
  file-editor-bot
```

### UptimeRobot / health probe

Point any uptime monitor at:

| URL | Expected |
|---|---|
| `http://YOUR_HOST:8080/health` | **HTTP 200** · body `ok` |
| `http://YOUR_HOST:8080/` | **HTTP 200** · body `ok` |

Docker’s own `HEALTHCHECK` already hits `/health` every 30s.

### docker-compose (optional)

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      PORT: "8080"
    ports:
      - "8080:8080"
```

```bash
export BOT_TOKEN="123456:ABC-your-token"
docker compose up -d --build
```

## 💻 Run locally (no Docker)

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:ABC-your-token"
export PORT=8080   # optional, default 8080
python bot.py
# then: curl http://127.0.0.1:8080/health  →  ok
```

## 🕹️ Usage

```
/start          → how it works
send a file     → list of detected links as buttons
tap links       → select / deselect  (🟢 = on, ⚪ = off)
[Mode]          → switch "link only" ✂️  ↔  "whole line" 🧹
[🗑 Remove]     → delete selected
[✏️ Replace]    → bot asks for replacement text (or /skip to remove)
/cancel         → abort anytime
```

## ⚙️ How link detection works

| Pattern | Example |
|---|---|
| `https?://…` | `https://example.com/promo`, `http://t.me/abc` |
| bare `t.me/…` | `t.me/SomeChannel`, `t.me/joinchat/AbC123` |
| `@handle` | `@promo_channel` (5–32 chars, letter first; emails ignored) |

Trailing punctuation (`), . !` …) is stripped from URLs; duplicate links are merged.
Selected edits apply to **every occurrence** in the file.

## 📁 Project layout

```
bot.py            # the whole bot (single file)
Dockerfile        # production image + HEALTHCHECK on /health
.dockerignore     # keep the image lean
requirements.txt  # python-telegram-bot
render.yaml       # optional Render config (if you still use it)
```

## License

MIT — see [LICENSE](LICENSE).
