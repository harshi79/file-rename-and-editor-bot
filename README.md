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
- 🚫 **No database, no API keys** — just the bot token

## 🚀 Deploy on Render (free)

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token
2. Push this repo to GitHub, then in Render: **New → Web Service** → connect the repo
   (or use the blueprint: **New → Blueprint** and pick this repo — `render.yaml` is included)
3. Add the environment variable:

   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | `123456:ABC-your-token` |

4. Deploy — build `pip install -r requirements.txt`, start `python bot.py`. Done ✅

The bot also runs a tiny health-check server on `$PORT` so it works as a Render **free web service**.

## 💻 Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:ABC-your-token"
python bot.py
```

## 🕹️ Usage

```
/start          → how it works
send a file     → list of detected links as buttons
tap links       → select / deselect
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
requirements.txt  # python-telegram-bot
render.yaml       # Render blueprint
```

## License

MIT — see [LICENSE](LICENSE).
