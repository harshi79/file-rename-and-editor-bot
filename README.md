# ✦ File Studio — Link Editor + File Renamer

A Telegram bot with two tools:

**A) Link editor** — cleans **promo links out of text and code files**.
Send it a `.txt`, `.py`, `.js`, `.json`, `.html`, … file and it will:

1. **Find every link** in the content — `https://…`, `http://…`, `t.me/…` (incl. `telegram.me`/`telegram.dog`), and `@usernames`
2. **Show them as inline toggle buttons** — tap to select the ones you want to edit
3. **Remove or Replace** them — with your own promo text
4. **Send the edited file back** with the same filename

**B) File renamer** — rename any file by replying to it:

```
/rename newname            → keeps the original extension
/rename newname ext        → new name + new extension (dot optional)
/rename newname.txt        → name + extension in one token
/rename .md                → extension only, name stays the same
```

The command also works when typed in the file's caption instead of a reply.

> The UI uses a small-caps unicode font and text symbols (no emojis), and every
> menu panel ships with its own anime artwork from `assets/`.

## ✨ Features

- ⌂ **Photo menu on /start** — inline buttons for editor / renamer / help /
  about / commands / close; every sub-menu has a ◂ back button
- ✦ **File renamer** — reply to any document with `/rename …` (4 argument
  forms, caption supported, names sanitised, ≤ ~18 MB)
- ✎ **Link editor** — detects `https://` / `http://` URLs, bare `t.me/...`
  links, and Telegram-style `@handles` (5–32 chars; emails excluded)
- ◉ Per-link selection with inline buttons — untap false positives (e.g.
  Python decorators like `@staticmethod`) before applying
- ⌁ **Link-only mode** — remove/replace just the link string, keep the rest of the line
- ⌁ **Whole-line mode** — drop (or swap) the entire promo line, e.g. `# Join @fake_channel for more`
- ✎ Replace with any custom text (multi-line OK), `/skip` to remove instead
- ✓ Result summary showing exactly what was edited and how many times
- 🔤 Encoding-safe (UTF-8, with byte-preserving fallback) and CRLF-preserving
- 💚 Built-in **`/health`** endpoint (returns `200 ok`) for UptimeRobot / Docker healthchecks
- 🐳 **Dockerfile** included (artwork bundled) — no paid blueprints needed
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

Docker's own `HEALTHCHECK` already hits `/health` every 30s.

### Menu artwork hosting (optional)

By default the bot uploads each panel image from `assets/` once and then reuses
the cached Telegram `file_id`. To serve them from your own host instead, set:

```bash
-e ASSET_BASE_URL="https://cdn.example.com/bot-assets"   # + start.jpg, help.jpg, …
```

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
/start            → photo main menu (inline buttons, ◂ back everywhere)
/help             → help & guide panel
send a file       → list of detected links as buttons
tap links         → select / deselect  (◉ = on, ○ = off)
[mode]            → switch "link only" ⌁  ↔  "whole line" ⌁
[✕ remove]        → delete selected
[✎ replace]       → bot asks for replacement text (or /skip to remove)
reply /rename …   → rename the file you replied to (see forms above)
/cancel           → abort anytime
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
assets/           # anime artwork for the menu panels (start/help/editor/…)
Dockerfile        # production image (assets included) + HEALTHCHECK on /health
.dockerignore     # keep the image lean
requirements.txt  # python-telegram-bot
render.yaml       # optional Render config (if you still use it)
```

## License

MIT — see [LICENSE](LICENSE).
