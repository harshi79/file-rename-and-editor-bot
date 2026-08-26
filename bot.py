#!/usr/bin/env python3
"""
📄 Telegram File Promo-Link Editor Bot
======================================
Send the bot a text/code file (.txt, .py, .js, .json, ...) and it will:

  1. Scan the content for every promo-ish thing:
       - https://... and http://... links
       - t.me / telegram.me / telegram.dog links (with or without scheme)
       - @usernames (Telegram-style handles, 5-32 chars)
  2. Show them as inline toggle buttons — tap to select which ones to edit
  3. Remove or Replace the selected ones (link-only or whole-line mode)
  4. Send the edited file back with the same name

Config: only BOT_TOKEN (env var). No database needed.

Deploy: Render web service (a tiny health-check HTTP server binds $PORT).
"""

import io
import logging
import os
import re
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

MAX_FILE_MB = 18            # Bot API download limit is 20 MB; stay under it
MAX_LINKS_SHOWN = 40        # inline keyboards cap at ~100 buttons
MAX_LINK_LABEL = 56         # button text display length
SESSION_TTL = 15 * 60       # seconds before an idle session expires

# Text / code extensions we accept (anything else is politely rejected)
TEXT_EXTS = {
    ".txt", ".text", ".log", ".csv", ".tsv", ".md", ".rst",
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".json", ".xml", ".html", ".htm", ".xhtml", ".css", ".scss", ".sass", ".less",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".kt", ".kts", ".rs", ".go",
    ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".m", ".swift", ".dart",
    ".sql", ".graphql", ".vue", ".svelte",
    ".svg", ".srt", ".vtt", ".ass", ".ssa", ".lrc",
    ".gitignore", ".dockerfile", ".editorconfig",
}

# Also accept by MIME type even if the extension is unknown
TEXT_MIMES = {
    "text/plain", "text/html", "text/css", "text/javascript", "text/x-python",
    "text/markdown", "text/csv", "text/xml", "text/x-sh", "application/json",
    "application/javascript", "application/x-javascript", "application/xml",
    "application/yaml", "application/x-yaml", "application/x-sh",
    "application/xhtml+xml",
}

# --------------------------------------------------------------------------- #
# Link detection                                                               #
# --------------------------------------------------------------------------- #

# explicit http(s) URLs
HTTP_RE = re.compile(r"https?://[^\s<>\"']+")
# bare t.me / telegram.me / telegram.dog links (no scheme) — the lookbehind
# avoids re-matching the part of an https://t.me/... URL
TME_RE = re.compile(
    r"(?<![\w/.@])(?:(?:www\.)?(?:t\.me|telegram\.(?:me|dog)))/[^\s<>\"']+"
)
# @handles: 5-32 chars, starts with a letter, not part of an email
# (code decorators like @staticmethod can still match — user untaps those)
HANDLE_RE = re.compile(r"(?<![\w@.])@[A-Za-z][A-Za-z0-9_]{4,31}")

TRAILING_PUNCT = ".,;:!?)]}>'\"`»«"


def _trim_token(tok: str) -> str:
    """Strip trailing punctuation captured by the URL regexes (e.g. 'x),' → 'x')."""
    while tok and tok[-1] in TRAILING_PUNCT:
        tok = tok[:-1]
    return tok


def find_links(text: str) -> tuple[list[str], int]:
    """Return (unique links in order of first appearance, total occurrences)."""
    seen: dict[str, None] = {}
    total = 0
    for regex in (HTTP_RE, TME_RE, HANDLE_RE):
        for m in regex.finditer(text):
            tok = _trim_token(m.group(0))
            if len(tok) < 4:  # noise
                continue
            total += 1
            seen.setdefault(tok, None)
    return list(seen), total


def _line_spans(line: str) -> list[tuple[int, int, str]]:
    """All link (start, end, token) spans in one line, overlaps resolved."""
    spans: list[tuple[int, int, str]] = []
    for regex in (HTTP_RE, TME_RE, HANDLE_RE):
        for m in regex.finditer(line):
            tok = _trim_token(m.group(0))
            if len(tok) < 4:
                continue
            spans.append((m.start(), m.start() + len(tok), tok))
    spans.sort()
    out, last_end = [], -1
    for s, e, tok in spans:  # drop overlapping matches (keep left-most)
        if s >= last_end:
            out.append((s, e, tok))
            last_end = e
    return out


def apply_edits(
    text: str,
    selected: set[str],
    action: str,          # "remove" | "replace"
    mode: str,            # "link" | "line"
    replacement: str = "",
) -> tuple[str, dict[str, int]]:
    """Apply the edit to `text`. Returns (new_text, {link: times_edited})."""
    stats: dict[str, int] = {}
    result_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        spans = _line_spans(body)
        hits = [(s, e, tok) for s, e, tok in spans if tok in selected]
        if not hits:
            result_lines.append(line)
            continue

        if mode == "line":
            # whole-line: the line itself is promo → drop it or replace it
            for _, _, tok in hits:
                stats[tok] = stats.get(tok, 0) + 1
            if action == "replace":
                result_lines.append(replacement + ending)
            continue

        # link-only: edit each span, keep the rest of the line
        for s, e, tok in sorted(hits, reverse=True):
            stats[tok] = stats.get(tok, 0) + 1
            body = body[:s] + (replacement if action == "replace" else "") + body[e:]
        if action == "remove":
            body = re.sub(r" {2,}", " ", body).rstrip()
            if not body:  # line became empty → drop it entirely
                continue
        result_lines.append(body + ending)

    return "".join(result_lines), stats


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode file bytes; returns (text, encoding). latin-1 round-trips anything."""
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def is_text_file(name: str, mime: str | None) -> bool:
    lower = name.lower()
    base = os.path.basename(lower)
    if any(base == ext or base.endswith(ext) for ext in TEXT_EXTS):
        return True
    return (mime or "").split(";")[0].strip().lower() in TEXT_MIMES


# --------------------------------------------------------------------------- #
# Sessions (in-memory, no DB)                                                  #
# --------------------------------------------------------------------------- #

# chat_id -> session
SESSIONS: dict[int, dict] = {}


def _now() -> float:
    return time.time()


def get_session(chat_id: int) -> dict | None:
    s = SESSIONS.get(chat_id)
    if not s:
        return None
    if _now() - s["ts"] > SESSION_TTL:
        SESSIONS.pop(chat_id, None)
        return None
    return s


def new_session(chat_id: int, user_id: int, name: str, content: str, enc: str,
                links: list[str], total: int) -> dict:
    s = {
        "user_id": user_id,
        "file_name": name,
        "content": content,
        "encoding": enc,
        "links": links,
        "total": total,
        "selected": set(),
        "mode": "link",            # "link" | "line"
        "awaiting": None,          # None | "replace"
        "ts": _now(),
    }
    SESSIONS[chat_id] = s
    return s


# --------------------------------------------------------------------------- #
# UI helpers                                                                   #
# --------------------------------------------------------------------------- #


def _short(link: str) -> str:
    return link if len(link) <= MAX_LINK_LABEL else link[: MAX_LINK_LABEL - 1] + "…"


def build_keyboard(s: dict, awaiting: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not awaiting:
        for i, link in enumerate(s["links"][:MAX_LINKS_SHOWN]):
            mark = "✅" if i in s["selected"] else "⬜"
            rows.append([InlineKeyboardButton(f"{mark} {_short(link)}",
                                              callback_data=f"t{i}")])
        mode_label = "whole line 🧹" if s["mode"] == "line" else "link only ✂️"
        rows.append([InlineKeyboardButton(f"Mode: {mode_label}", callback_data="m")])
        rows.append([
            InlineKeyboardButton("🗑 Remove", callback_data="rm"),
            InlineKeyboardButton("✏️ Replace", callback_data="rp"),
        ])
        rows.append([
            InlineKeyboardButton("☑️ All", callback_data="sa"),
            InlineKeyboardButton("⬜ None", callback_data="sn"),
            InlineKeyboardButton("✖️ Cancel", callback_data="cx"),
        ])
    return InlineKeyboardMarkup(rows)


def build_header(s: dict) -> str:
    shown = s["links"][:MAX_LINKS_SHOWN]
    extra = len(s["links"]) - len(shown)
    lines = [
        f"📄 <b>{escape(s['file_name'])}</b>",
        f"🔍 Found <b>{len(s['links'])}</b> unique link(s), {s['total']} occurrence(s).",
        "",
        "Tap a link to select it (tap again to untap — e.g. code decorators "
        "like <code>@staticmethod</code> aren't Telegram handles), then choose "
        "an action below.",
    ]
    if extra > 0:
        lines.append(f"⚠️ Showing first {MAX_LINKS_SHOWN} of {len(s['links'])}.")
    lines.append(f"⚙️ Mode: <b>{'whole line' if s['mode'] == 'line' else 'link only'}</b>")
    return "\n".join(lines)


def build_summary(s: dict, stats: dict[str, int], action: str, replacement: str) -> str:
    verb = "replaced" if action == "replace" else "removed"
    unit = "line(s)" if s["mode"] == "line" else "link(s)"
    n = sum(stats.values())
    out = [f"✅ <b>{escape(s['file_name'])}</b> — {n} {unit} {verb}."]
    if action == "replace":
        out.append(f"✏️ Replacement: <code>{escape(_short(replacement))}</code>")
    out.append("")
    for tok, cnt in list(stats.items())[:15]:
        suffix = f" (×{cnt})" if cnt > 1 else ""
        out.append(f"• <code>{escape(_short(tok))}</code>{suffix}")
    if len(stats) > 15:
        out.append(f"• … and {len(stats) - 15} more")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Handlers                                                                     #
# --------------------------------------------------------------------------- #

WELCOME = (
    "👋 <b>File Promo-Link Editor</b>\n\n"
    "Send me a <b>text or code file</b> (e.g. <code>.txt</code>, <code>.py</code>, "
    "<code>.js</code>, <code>.json</code>) and I'll:\n\n"
    "1️⃣ Find every link — <code>https://…</code>, <code>t.me/…</code>, <code>@username</code>\n"
    "2️⃣ Show them as buttons — you tap the ones to edit\n"
    "3️⃣ Remove them, or replace with your own promo text\n"
    "4️⃣ Send the edited file back\n\n"
    "❌ No zips, videos, photos or binaries — text/code files only.\n"
    "📏 Max size ~18 MB."
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode="HTML")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skip while awaiting a replacement → just remove instead."""
    chat_id = update.effective_chat.id
    s = get_session(chat_id)
    if not s or s["awaiting"] != "replace" or s["user_id"] != update.effective_user.id:
        await update.effective_message.reply_text("Nothing to skip 🙂")
        return
    s["awaiting"] = None
    await _apply(update, context, s, "remove")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SESSIONS.pop(update.effective_chat.id, None)
    await update.effective_message.reply_text("✖️ Cancelled. Send a file any time.")


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    doc = msg.document
    chat_id = update.effective_chat.id

    if not is_text_file(doc.file_name or "", doc.mime_type):
        await msg.reply_text(
            "❌ I only handle <b>text / code files</b> (like <code>.txt .py .js .json "
            ".html .css .md</code>…).\nNo zips, videos, photos or binaries.",
            parse_mode="HTML",
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ File too big — my limit is ~{MAX_FILE_MB} MB.")
        return

    status = await msg.reply_text("⏳ Downloading and scanning…")

    buf = io.BytesIO()
    tg_file = await doc.get_file()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()

    if _is_binary(data):
        await status.edit_text("❌ That looks like a binary file, not text/code.")
        return

    content, enc = decode_bytes(data)
    links, total = find_links(content)

    if not links:
        await status.edit_text("🎉 No links found in this file — it's already clean!")
        SESSIONS.pop(chat_id, None)
        return

    s = new_session(chat_id, update.effective_user.id, doc.file_name or "file.txt",
                    content, enc, links, total)
    await status.edit_text(build_header(s), parse_mode="HTML",
                           reply_markup=build_keyboard(s))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text messages: either the replacement text, or a gentle hint."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    s = get_session(chat_id)

    if s and s["awaiting"] == "replace" and s["user_id"] == update.effective_user.id:
        replacement = msg.text.strip()[:200]
        if not replacement:
            await msg.reply_text("Replacement can't be empty. Send text, or /skip.")
            return
        s["awaiting"] = None
        await msg.reply_text("⏳ Editing file…")
        await _apply(update, context, s, "replace", replacement)
        return

    await msg.reply_text(
        "Send me a <b>text/code file</b> as a <i>document</i> 📎 and I'll find its links.",
        parse_mode="HTML",
    )


async def on_media(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "❌ I only work with text/code files 📄 — not photos, videos, audio, "
        "stickers or other media."
    )


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE, s: dict,
                 action: str, replacement: str = "") -> None:
    chat_id = update.effective_chat.id
    selected = {s["links"][i] for i in s["selected"]}
    new_text, stats = apply_edits(s["content"], selected, action, s["mode"], replacement)

    if not stats:
        await context.bot.send_message(
            chat_id, "🤔 Nothing matched — tap at least one link first.")
        s["awaiting"] = None
        return

    data = new_text.encode(s["encoding"])
    caption = build_summary(s, stats, action, replacement)
    if len(caption) > 1000:
        caption = caption[:1000] + "…"
    await context.bot.send_document(
        chat_id,
        document=InputFile(io.BytesIO(data), filename=s["file_name"]),
        caption=caption,
        parse_mode="HTML",
    )
    SESSIONS.pop(chat_id, None)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    data = q.data or ""
    s = get_session(chat_id)

    if not s:
        await q.answer("⌛ Session expired — send the file again.", show_alert=True)
        return
    if q.from_user.id != s["user_id"]:
        await q.answer("🙂 This isn't your session — send your own file.")
        return

    s["ts"] = _now()

    if data.startswith("t"):
        i = int(data[1:])
        if i >= len(s["links"]):
            await q.answer("Link gone — send the file again.")
            return
        s["selected"].symmetric_difference_update({i})
        n = len(s["selected"])
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(f"Selected {n}" if n else "Nothing selected")

    elif data == "m":
        s["mode"] = "line" if s["mode"] == "link" else "link"
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(f"Mode: {'whole line' if s['mode'] == 'line' else 'link only'}")

    elif data == "sa":
        s["selected"] = set(range(len(s["links"][:MAX_LINKS_SHOWN])))
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer("All selected")

    elif data == "sn":
        s["selected"] = set()
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer("Selection cleared")

    elif data == "rm":
        if not s["selected"]:
            await q.answer("Tap at least one link first ⬆️")
            return
        await q.answer()
        await q.edit_message_text("⏳ Editing file…")
        await _apply(update, context, s, "remove")

    elif data == "rp":
        if not s["selected"]:
            await q.answer("Tap at least one link first ⬆️")
            return
        s["awaiting"] = "replace"
        await q.answer()
        await q.edit_message_text(
            f"✏️ <b>{escape(s['file_name'])}</b>\n\n"
            f"You selected <b>{len(s['selected'])}</b> link(s). "
            "Now send me the <b>replacement text</b> "
            "(or /skip to just remove them, /cancel to abort).",
            parse_mode="HTML",
        )

    elif data == "cx":
        SESSIONS.pop(chat_id, None)
        await q.answer("Cancelled")
        try:
            await q.delete_message()
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Unhandled error: %s", context.error)


# --------------------------------------------------------------------------- #
# Health-check server (Render web services must bind $PORT)                    #
# --------------------------------------------------------------------------- #


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"OK - file editor bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence request logs
        pass


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Health server listening on 0.0.0.0:%s", port)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("http.server").setLevel(logging.WARNING)

    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set.\n"
            "Get a token from @BotFather on Telegram and set the BOT_TOKEN "
            "environment variable."
        )

    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE
        | filters.VIDEO_NOTE | filters.ANIMATION
        # catch-all for stickers & anything else unexpected
        | ~filters.COMMAND & ~filters.Document.ALL & ~filters.TEXT
          & ~filters.StatusUpdate.ALL,
        on_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    logging.info("Bot starting — polling for updates…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
