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

Deploy: Docker (or any host). Tiny /health HTTP server binds $PORT for
UptimeRobot / orchestrator probes.
"""

import io
import logging
import os
import re
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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
MAX_LINK_LABEL = 42         # button text display length
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


def _link_icon(link: str) -> str:
    """Pick a small icon based on link type (display only)."""
    low = link.lower()
    if low.startswith("@"):
        return "👤"
    if "t.me/" in low or "telegram.me/" in low or "telegram.dog/" in low:
        return "✈️"
    if low.startswith("https://"):
        return "🔗"
    if low.startswith("http://"):
        return "🌐"
    return "🔗"


def _div() -> str:
    return "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def build_keyboard(s: dict, awaiting: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if awaiting:
        return InlineKeyboardMarkup(rows)

    selected = s["selected"]
    links = s["links"][:MAX_LINKS_SHOWN]

    # Link toggles — one per row so long URLs stay readable
    for i, link in enumerate(links):
        on = i in selected
        mark = "🟢" if on else "⚪"
        icon = _link_icon(link)
        label = f"{mark} {icon} {_short(link)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t{i}")])

    n_sel = len(selected)
    n_tot = len(links)

    # Selection helpers
    rows.append([
        InlineKeyboardButton("☑️ Select all", callback_data="sa"),
        InlineKeyboardButton("⬜ Clear", callback_data="sn"),
    ])

    # Mode toggle — shows the *current* mode clearly
    if s["mode"] == "line":
        mode_btn = InlineKeyboardButton(
            "🧹 Mode · whole line  (tap → link only)",
            callback_data="m",
        )
    else:
        mode_btn = InlineKeyboardButton(
            "✂️ Mode · link only  (tap → whole line)",
            callback_data="m",
        )
    rows.append([mode_btn])

    # Primary actions
    rm_label = f"🗑 Remove ({n_sel})" if n_sel else "🗑 Remove"
    rp_label = f"✏️ Replace ({n_sel})" if n_sel else "✏️ Replace"
    rows.append([
        InlineKeyboardButton(rm_label, callback_data="rm"),
        InlineKeyboardButton(rp_label, callback_data="rp"),
    ])

    # Footer
    rows.append([
        InlineKeyboardButton(f"📎 {n_sel}/{n_tot} selected", callback_data="sa"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])

    return InlineKeyboardMarkup(rows)


def build_header(s: dict) -> str:
    shown = s["links"][:MAX_LINKS_SHOWN]
    extra = len(s["links"]) - len(shown)
    n_sel = len(s["selected"])
    mode_name = "whole line 🧹" if s["mode"] == "line" else "link only ✂️"

    lines = [
        "╭─────────────────────╮",
        "│  📄  <b>Link Editor</b>        │",
        "╰─────────────────────╯",
        "",
        f"📁 <b>{escape(s['file_name'])}</b>",
        f"🔍 <b>{len(s['links'])}</b> unique · <b>{s['total']}</b> total hits",
        f"✅ Selected · <b>{n_sel}</b>",
        f"⚙️ Mode · <b>{mode_name}</b>",
        "",
        _div(),
        "<i>Tap a link to toggle · green = selected</i>",
        "<i>Untap false positives (e.g. <code>@staticmethod</code>)</i>",
    ]
    if extra > 0:
        lines.append("")
        lines.append(
            f"⚠️ Showing first <b>{MAX_LINKS_SHOWN}</b> of "
            f"<b>{len(s['links'])}</b> links."
        )
    return "\n".join(lines)


def build_summary(s: dict, stats: dict[str, int], action: str, replacement: str) -> str:
    verb = "replaced" if action == "replace" else "removed"
    unit = "line(s)" if s["mode"] == "line" else "link(s)"
    n = sum(stats.values())
    mode_name = "whole line" if s["mode"] == "line" else "link only"

    out = [
        "✨ <b>Done!</b>",
        "",
        f"📁 <b>{escape(s['file_name'])}</b>",
        f"✅ <b>{n}</b> {unit} {verb}",
        f"⚙️ Mode · <i>{mode_name}</i>",
    ]
    if action == "replace":
        out.append(f"✏️ With · <code>{escape(_short(replacement))}</code>")

    out.append("")
    out.append(_div())
    out.append("<b>Changes</b>")

    for tok, cnt in list(stats.items())[:15]:
        icon = _link_icon(tok)
        suffix = f"  ×{cnt}" if cnt > 1 else ""
        out.append(f"{icon} <code>{escape(_short(tok))}</code>{suffix}")
    if len(stats) > 15:
        out.append(f"… and <b>{len(stats) - 15}</b> more")

    out.append("")
    out.append("<i>Send another file anytime.</i>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Handlers                                                                     #
# --------------------------------------------------------------------------- #

WELCOME = (
    "╭─────────────────────╮\n"
    "│  👋  <b>File Link Editor</b>  │\n"
    "╰─────────────────────╯\n"
    "\n"
    "Drop a <b>text / code file</b> and I’ll clean the promo junk out of it.\n"
    "\n"
    f"{_div()}\n"
    "<b>What I catch</b>\n"
    "🔗  <code>https://…</code>  ·  <code>http://…</code>\n"
    "✈️  <code>t.me/…</code>  ·  telegram.me / .dog\n"
    "👤  <code>@usernames</code>\n"
    "\n"
    f"{_div()}\n"
    "<b>How it works</b>\n"
    "1️⃣  Send a file as a <b>document</b> 📎\n"
    "2️⃣  Tap the links you want to edit\n"
    "3️⃣  <b>Remove</b> them — or <b>Replace</b> with your text\n"
    "4️⃣  Get the edited file back, same name\n"
    "\n"
    f"{_div()}\n"
    "✂️  <b>link only</b> — edit just the URL / handle\n"
    "🧹  <b>whole line</b> — drop / swap the entire line\n"
    "\n"
    "❌  No zips, videos, photos or binaries\n"
    f"📏  Max size ~{MAX_FILE_MB} MB\n"
    "\n"
    "<i>Commands · /help  ·  /cancel  ·  /skip</i>"
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode="HTML")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skip while awaiting a replacement → just remove instead."""
    chat_id = update.effective_chat.id
    s = get_session(chat_id)
    if not s or s["awaiting"] != "replace" or s["user_id"] != update.effective_user.id:
        await update.effective_message.reply_text(
            "🤷 Nothing to skip right now.\n"
            "<i>Send a file to get started.</i>",
            parse_mode="HTML",
        )
        return
    s["awaiting"] = None
    await _apply(update, context, s, "remove")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SESSIONS.pop(update.effective_chat.id, None)
    await update.effective_message.reply_text(
        "❌ <b>Cancelled.</b>\n"
        "<i>Send a file anytime to start over.</i>",
        parse_mode="HTML",
    )


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    doc = msg.document
    chat_id = update.effective_chat.id

    if not is_text_file(doc.file_name or "", doc.mime_type):
        await msg.reply_text(
            "🚫 <b>Unsupported file</b>\n\n"
            "I only handle <b>text / code</b> files:\n"
            "<code>.txt  .py  .js  .json  .html  .css  .md</code> …\n\n"
            "No zips, videos, photos or binaries.",
            parse_mode="HTML",
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(
            f"📦 <b>File too big</b>\n\n"
            f"Limit is ~<b>{MAX_FILE_MB} MB</b>. Try a smaller file.",
            parse_mode="HTML",
        )
        return

    status = await msg.reply_text(
        "⏳ <b>Downloading & scanning…</b>\n"
        "<i>hang tight</i>",
        parse_mode="HTML",
    )

    buf = io.BytesIO()
    tg_file = await doc.get_file()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()

    if _is_binary(data):
        await status.edit_text(
            "🚫 <b>Binary file detected</b>\n\n"
            "That doesn’t look like text/code.",
            parse_mode="HTML",
        )
        return

    content, enc = decode_bytes(data)
    links, total = find_links(content)

    if not links:
        await status.edit_text(
            "🎉 <b>All clean!</b>\n\n"
            f"📁 <code>{escape(doc.file_name or 'file')}</code>\n"
            "No promo links found in this file.",
            parse_mode="HTML",
        )
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
            await msg.reply_text(
                "⚠️ Replacement can’t be empty.\n"
                "Send some text, or /skip to remove instead.",
                parse_mode="HTML",
            )
            return
        s["awaiting"] = None
        await msg.reply_text(
            "⏳ <b>Editing file…</b>",
            parse_mode="HTML",
        )
        await _apply(update, context, s, "replace", replacement)
        return

    await msg.reply_text(
        "📎 Send me a <b>text/code file</b> as a <i>document</i>\n"
        "and I’ll find its links.\n\n"
        "<i>Tip · use the paperclip → File, not photo.</i>",
        parse_mode="HTML",
    )


async def on_media(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🚫 <b>Media not supported</b>\n\n"
        "I only work with text/code <b>files</b> 📄 —\n"
        "not photos, videos, audio, stickers or other media.",
        parse_mode="HTML",
    )


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE, s: dict,
                 action: str, replacement: str = "") -> None:
    chat_id = update.effective_chat.id
    selected = {s["links"][i] for i in s["selected"]}
    new_text, stats = apply_edits(s["content"], selected, action, s["mode"], replacement)

    if not stats:
        await context.bot.send_message(
            chat_id,
            "🤔 <b>Nothing matched</b>\n\n"
            "Tap at least one link first, then try again.",
            parse_mode="HTML",
        )
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
        await q.answer("🙂 This isn’t your session — send your own file.")
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
        await q.answer(f"✅ {n} selected" if n else "Selection cleared")

    elif data == "m":
        s["mode"] = "line" if s["mode"] == "link" else "link"
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(
            f"Mode → {'whole line 🧹' if s['mode'] == 'line' else 'link only ✂️'}"
        )

    elif data == "sa":
        s["selected"] = set(range(len(s["links"][:MAX_LINKS_SHOWN])))
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(f"✅ All {len(s['selected'])} selected")

    elif data == "sn":
        s["selected"] = set()
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer("⬜ Selection cleared")

    elif data == "rm":
        if not s["selected"]:
            await q.answer("Tap at least one link first ⬆️", show_alert=True)
            return
        await q.answer("🗑 Removing…")
        await q.edit_message_text(
            "⏳ <b>Editing file…</b>\n"
            f"<i>removing {len(s['selected'])} item(s)</i>",
            parse_mode="HTML",
        )
        await _apply(update, context, s, "remove")

    elif data == "rp":
        if not s["selected"]:
            await q.answer("Tap at least one link first ⬆️", show_alert=True)
            return
        s["awaiting"] = "replace"
        await q.answer()
        n = len(s["selected"])
        await q.edit_message_text(
            "╭─────────────────────╮\n"
            "│  ✏️  <b>Replace</b>              │\n"
            "╰─────────────────────╯\n"
            "\n"
            f"📁 <b>{escape(s['file_name'])}</b>\n"
            f"✅ <b>{n}</b> link(s) selected\n"
            "\n"
            f"{_div()}\n"
            "Send the <b>replacement text</b> now.\n"
            "\n"
            "• /skip — remove instead\n"
            "• /cancel — abort\n",
            parse_mode="HTML",
        )

    elif data == "cx":
        SESSIONS.pop(chat_id, None)
        await q.answer("Cancelled")
        try:
            await q.edit_message_text(
                "❌ <b>Cancelled.</b>\n"
                "<i>Send a file anytime to start over.</i>",
                parse_mode="HTML",
            )
        except Exception:
            try:
                await q.delete_message()
            except Exception:
                pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Unhandled error: %s", context.error)


# --------------------------------------------------------------------------- #
# Health-check server (Docker / UptimeRobot / Render bind $PORT)               #
# --------------------------------------------------------------------------- #


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server for uptime probes.

    - GET /health  → 200  body "ok"   (preferred for UptimeRobot)
    - GET /        → 200  body "ok"
    - anything else → 404
    """

    def _reply(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            self._reply(200, b"ok")
        else:
            self._reply(404, b"not found")

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args) -> None:  # silence request logs
        pass


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Health server listening on 0.0.0.0:%s  →  GET /health", port)


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
