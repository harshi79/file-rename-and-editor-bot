#!/usr/bin/env python3
"""
✦ Telegram File Studio — Link Editor + File Renamer
===================================================
Two tools in one bot:

  A) LINK EDITOR (original logic, unchanged behaviour)
     Send a text/code file (.txt, .py, .js, .json, ...) and it will:
       1. Scan the content for every promo-ish thing:
            - https://... and http://... links
            - t.me / telegram.me / telegram.dog links (with or without scheme)
            - @usernames (Telegram-style handles, 5-32 chars)
       2. Show them as inline toggle buttons — tap to select which ones to edit
       3. Remove or Replace the selected ones (link-only or whole-line mode)
       4. Send the edited file back with the same name

  B) FILE RENAMER (new, fully separate feature)
     Reply to any file (document) with:
       /rename newname           → keeps the original extension
       /rename newname ext       → new name + new extension (dot optional)
       /rename newname.txt       → name + extension in one token
       /rename .ext              → extension only, name stays the same
     The command also works inside the file's caption.

Menu: /start opens a photo menu with inline buttons (editor / rename / help /
about / commands / close) — every sub-menu has a back button. Panels use a
small-caps unicode font and text symbols (no emojis). Each panel ships with
its own anime artwork from ./assets (overridable via ASSET_BASE_URL).

Config: BOT_TOKEN (env var). Optional: PORT, ASSET_BASE_URL. No database.

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
from pathlib import Path
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    MessageEntity,
    Update,
)
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
CAPTION_MAX = 1024          # Telegram photo/document caption limit

# Anime artwork for the menu panels. Local files are uploaded once and then
# served from the cached Telegram file_id; set ASSET_BASE_URL to serve them
# from a public host instead (e.g. a CDN or raw file hosting).
ASSET_DIR = Path(__file__).resolve().parent / "assets"
ASSET_BASE_URL = os.environ.get("ASSET_BASE_URL", "").strip().rstrip("/")
PHOTO_CACHE: dict[str, str] = {}

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
# Small-caps unicode styling                                                   #
# --------------------------------------------------------------------------- #

_SC_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "ꜱ", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}

_TAG_RE = re.compile(r"<[^>]+>")


def sc_plain(text: str) -> str:
    """Small-caps a plain (non-HTML) string (upper & lower case)."""
    out = []
    for c in text:
        if "A" <= c <= "Z":
            out.append(_SC_MAP[c.lower()])
        else:
            out.append(_SC_MAP.get(c, c))
    return "".join(out)


def esc_sc(text: str) -> str:
    """Small-cap then HTML-escape a dynamic value (keeps entities valid)."""
    return escape(sc_plain(text))


def sc(html: str) -> str:
    """Small-cap an HTML template; tags stay intact and <code>…</code>
    content is left verbatim so links/commands stay readable."""
    out: list[str] = []
    pos = 0
    in_code = False
    for m in _TAG_RE.finditer(html):
        seg = html[pos:m.start()]
        out.append(seg if in_code else sc_plain(seg))
        out.append(html[m.start():m.end()])
        tag = m.group(0).lower()
        if tag.startswith("<code"):
            in_code = True
        elif tag.startswith("</code"):
            in_code = False
        pos = m.end()
    tail = html[pos:]
    out.append(tail if in_code else sc_plain(tail))
    return "".join(out)


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
# Rename parsing (fully separate from the editor)                              #
# --------------------------------------------------------------------------- #

_BAD_NAME_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]")
_EXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,15}$")


def _clean_stem(s: str) -> str:
    s = _BAD_NAME_CHARS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:120]


def _clean_ext(s: str) -> str:
    s = s.strip().lstrip(".").strip()
    s = re.sub(r"[^A-Za-z0-9+._-]", "", s).strip(".")
    return s[:16]


def parse_rename_target(raw: str, original: str) -> tuple[str | None, str]:
    """Turn `/rename` args + the original filename into a new filename.

    Accepted forms:
      ``newname``            → keep the original extension
      ``newname ext``        → new name + new extension (dot optional)
      ``newname.ext``        → name + extension in a single token
      ``.ext``               → extension only, original name kept

    Returns ``(new_filename, "")`` or ``(None, error_message)``.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "no name given"

    stem, dot_ext = os.path.splitext(original or "file")
    orig_ext = dot_ext.lstrip(".")

    # form: ".ext" → extension only
    if raw.startswith("."):
        ext = _clean_ext(raw)
        if not ext or not _EXT_RE.match(ext):
            return None, f"“{raw}” isn’t a valid extension"
        return f"{stem or 'file'}.{ext}", ""

    parts = raw.split(None, 1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if rest:
        # form: "newname ext"
        new_stem = _clean_stem(first)
        ext = _clean_ext(rest)
    elif "." in first and not first.endswith("."):
        # form: "newname.ext"
        new_stem, e2 = os.path.splitext(first)
        new_stem = _clean_stem(new_stem)
        ext = _clean_ext(e2)
    else:
        # form: "newname"
        new_stem = _clean_stem(first)
        ext = orig_ext

    if not new_stem:
        return None, "the new name is empty after cleaning"
    if ext and not _EXT_RE.match(ext):
        return None, f"“{ext}” isn’t a valid extension"

    new_name = f"{new_stem}.{ext}" if ext else new_stem
    if len(new_name) > 200:
        return None, "that name is too long (max 200 chars)"
    return new_name, ""


# --------------------------------------------------------------------------- #
# Sessions (in-memory, no DB) — link editor only                               #
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
# UI helpers — text symbols only (no emojis), small-caps font                  #
# --------------------------------------------------------------------------- #


def _short(link: str) -> str:
    return link if len(link) <= MAX_LINK_LABEL else link[: MAX_LINK_LABEL - 1] + "…"


def _link_icon(link: str) -> str:
    """Small text glyph per link type (display only)."""
    low = link.lower()
    if low.startswith("@"):
        return "✦"
    if "t.me/" in low or "telegram.me/" in low or "telegram.dog/" in low:
        return "➜"
    if low.startswith("https://"):
        return "❖"
    if low.startswith("http://"):
        return "◇"
    return "◆"


def _div() -> str:
    return "┄┄┄┄┄┄┄┄┄┄┄"


def _box(title: str) -> str:
    inner_w = 27
    t = f"  {title}  "
    if len(t) > inner_w:
        t = t[:inner_w]
    t += " " * (inner_w - len(t))
    bar = "─" * inner_w
    return f"╭{bar}╮\n│{t}│\n╰{bar}╯"


# --------------------------------------------------------------------------- #
# Photo panels (start / help / editor / rename / commands / about)             #
# --------------------------------------------------------------------------- #


def _nav_btn(label: str, target: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(sc_plain(label), callback_data=f"n:{target}")


def _kb_home_back(extra: list[list[InlineKeyboardButton]] | None = None):
    rows = list(extra or [])
    rows.append([_nav_btn("≡ ᴄᴏᴍᴍᴀɴᴅꜱ", "commands"), _nav_btn("◂ ʙᴀᴄᴋ", "start")])
    return InlineKeyboardMarkup(rows)


START_TEXT = sc(
    _box("⌂ file studio") + "\n"
    "\n"
    "hello ◡̈  i clean & rename your files.\n"
    "\n"
    "◆ <b>link editor</b> ▸ strip promo links from text & code files\n"
    "◆ <b>file renamer</b> ▸ rename any file by replying to it\n"
    "\n" + _div() + "\n"
    "◂ tap a menu below ◂"
)

START_KB = InlineKeyboardMarkup([
    [_nav_btn("✎ link editor", "editor"), _nav_btn("✦ renamer", "rename")],
    [_nav_btn("❖ help", "help"), _nav_btn("♥ about", "about")],
    [_nav_btn("≡ commands", "commands"), _nav_btn("✕ close", "close")],
])

HELP_TEXT = sc(
    _box("❖ help & guide") + "\n"
    "\n"
    "<b>link editor</b>\n"
    "1. send a text/code file as a document ▤\n"
    "2. tap links to select · ◉ = on · ○ = off\n"
    "3. <b>remove</b> them — or <b>replace</b> with your text\n"
    "4. get the edited file back, same name\n"
    "\n"
    "<b>file renamer</b>\n"
    "reply to any file with <code>/rename</code> + args —\n"
    "see the rename menu for examples.\n"
    "\n"
    "<b>what i catch</b>\n"
    "➜ <code>https://…</code> · <code>http://…</code>\n"
    "➜ <code>t.me/…</code> · telegram.me · telegram.dog\n"
    "➜ <code>@usernames</code>\n"
    "\n"
    "<b>modes</b>\n"
    "⌁ link only ▸ edit just the url / handle\n"
    "⌁ whole line ▸ drop / swap the full line\n"
    "\n" + _div() + "\n"
    f"△ no zips / videos / binaries · max ~{MAX_FILE_MB} mb"
)

HELP_KB = _kb_home_back([
    [_nav_btn("✎ editor guide", "editor"), _nav_btn("✦ rename guide", "rename")],
])

EDITOR_TEXT = sc(
    _box("✎ link editor") + "\n"
    "\n"
    "send a <b>text / code file</b> as a document ▤\n"
    "i list every link as a button — tap to toggle.\n"
    "\n"
    "◆ ◉ / ○ ▸ selected / not selected\n"
    "◆ ✕ remove ▸ delete the selected links\n"
    "◆ ✎ replace ▸ swap them for your own text\n"
    "  · /skip ▸ remove instead of replacing\n"
    "\n"
    "<b>modes</b>\n"
    "⌁ link only  touch only the url / handle\n"
    "⌁ whole line ▸ drop or swap the entire line\n"
    "\n" + _div() + "\n"
    "△ untap false positives like <code>@staticmethod</code>\n"
    "△ supported: text & code files only · max ~18 mb"
)

EDITOR_KB = _kb_home_back()

RENAME_TEXT = sc(
    _box("✦ file renamer") + "\n"
    "\n"
    "reply to any file (document) with:\n"
    "\n"
    "<code>/rename newname</code>\n"
    "▸ keeps the original extension\n"
    "\n"
    "<code>/rename newname ext</code>\n"
    "▸ new name + new extension (dot optional)\n"
    "\n"
    "<code>/rename newname.txt</code>\n"
    "▸ name + extension in one token\n"
    "\n"
    "<code>/rename .md</code>\n"
    "▸ extension only — name stays same\n"
    "\n" + _div() + "\n"
    "◆ you can also put the command in the file's caption\n"
    "◆ file size limit ~18 mb"
)

RENAME_KB = _kb_home_back()

COMMANDS_TEXT = sc(
    _box("≡ commands") + "\n"
    "\n"
    "<code>/start</code> ▸ open the main menu\n"
    "<code>/help</code> ▸ help & guide\n"
    "<code>/rename</code> ▸ rename the file you reply to\n"
    "<code>/skip</code> ▸ remove instead of replace\n"
    "<code>/cancel</code> ▸ abort the current edit\n"
    "\n" + _div() + "\n"
    "◆ you can also just send a file — no command needed"
)

COMMANDS_KB = _kb_home_back([[_nav_btn("❖ help", "help")]])

ABOUT_TEXT = sc(
    _box("♥ about") + "\n"
    "\n"
    "<b>file studio</b> · link editor + file renamer\n"
    "\n"
    "◆ no database — files are edited in memory\n"
    "◆ encoding-safe · crlf-preserving\n"
    "◆ open source · mit license\n"
    "\n" + _div() + "\n"
    "made with ♥ for clean files"
)

ABOUT_KB = _kb_home_back()

PANELS: dict[str, tuple[str, str, InlineKeyboardMarkup]] = {
    "start": ("start.jpg", START_TEXT, START_KB),
    "help": ("help.jpg", HELP_TEXT, HELP_KB),
    "editor": ("editor.jpg", EDITOR_TEXT, EDITOR_KB),
    "rename": ("rename.jpg", RENAME_TEXT, RENAME_KB),
    "commands": ("commands.jpg", COMMANDS_TEXT, COMMANDS_KB),
    "about": ("about.jpg", ABOUT_TEXT, ABOUT_KB),
}


def _photo_arg(asset: str):
    """URL / cached file_id / fresh upload for an asset image."""
    if ASSET_BASE_URL:
        return f"{ASSET_BASE_URL}/{asset}"
    if asset in PHOTO_CACHE:
        return PHOTO_CACHE[asset]
    path = ASSET_DIR / asset
    if path.exists():
        return InputFile(str(path))
    return None


def _cache_photo(asset: str, msg) -> None:
    try:
        if msg is not None and getattr(msg, "photo", None):
            PHOTO_CACHE[asset] = msg.photo[-1].file_id
    except Exception:
        pass


async def send_panel(bot, chat_id, panel: str, reply_to: int | None = None) -> None:
    asset, text, kb = PANELS[panel]
    photo = _photo_arg(asset)
    if photo is None:  # artwork missing → text-only fallback
        await bot.send_message(chat_id, text, parse_mode="HTML",
                               reply_markup=kb, reply_to_message_id=reply_to)
        return
    m = await bot.send_photo(chat_id, photo, caption=text, parse_mode="HTML",
                             reply_markup=kb, reply_to_message_id=reply_to)
    _cache_photo(asset, m)


async def edit_panel(q, panel: str) -> None:
    """Swap a menu message to another panel (photo → photo edit, with a
    send-new/delete-old fallback for non-photo messages)."""
    asset, text, kb = PANELS[panel]
    photo = _photo_arg(asset)
    if photo is not None:
        try:
            m = await q.edit_message_media(
                InputMediaPhoto(photo, caption=text, parse_mode="HTML"),
                reply_markup=kb,
            )
            _cache_photo(asset, m)
            return
        except Exception:
            pass
    chat_id = q.message.chat.id if q.message else None
    if chat_id is not None:
        await send_panel(q.get_bot(), chat_id, panel)
    try:
        await q.delete_message()
    except Exception:
        pass


def _split_caption(text: str, limit: int = CAPTION_MAX) -> list[str]:
    """Split long text into caption-sized chunks at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        piece = (cur + "\n\n" + para) if cur else para
        if len(piece) > limit and cur:
            chunks.append(cur)
            piece = para
        while len(piece) > limit:  # pathological single paragraph
            chunks.append(piece[:limit])
            piece = piece[limit:]
        cur = piece
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- #
# Link-editor UI (same logic as before, restyled)                              #
# --------------------------------------------------------------------------- #


def build_keyboard(s: dict, awaiting: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if awaiting:
        return InlineKeyboardMarkup(rows)

    selected = s["selected"]
    links = s["links"][:MAX_LINKS_SHOWN]

    # Link toggles — one per row so long URLs stay readable.
    # Link text itself is NOT small-capped so URLs stay copy-able.
    for i, link in enumerate(links):
        on = i in selected
        mark = "◉" if on else "○"
        icon = _link_icon(link)
        label = f"{mark} {icon} {_short(link)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t{i}")])

    n_sel = len(selected)
    n_tot = len(links)

    # Selection helpers
    rows.append([
        InlineKeyboardButton(sc_plain("◈ select all"), callback_data="sa"),
        InlineKeyboardButton(sc_plain("◇ clear"), callback_data="sn"),
    ])

    # Mode toggle — shows the *current* mode clearly
    if s["mode"] == "line":
        mode_btn = InlineKeyboardButton(
            sc_plain("⌁ mode · whole line  (tap → link only)"),
            callback_data="m",
        )
    else:
        mode_btn = InlineKeyboardButton(
            sc_plain("⌁ mode · link only  (tap → whole line)"),
            callback_data="m",
        )
    rows.append([mode_btn])

    # Primary actions
    rm_label = f"✕ remove ({n_sel})" if n_sel else "✕ remove"
    rp_label = f"✎ replace ({n_sel})" if n_sel else "✎ replace"
    rows.append([
        InlineKeyboardButton(sc_plain(rm_label), callback_data="rm"),
        InlineKeyboardButton(sc_plain(rp_label), callback_data="rp"),
    ])

    # Footer
    rows.append([
        InlineKeyboardButton(sc_plain(f"◉ {n_sel}/{n_tot} selected"), callback_data="sa"),
        InlineKeyboardButton(sc_plain("✕ cancel"), callback_data="cx"),
    ])

    return InlineKeyboardMarkup(rows)


def build_header(s: dict) -> str:
    shown = s["links"][:MAX_LINKS_SHOWN]
    extra = len(s["links"]) - len(shown)
    n_sel = len(s["selected"])
    mode_name = "whole line ⌁" if s["mode"] == "line" else "link only ⌁"

    lines = [
        _box("✎ link editor"),
        "",
        f"▤ <b>{esc_sc(s['file_name'])}</b>",
        f"◎ <b>{len(s['links'])}</b> unique · <b>{s['total']}</b> total hits",
        f"◉ selected · <b>{n_sel}</b>",
        f"❖ mode · <b>{mode_name}</b>",
        "",
        _div(),
        "<i>tap a link to toggle · ◉ = selected</i>",
        "<i>untap false positives (e.g. <code>@staticmethod</code>)</i>",
    ]
    if extra > 0:
        lines.append("")
        lines.append(
            f"△ showing first <b>{MAX_LINKS_SHOWN}</b> of "
            f"<b>{len(s['links'])}</b> links."
        )
    return sc("\n".join(lines))


def build_summary(s: dict, stats: dict[str, int], action: str, replacement: str) -> str:
    verb = "replaced" if action == "replace" else "removed"
    unit = "line(s)" if s["mode"] == "line" else "link(s)"
    n = sum(stats.values())
    mode_name = "whole line" if s["mode"] == "line" else "link only"

    out = [
        "✓ <b>done!</b>",
        "",
        f"▤ <b>{esc_sc(s['file_name'])}</b>",
        f"✓ <b>{n}</b> {unit} {verb}",
        f"❖ mode · <i>{mode_name}</i>",
    ]
    if action == "replace":
        out.append(f"✎ with · <code>{escape(_short(replacement))}</code>")

    out.append("")
    out.append(_div())
    out.append("<b>changes</b>")

    for tok, cnt in list(stats.items())[:15]:
        icon = _link_icon(tok)
        suffix = f"  ×{cnt}" if cnt > 1 else ""
        out.append(f"{icon} <code>{escape(_short(tok))}</code>{suffix}")
    if len(stats) > 15:
        out.append(f"… and <b>{len(stats) - 15}</b> more")

    out.append("")
    out.append("<i>send another file anytime.</i>")
    return sc("\n".join(out))


DONE_KB = InlineKeyboardMarkup([
    [_nav_btn("⌂ home", "start"), _nav_btn("❖ help", "help")],
])


# --------------------------------------------------------------------------- #
# Handlers                                                                     #
# --------------------------------------------------------------------------- #


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_panel(context.bot, update.effective_chat.id, "start")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_panel(context.bot, update.effective_chat.id, "help")


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rename [args] — as a reply to a document (or with one attached)."""
    msg = update.effective_message
    doc = None
    if msg.reply_to_message is not None and msg.reply_to_message.document:
        doc = msg.reply_to_message.document
    elif msg.document:
        doc = msg.document

    raw = " ".join(context.args or "").strip()
    await do_rename(update, context, doc, raw)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skip while awaiting a replacement → just remove instead."""
    chat_id = update.effective_chat.id
    s = get_session(chat_id)
    if not s or s["awaiting"] != "replace" or s["user_id"] != update.effective_user.id:
        await update.effective_message.reply_text(
            sc("△ nothing to skip right now.\n") +
            "<i>send a file to get started.</i>",
            parse_mode="HTML",
        )
        return
    s["awaiting"] = None
    await _apply(update, context, s, "remove")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SESSIONS.pop(update.effective_chat.id, None)
    await update.effective_message.reply_text(
        sc("✕ cancelled.\n") + "<i>send a file anytime to start over.</i>",
        parse_mode="HTML",
    )


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _caption_command(msg) -> tuple[str, list[str]] | None:
    """Detect a bot command typed as a document caption (CommandHandler only
    sees message text, so captions are routed manually)."""
    ents = msg.caption_entities or ()
    if not (ents and ents[0].type == MessageEntity.BOT_COMMAND
            and ents[0].offset == 0 and msg.caption):
        return None
    cmd = msg.caption[1:ents[0].length].split("@")[0].lower()
    args = msg.caption[ents[0].length:].strip().split()
    return cmd, args


async def do_rename(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    doc, raw_args: str) -> None:
    """The standalone rename flow — never touches editor sessions."""
    msg = update.effective_message
    chat_id = update.effective_chat.id

    if doc is None or not raw_args:
        # no file or no args → show the rename guide
        await send_panel(context.bot, chat_id, "rename", reply_to=msg.message_id)
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(
            sc(f"△ file too big — limit is ~{MAX_FILE_MB} mb."),
            parse_mode="HTML",
        )
        return

    old_name = doc.file_name or "file"
    new_name, err = parse_rename_target(raw_args, old_name)
    if err:
        await msg.reply_text(
            sc("△ that name won't work — ") + esc_sc(err) + "\n\n"
            "<i>e.g. <code>/rename notes</code> · "
            "<code>/rename notes md</code> · <code>/rename .md</code></i>",
            parse_mode="HTML",
        )
        return

    status = await msg.reply_text(sc("◌ renaming…"), parse_mode="HTML")

    buf = io.BytesIO()
    tg_file = await doc.get_file()
    await tg_file.download_to_memory(buf)
    buf.seek(0)

    summary = sc(
        "✓ <b>renamed!</b>\n\n"
        f"▤ old · <code>{escape(_short(old_name))}</code>\n"
        f"▸ new · <code>{escape(_short(new_name))}</code>\n\n"
        "<i>reply to any file with /rename to use again.</i>"
    )
    await context.bot.send_document(
        chat_id,
        document=InputFile(buf, filename=new_name),
        caption=summary[:CAPTION_MAX],
        parse_mode="HTML",
        reply_markup=DONE_KB,
    )
    try:
        await status.delete()
    except Exception:
        pass


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    doc = msg.document
    chat_id = update.effective_chat.id

    # "/rename …" typed as the file's caption → standalone rename flow
    cap = _caption_command(msg)
    if cap and cap[0] == "rename":
        await do_rename(update, context, doc, " ".join(cap[1]))
        return

    if not is_text_file(doc.file_name or "", doc.mime_type):
        await msg.reply_text(
            sc("△ unsupported file\n\n"
               "i only handle text / code files:\n"
               ".txt  .py  .js  .json  .html  .css  .md …\n\n"
               "no zips, videos, photos or binaries.\n"
               "(tip: reply to any file with /rename to rename it)"),
            parse_mode="HTML",
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(
            sc(f"△ file too big\n\nlimit is ~{MAX_FILE_MB} mb. try a smaller file."),
            parse_mode="HTML",
        )
        return

    status = await msg.reply_text(
        sc("◌ downloading & scanning…\n") + "<i>hang tight</i>",
        parse_mode="HTML",
    )

    buf = io.BytesIO()
    tg_file = await doc.get_file()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()

    if _is_binary(data):
        await status.edit_text(
            sc("△ binary file detected\n\nthat doesn't look like text/code."),
            parse_mode="HTML",
        )
        return

    content, enc = decode_bytes(data)
    links, total = find_links(content)

    if not links:
        SESSIONS.pop(chat_id, None)
        text = sc("✓ <b>all clean!</b>\n\n"
                  f"▤ <code>{escape(doc.file_name or 'file')}</code>\n"
                  "no promo links found in this file.")
        asset = _photo_arg("done.jpg")
        try:
            if asset is None:
                raise RuntimeError("no artwork")
            m = await status.edit_media(
                InputMediaPhoto(asset, caption=text, parse_mode="HTML"),
                reply_markup=DONE_KB,
            )
            _cache_photo("done.jpg", m)
        except Exception:
            await status.edit_text(text, parse_mode="HTML", reply_markup=DONE_KB)
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
                sc("△ replacement can't be empty.\n"
                   "send some text, or /skip to remove instead."),
                parse_mode="HTML",
            )
            return
        s["awaiting"] = None
        await msg.reply_text(sc("◌ editing file…"), parse_mode="HTML")
        await _apply(update, context, s, "replace", replacement)
        return

    await msg.reply_text(
        sc("▤ send me a text/code file as a document\n"
           "and i'll find its links.\n\n"
           "tip · use the paperclip → file, not photo.\n"
           "rename · reply to a file with /rename newname"),
        parse_mode="HTML",
    )


async def on_media(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        sc("△ media not supported\n\n"
           "i only work with text/code files ▤ —\n"
           "not photos, videos, audio, stickers or other media."),
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
            sc("△ nothing matched\n\n"
               "tap at least one link first, then try again."),
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
        reply_markup=DONE_KB,
    )
    SESSIONS.pop(chat_id, None)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    data = q.data or ""

    # ---- menu navigation (works with or without an editor session) ----
    if data.startswith("n:"):
        target = data[2:]
        if target == "close":
            await q.answer()
            try:
                await q.delete_message()
            except Exception:
                pass
            return
        if target in PANELS:
            await q.answer()
            await edit_panel(q, target)
        return

    # ---- link editor session callbacks ----
    s = get_session(chat_id)

    if not s:
        await q.answer(sc_plain("△ session expired — send the file again."),
                       show_alert=True)
        return
    if q.from_user.id != s["user_id"]:
        await q.answer(sc_plain("◌ this isn't your session — send your own file."))
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
        await q.answer(sc_plain(f"✓ {n} selected") if n else "Selection cleared")

    elif data == "m":
        s["mode"] = "line" if s["mode"] == "link" else "link"
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(
            f"Mode → {'whole line ⌁' if s['mode'] == 'line' else 'link only ⌁'}"
        )

    elif data == "sa":
        s["selected"] = set(range(len(s["links"][:MAX_LINKS_SHOWN])))
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(sc_plain(f"✓ all {len(s['selected'])} selected"))

    elif data == "sn":
        s["selected"] = set()
        await q.edit_message_text(build_header(s), parse_mode="HTML",
                                  reply_markup=build_keyboard(s))
        await q.answer(sc_plain("◇ selection cleared"))

    elif data == "rm":
        if not s["selected"]:
            await q.answer("Tap at least one link first ▲", show_alert=True)
            return
        await q.answer("✕ Removing…")
        await q.edit_message_text(
            sc("◌ editing file…\n") +
            f"<i>removing {len(s['selected'])} item(s)</i>",
            parse_mode="HTML",
        )
        await _apply(update, context, s, "remove")

    elif data == "rp":
        if not s["selected"]:
            await q.answer("Tap at least one link first ▲", show_alert=True)
            return
        s["awaiting"] = "replace"
        await q.answer()
        n = len(s["selected"])
        await q.edit_message_text(
            sc(_box("✎ replace") + "\n\n"
               f"▤ <b>{escape(s['file_name'])}</b>\n"
               f"✓ <b>{n}</b> link(s) selected\n\n" + _div() + "\n"
               "send the replacement text now.\n\n"
               "• /skip — remove instead\n"
               "• /cancel — abort\n"),
            parse_mode="HTML",
        )

    elif data == "cx":
        SESSIONS.pop(chat_id, None)
        await q.answer("Cancelled")
        try:
            await q.edit_message_text(
                sc("✕ cancelled.\n") +
                "<i>send a file anytime to start over.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[_nav_btn("⌂ home", "start")]]),
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("rename", cmd_rename))
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
