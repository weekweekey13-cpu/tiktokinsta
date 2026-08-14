"""
TikTok / Instagram → видео Telegram-бот.

Пользователь кидает ссылку — получает видео в лучшем качестве без водяного знака.
Перед скачиванием обязательна подписка на каналы из админки.
Админ @bonamartin69: каналы, реклама, рассылка, рестарт.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def load_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if token:
        return token
    fallback = ROOT.parent / "токен3.txt"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return ""


BOT_TOKEN = load_token()
PORT = int(os.getenv("PORT", "10000"))
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "bonamartin69").split(",")
    if u.strip()
} | {"bonamartin69"}

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
STATS_PATH = DATA_DIR / "stats.json"
CACHE_PATH = DATA_DIR / "file_cache.json"
CACHE_LIMIT = 3000
SUBS_PATH = DATA_DIR / "subscriptions.json"
SUBS_EVENT_LIMIT = 400
BOT_USERNAME = "downloader_insta_tiktokbot"

MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "900"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))

TIKTOK_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s<>]+)",
    re.IGNORECASE,
)
INSTA_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/[^\s<>]+)",
    re.IGNORECASE,
)
TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_+]+)",
    re.IGNORECASE,
)
TG_C_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)",
    re.IGNORECASE,
)

STARTED_AT = time.time()
user_locks: dict[int, asyncio.Lock] = {}
pending_action: dict[int, str] = {}
settings_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tiktokinsta")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def default_settings() -> dict[str, Any]:
    return {
        "channels": [],
        "ads": {
            "enabled_after_download": False,
            "text": "",
            "buttons": [],
        },
    }


def load_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не смог прочитать %s", path)
    return fallback


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> dict[str, Any]:
    with settings_lock:
        existed = SETTINGS_PATH.exists()
        data = load_json(SETTINGS_PATH, default_settings())
        if not isinstance(data, dict):
            data = default_settings()
        data.setdefault("channels", [])
        ads = data.get("ads")
        if not isinstance(ads, dict):
            ads = default_settings()["ads"]
        ads.setdefault("enabled_after_download", False)
        ads.setdefault("text", "")
        ads.setdefault("buttons", [])
        if not isinstance(ads["buttons"], list):
            ads["buttons"] = []
        data["ads"] = ads
        if not existed:
            save_json(SETTINGS_PATH, data)
        return data


def save_settings(data: dict[str, Any]) -> None:
    with settings_lock:
        save_json(SETTINGS_PATH, data)


def load_stats() -> dict[str, Any]:
    data = load_json(STATS_PATH, {"downloads": 0, "users": []})
    data.setdefault("downloads", 0)
    data.setdefault("users", [])
    return data


def touch_user(user_id: int) -> None:
    with settings_lock:
        stats = load_stats()
        users = set(int(x) for x in (stats.get("users") or []) if str(x).lstrip("-").isdigit())
        users.add(int(user_id))
        stats["users"] = sorted(users)
        save_json(STATS_PATH, stats)


def bump_stats(user_id: int) -> None:
    with settings_lock:
        stats = load_stats()
        stats["downloads"] = int(stats.get("downloads", 0)) + 1
        users = set(int(x) for x in (stats.get("users") or []) if str(x).lstrip("-").isdigit())
        users.add(int(user_id))
        stats["users"] = sorted(users)
        save_json(STATS_PATH, stats)


def load_cache() -> dict[str, Any]:
    data = load_json(CACHE_PATH, {})
    return data if isinstance(data, dict) else {}


def cache_key(platform: str, media_id: str) -> str:
    return f"{platform}:{media_id}"


def get_cached_video(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    with settings_lock:
        item = load_cache().get(key)
    if not isinstance(item, dict) or not item.get("file_id"):
        return None
    return item


def save_cached_video(key: str, meta: dict[str, Any]) -> None:
    if not key or not meta.get("file_id"):
        return
    with settings_lock:
        cache = load_cache()
        cache[key] = {
            "file_id": meta["file_id"],
            "title": meta.get("title") or "video",
            "author": meta.get("author") or "",
            "duration": int(meta.get("duration") or 0),
            "width": int(meta.get("width") or 0),
            "height": int(meta.get("height") or 0),
            "filename": meta.get("filename") or "",
            "ts": time.time(),
        }
        if len(cache) > CACHE_LIMIT:
            oldest = sorted(cache.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))
            for old_key, _ in oldest[: len(cache) - CACHE_LIMIT]:
                cache.pop(old_key, None)
        save_json(CACHE_PATH, cache)


def drop_cached_video(key: str) -> None:
    if not key:
        return
    with settings_lock:
        cache = load_cache()
        if key in cache:
            cache.pop(key, None)
            save_json(CACHE_PATH, cache)


def load_subs() -> dict[str, Any]:
    data = load_json(SUBS_PATH, {"events": [], "members": {}})
    if not isinstance(data, dict):
        data = {"events": [], "members": {}}
    data.setdefault("events", [])
    data.setdefault("members", {})
    return data


def format_sub_time(ts: float) -> str:
    try:
        from datetime import datetime, timedelta, timezone

        moscow = timezone(timedelta(hours=3))
        return datetime.fromtimestamp(float(ts), tz=moscow).strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def display_user(user_id: int, username: str = "", name: str = "") -> str:
    if username:
        return f"@{username.lstrip('@')}"
    if name:
        return name
    return f"id {user_id}"


def channel_store_key(ch: dict[str, Any]) -> str:
    if has_numeric_chat(ch):
        return str(int(str(ch.get("chat_id"))))
    return str(ch.get("id") or ch.get("username") or "unknown")


def record_subscription(
    *,
    user_id: int,
    username: str = "",
    name: str = "",
    channel_key: str,
    channel_title: str,
    action: str,
) -> None:
    if not user_id or not channel_key:
        return
    now = time.time()
    member_key = f"{user_id}:{channel_key}"
    with settings_lock:
        data = load_subs()
        members = data["members"]
        prev = members.get(member_key) if isinstance(members.get(member_key), dict) else None
        if action == "check" and prev and prev.get("action") != "leave":
            prev["last_seen"] = now
            prev["username"] = username or prev.get("username") or ""
            prev["name"] = name or prev.get("name") or ""
            save_json(SUBS_PATH, data)
            return
        event = {
            "user_id": int(user_id),
            "username": (username or "").lstrip("@"),
            "name": name or "",
            "channel_key": channel_key,
            "channel_title": channel_title,
            "action": action,
            "ts": now,
        }
        data["events"].append(event)
        data["events"] = data["events"][-SUBS_EVENT_LIMIT:]
        if action == "leave":
            members.pop(member_key, None)
        else:
            members[member_key] = {
                "user_id": int(user_id),
                "username": (username or "").lstrip("@"),
                "name": name or "",
                "channel_key": channel_key,
                "channel_title": channel_title,
                "action": action,
                "joined_at": (prev or {}).get("joined_at") or now,
                "last_seen": now,
            }
        save_json(SUBS_PATH, data)


def record_user_on_channels(user, channels: list[dict[str, Any]], action: str) -> None:
    if user is None:
        return
    name = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
    for ch in channels:
        record_subscription(
            user_id=user.id,
            username=user.username or "",
            name=name,
            channel_key=channel_store_key(ch),
            channel_title=channel_button_title(ch, 1),
            action=action,
        )


def find_required_channel_for_chat(chat) -> dict[str, Any] | None:
    if chat is None:
        return None
    cid = str(getattr(chat, "id", "") or "")
    uname = (getattr(chat, "username", None) or "").lower()
    for ch in load_settings().get("channels") or []:
        if has_numeric_chat(ch) and str(int(str(ch.get("chat_id")))) == cid:
            return ch
        if uname and str(ch.get("username") or "").lower() == uname:
            return ch
        if uname and str(ch.get("id") or "").lower() == uname:
            return ch
    return None


def subs_overview_text(offset: int = 0, page_size: int = 10) -> tuple[str, int]:
    data = load_subs()
    events = list(reversed(data.get("events") or []))
    members = data.get("members") or {}
    live = [m for m in members.values() if isinstance(m, dict) and m.get("action") != "leave"]
    by_ch: dict[str, int] = {}
    for m in live:
        title = m.get("channel_title") or m.get("channel_key") or "канал"
        by_ch[title] = by_ch.get(title, 0) + 1
    total = len(events)
    chunk = events[offset : offset + page_size]
    lines = [
        "👥 <b>Кто подписался</b>",
        "",
        f"Сейчас в журнале как подписанные: <b>{len(live)}</b>",
        f"Событий: <b>{total}</b>",
    ]
    if by_ch:
        lines.append("")
        lines.append("<b>По каналам:</b>")
        for title, n in sorted(by_ch.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"• {title} — {n}")
    lines.append("")
    if not chunk:
        lines.append("Пока пусто. Записи появятся, когда кто-то вступит или нажмёт «Я подписался».")
    else:
        lines.append("<b>Последние:</b>")
        verbs = {"join": "вступил", "check": "проверили", "leave": "отписался"}
        for ev in chunk:
            who = display_user(int(ev.get("user_id") or 0), ev.get("username") or "", ev.get("name") or "")
            verb = verbs.get(ev.get("action") or "", ev.get("action") or "")
            ch = ev.get("channel_title") or ev.get("channel_key") or "канал"
            lines.append(f"{format_sub_time(ev.get('ts') or 0)}  {who} → {ch} ({verb})")
    return "\n".join(lines), total


def subs_keyboard(offset: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"subs:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"subs:{offset + page_size}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"subs:{offset}")])
    rows.append([InlineKeyboardButton("⬅️ Назад в админку", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def is_admin(user) -> bool:
    if user is None:
        return False
    uname = (user.username or "").lstrip("@").lower()
    return bool(uname) and uname in ADMIN_USERNAMES


def get_lock(user_id: int) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_locks[user_id] = lock
    return lock


def has_numeric_chat(ch: dict[str, Any]) -> bool:
    raw = ch.get("chat_id")
    if raw in (None, ""):
        return False
    return str(raw).lstrip("-").isdigit()


def parse_channel_input(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None

    c_m = TG_C_RE.search(text)
    if c_m:
        chat_id = int("-100" + c_m.group(1))
        return {
            "id": str(chat_id),
            "title": f"Чат {chat_id}",
            "username": "",
            "chat_id": chat_id,
            "url": text if text.lower().startswith("http") else f"https://t.me/c/{c_m.group(1)}/1",
        }

    if text.startswith("@"):
        username = text[1:].strip()
        if not username:
            return None
        return {
            "id": username.lower(),
            "title": "@" + username,
            "username": username,
            "chat_id": "@" + username,
            "url": f"https://t.me/{username}",
        }

    m = TG_LINK_RE.search(text)
    if m:
        slug = m.group(1)
        url = text if text.lower().startswith("http") else "https://" + text
        if slug.startswith("+"):
            return {
                "id": slug,
                "title": "Приватный канал",
                "username": "",
                "chat_id": "",
                "url": url if url.startswith("http") else f"https://t.me/{slug}",
            }
        return {
            "id": slug.lower(),
            "title": "@" + slug,
            "username": slug,
            "chat_id": "@" + slug,
            "url": f"https://t.me/{slug}",
        }

    if text.startswith("-") and text[1:].isdigit():
        return {
            "id": text,
            "title": f"Чат {text}",
            "username": "",
            "chat_id": int(text),
            "url": "",
        }
    return None


def parse_ad_button(text: str) -> dict[str, str] | None:
    text = (text or "").strip()
    if not text:
        return None
    if "|" in text:
        label, url = text.split("|", 1)
    else:
        parts = text.split(None, 1)
        if len(parts) == 2 and parts[1].startswith(("http://", "https://", "t.me/", "@")):
            label, url = parts
        else:
            parsed = parse_channel_input(text)
            if not parsed:
                return None
            label = channel_button_title(parsed, 1)
            url = parsed.get("url") or ""
            if not url:
                return None
            return {"text": label[:64], "url": url}
    label = " ".join(label.split()).strip()
    url = url.strip()
    if url.startswith("@"):
        url = "https://t.me/" + url[1:]
    elif url.lower().startswith("t.me/"):
        url = "https://" + url
    if not label or not url.startswith(("http://", "https://")):
        return None
    return {"text": label[:64], "url": url}


def channel_button_title(ch: dict[str, Any], index: int) -> str:
    title = (ch.get("title") or "").strip()
    if title:
        return title[:40]
    username = (ch.get("username") or "").strip()
    if username:
        return "@" + username.lstrip("@")
    return f"Канал {index}"


def subscribe_button_label(ch: dict[str, Any], index: int) -> str:
    custom = (ch.get("button_text") or "").strip()
    if custom:
        return custom[:64]
    return f"Подписаться на {channel_button_title(ch, index)}"[:64]


def check_button_label(settings: dict[str, Any] | None = None) -> str:
    data = settings if settings is not None else load_settings()
    custom = (data.get("check_button") or "").strip()
    return (custom or "✅ Я подписался")[:64]


def fmt_duration(sec: int | float | None) -> str:
    if not sec:
        return "—"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or "video")[:80]


def channel_public_url(ch: dict[str, Any]) -> str:
    url = (ch.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    username = (ch.get("username") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"
    chat_id = ch.get("chat_id")
    if chat_id is not None:
        raw = str(chat_id)
        if raw.startswith("-100") and raw[4:].isdigit():
            return f"https://t.me/c/{raw[4:]}/1"
    return ""


def subscribe_rows(channels: list[dict[str, Any]]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for i, ch in enumerate(channels, start=1):
        label = subscribe_button_label(ch, i)
        url = channel_public_url(ch)
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
        else:
            rows.append([InlineKeyboardButton(label, callback_data=f"subneed:{ch.get('id')}")])
    if channels:
        rows.append([InlineKeyboardButton(check_button_label(), callback_data="check_sub")])
    return rows


def subscribe_keyboard(channels: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(subscribe_rows(channels))


def ads_buttons(settings: dict[str, Any] | None = None) -> list[list[InlineKeyboardButton]]:
    data = settings if settings is not None else load_settings()
    ads = data.get("ads") or {}
    rows: list[list[InlineKeyboardButton]] = []
    for btn in ads.get("buttons") or []:
        if not isinstance(btn, dict):
            continue
        label = (btn.get("text") or "").strip()[:64]
        url = (btn.get("url") or "").strip()
        if label and url.startswith(("http://", "https://")):
            rows.append([InlineKeyboardButton(label, url=url)])
    return rows


def ads_keyboard(settings: dict[str, Any] | None = None) -> InlineKeyboardMarkup | None:
    rows = ads_buttons(settings)
    return InlineKeyboardMarkup(rows) if rows else None


def video_caption(title: str, author: str, duration: int, settings: dict[str, Any] | None = None) -> str:
    data = settings if settings is not None else load_settings()
    ads = data.get("ads") or {}
    lines = [f"🎬 {title or 'Видео'}"]
    if author:
        lines.append(f"👤 {author}")
    lines.append(f"⏱ {fmt_duration(duration)}")
    promo = (ads.get("text") or "").strip()
    if ads.get("enabled_after_download") and promo:
        lines.append("")
        lines.append(promo)
    return "\n".join(lines)[:1024]


BTN_START = "🚀 Старт"
BTN_ADMIN = "🛠 Админка"


def is_start_text(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"🚀 старт", "старт", "start", "/start"}


def is_admin_btn_text(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"🛠 админка", "админка"}


def bottom_keyboard(admin: bool) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(BTN_START)]
    if admin:
        row.append(KeyboardButton(BTN_ADMIN))
    return ReplyKeyboardMarkup([row], resize_keyboard=True, is_persistent=True)


def user_home_keyboard(admin: bool, channels: list[dict[str, Any]] | None = None) -> InlineKeyboardMarkup:
    rows = subscribe_rows(channels or [])
    rows.append([InlineKeyboardButton("📥 Как скачать?", callback_data="help")])
    if admin:
        rows.append([InlineKeyboardButton("🛠 Админка", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard(channels: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, ch in enumerate(channels):
        title = channel_button_title(ch, i + 1)
        rows.append(
            [
                InlineKeyboardButton(f"📢 {title}", callback_data=f"chinfo:{ch.get('id')}"),
                InlineKeyboardButton("✏️", callback_data=f"chbtn:{ch.get('id')}"),
                InlineKeyboardButton("🗑", callback_data=f"chdel:{ch.get('id')}"),
            ]
        )
    rows.append([InlineKeyboardButton("👥 Кто подписался", callback_data="subs:0")])
    rows.append([InlineKeyboardButton("📣 Реклама каналов", callback_data="ads")])
    rows.append([InlineKeyboardButton("✏️ Текст «Я подписался»", callback_data="checkbtn")])
    rows.append([InlineKeyboardButton("👁 Как видят кнопку подписки", callback_data="subpreview")])
    rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="chadd")])
    rows.append([InlineKeyboardButton("♻️ Рестарт бота", callback_data="restart_ask")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def ads_admin_keyboard(settings: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    data = settings if settings is not None else load_settings()
    ads = data.get("ads") or {}
    enabled = bool(ads.get("enabled_after_download"))
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "🟢 Реклама после скачивания: вкл" if enabled else "⚪️ Реклама после скачивания: выкл",
                callback_data="adstoggle",
            )
        ],
        [InlineKeyboardButton("✏️ Текст рекламы под видео", callback_data="adstext")],
        [InlineKeyboardButton("➕ Кнопка на канал", callback_data="adsaddbtn")],
    ]
    for i, btn in enumerate(ads.get("buttons") or []):
        if not isinstance(btn, dict):
            continue
        label = (btn.get("text") or f"кнопка {i + 1}")[:24]
        rows.append(
            [
                InlineKeyboardButton(f"🔗 {label}", callback_data=f"adsbtn:{i}"),
                InlineKeyboardButton("🗑", callback_data=f"adsdel:{i}"),
            ]
        )
    rows.append([InlineKeyboardButton("👁 Предпросмотр рекламы", callback_data="adspreview")])
    rows.append([InlineKeyboardButton("📤 Рассылка всем пользователям", callback_data="adscast")])
    rows.append([InlineKeyboardButton("⬅️ Назад в админку", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def ads_admin_text() -> str:
    settings = load_settings()
    ads = settings.get("ads") or {}
    users = load_stats().get("users") or []
    enabled = "включена" if ads.get("enabled_after_download") else "выключена"
    promo = (ads.get("text") or "").strip() or "—"
    buttons = ads.get("buttons") or []
    lines = [
        "📣 <b>Реклама других каналов</b>",
        "",
        f"После скачивания: <b>{enabled}</b>",
        f"Пользователей для рассылки: <b>{len(users)}</b>",
        "",
        f"Текст под видео:\n{promo}",
        "",
        f"Кнопок: <b>{len(buttons)}</b>",
    ]
    for i, btn in enumerate(buttons, start=1):
        if isinstance(btn, dict):
            lines.append(f"{i}. {btn.get('text') or '—'} — {btn.get('url') or '—'}")
    lines.extend(
        [
            "",
            "Кнопка ведёт в чужой канал. Формат новой кнопки:",
            "<code>Наш канал | https://t.me/имя</code>",
            "",
            "Рассылка: напиши текст (можно с ссылкой) — уйдёт всем, кто запускал бота.",
        ]
    )
    return "\n".join(lines)


def bind_known_chat(chat, invite_url: str = "") -> dict[str, Any]:
    settings = load_settings()
    chat_id = int(chat.id)
    username = getattr(chat, "username", None) or ""
    title = chat.title or str(chat_id)
    url = f"https://t.me/{username}" if username else (invite_url or "")

    for ch in settings["channels"]:
        same = str(ch.get("chat_id")) in {str(chat_id), f"@{username}"} if username else str(ch.get("chat_id")) == str(chat_id)
        same = same or (username and str(ch.get("id") or "").lower() == username.lower())
        same = same or (title and (ch.get("title") or "") == title)
        if same:
            ch["chat_id"] = chat_id
            ch["title"] = title
            if username:
                ch["username"] = username
                ch["id"] = username.lower()
                ch["url"] = url or ch.get("url") or ""
            else:
                ch["id"] = str(chat_id)
                if url and not channel_public_url(ch):
                    ch["url"] = url
            save_settings(settings)
            return ch

    incomplete = [c for c in settings["channels"] if not has_numeric_chat(c)]
    if len(incomplete) == 1:
        ch = incomplete[0]
        keep_url = ch.get("url") or url
        ch["chat_id"] = chat_id
        ch["title"] = title or ch.get("title")
        ch["id"] = username.lower() if username else str(chat_id)
        if username:
            ch["username"] = username
        if keep_url:
            ch["url"] = keep_url
        save_settings(settings)
        return ch

    new_ch = {
        "id": username.lower() if username else str(chat_id),
        "title": title,
        "username": username,
        "chat_id": chat_id,
        "url": url,
        "button_text": f"Подписаться на {title}"[:64],
    }
    settings["channels"].append(new_ch)
    save_settings(settings)
    return new_ch


def channel_candidates(ch: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    raw = ch.get("chat_id")
    if raw not in (None, ""):
        s = str(raw)
        if s.lstrip("-").isdigit():
            out.append(int(s))
        elif s.startswith("@") and not s.startswith("@+"):
            out.append(s)
    username = (ch.get("username") or "").strip().lstrip("@")
    if username and not username.startswith("+"):
        out.append("@" + username)
    seen: set[str] = set()
    uniq: list[Any] = []
    for item in out:
        key = str(item).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def is_bot_rights_error(err: BaseException) -> bool:
    text = str(err).lower()
    return any(
        s in text
        for s in (
            "member list is inaccessible",
            "chat not found",
            "bot is not a member",
            "not enough rights",
            "need administrator",
            "chat_admin_required",
        )
    )


async def check_one_channel(bot, user_id: int, ch: dict[str, Any]) -> tuple[str, str]:
    candidates = channel_candidates(ch)
    if not candidates:
        return "norights", "нет chat_id у канала"
    last = ""
    for cid in candidates:
        try:
            member = await bot.get_chat_member(cid, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return "left", str(member.status)
            if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", True) is False:
                return "left", "restricted"
            return "ok", str(member.status)
        except (Forbidden, BadRequest, TelegramError) as e:
            last = str(e)
            log.warning("Проверка %s / %s: %s", ch.get("username") or ch.get("id"), cid, e)
            if is_bot_rights_error(e):
                return "norights", last
    return "bad", last or "неизвестная ошибка"


async def required_missing(
    bot, user_id: int, channels: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    missing: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    for ch in channels:
        status, _detail = await check_one_channel(bot, user_id, ch)
        if status == "ok":
            continue
        if status == "left":
            missing.append(ch)
        else:
            broken.append(ch)
    return missing, broken


def subscribe_block_text(
    channels: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    broken: list[dict[str, Any]],
) -> str:
    if broken and not missing:
        names = ", ".join(channel_button_title(ch, i) for i, ch in enumerate(broken, start=1))
        return (
            "⚠️ <b>Не могу проверить подписку</b>\n\n"
            f"Канал: {names}\n\n"
            "Ты можешь быть подписан, но Telegram не отдаёт список участников, "
            "пока бот не станет <b>админом</b> этого канала.\n\n"
            f"Админ канала: добавь <code>@{BOT_USERNAME}</code> администратором "
            "и <b>перешли боту любой пост</b> из приватного канала — иначе нет числового id."
        )
    extra = ""
    if broken:
        names = ", ".join(channel_button_title(ch, i) for i, ch in enumerate(broken, start=1))
        extra = (
            f"\n\n⚠️ Ещё не проверяется: {names}. "
            "Туда тоже нужно добавить бота админом."
        )
    return (
        "🔒 <b>Сначала подпишись на каналы</b>\n\n"
        "Без подписки скачивание недоступно.\n"
        "Нажми «Подписаться», вступи, затем «Я подписался»."
        + extra
    )


async def reply_subscribe_gate(
    update: Update,
    channels: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    broken: list[dict[str, Any]],
) -> None:
    text = subscribe_block_text(channels, missing, broken)
    markup = subscribe_keyboard(channels)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
            )
        except BadRequest:
            await update.callback_query.message.reply_html(
                text, reply_markup=markup, disable_web_page_preview=True
            )
    elif update.effective_message:
        await update.effective_message.reply_html(
            text, reply_markup=markup, disable_web_page_preview=True
        )


async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if is_admin(user):
        return True
    channels = load_settings().get("channels") or []
    if not channels:
        return True
    missing, broken = await required_missing(context.bot, user.id, channels)
    if not missing and not broken:
        record_user_on_channels(user, channels, "check")
        return True
    ok = [c for c in channels if c not in missing and c not in broken]
    if ok:
        record_user_on_channels(user, ok, "check")
    await reply_subscribe_gate(update, channels, missing, broken)
    return False


def welcome_text(admin: bool) -> str:
    extra = "\n\n🛠 Тебе доступна <b>админка</b>." if admin else ""
    return (
        "📥 <b>TikTok / Instagram → видео</b>\n\n"
        "Кинь ссылку на ролик — пришлю в лучшем качестве и без водяного знака.\n\n"
        "Подходит:\n"
        "• tiktok.com/@…/video/…\n"
        "• vm.tiktok.com/…\n"
        "• instagram.com/reel/…\n"
        "• instagram.com/p/…\n\n"
        f"Лимит: до {MAX_DURATION_SEC // 60} минут, файл до {MAX_FILE_MB} МБ.{extra}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    target = update.effective_message
    if not user or not target:
        return
    pending_action.pop(user.id, None)
    touch_user(user.id)
    admin = is_admin(user)
    channels = load_settings().get("channels") or []
    await target.reply_html(
        welcome_text(admin),
        reply_markup=bottom_keyboard(admin),
        disable_web_page_preview=True,
    )
    if channels:
        await target.reply_html(
            "📢 Сначала подпишись на канал кнопкой ниже, потом кидай ссылку на TikTok или Instagram.",
            reply_markup=subscribe_keyboard(channels),
            disable_web_page_preview=True,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        channels = load_settings().get("channels") or []
        await update.message.reply_html(
            welcome_text(is_admin(update.effective_user)),
            reply_markup=user_home_keyboard(is_admin(update.effective_user), channels),
        )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_admin(update.effective_user):
        if update.message:
            await update.message.reply_text("Недостаточно прав.")
        return
    await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(load_settings()["channels"]))


def admin_text() -> str:
    settings = load_settings()
    stats = load_stats()
    channels = settings.get("channels") or []
    ads = settings.get("ads") or {}
    uptime = int(time.time() - STARTED_AT)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    lines = [
        "🛠 <b>Админка TikTok / Instagram</b>",
        "",
        f"📢 Каналов для подписки: <b>{len(channels)}</b>",
        f"👥 Подписавшихся в журнале: <b>{sum(1 for m in (load_subs().get('members') or {}).values() if isinstance(m, dict) and m.get('action') != 'leave')}</b>",
        f"⬇️ Скачиваний: <b>{int(stats.get('downloads', 0))}</b>",
        f"👤 Пользователей: <b>{len(stats.get('users') or [])}</b>",
        f"📣 Реклама после скачивания: <b>{'вкл' if ads.get('enabled_after_download') else 'выкл'}</b>",
        f"⏱ Аптайм: {h}ч {m}м {s}с",
        "",
        "Добавляй/убирай каналы. Бот должен быть <b>админом</b> каждого канала, иначе не проверит подписку.",
        "",
        "Чтобы добавить: нажми «Добавить канал» и пришли ссылку вида",
        "<code>https://t.me/channel</code> или <code>@channel</code>.",
        "",
        "📣 Реклама — кнопки на другие каналы под видео и рассылка всем пользователям.",
        "",
        "✏️ — поменять текст кнопки подписки.",
    ]
    if channels:
        lines.append("")
        lines.append("<b>Сейчас обязательны:</b>")
        for i, ch in enumerate(channels, start=1):
            url = ch.get("url") or ""
            title = channel_button_title(ch, i)
            lines.append(f"{i}. {title}" + (f" — {url}" if url else ""))
    return "\n".join(lines)


async def show_admin(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await query.edit_message_text(
            admin_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(load_settings()["channels"]),
            disable_web_page_preview=True,
        )
    except BadRequest:
        await query.message.reply_html(
            admin_text(),
            reply_markup=admin_keyboard(load_settings()["channels"]),
            disable_web_page_preview=True,
        )


async def show_ads_admin(query) -> None:
    try:
        await query.edit_message_text(
            ads_admin_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=ads_admin_keyboard(),
            disable_web_page_preview=True,
        )
    except BadRequest:
        await query.message.reply_html(
            ads_admin_text(),
            reply_markup=ads_admin_keyboard(),
            disable_web_page_preview=True,
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    await query.answer()

    if data == "help":
        try:
            await query.edit_message_text(
                welcome_text(is_admin(user)),
                parse_mode=ParseMode.HTML,
                reply_markup=user_home_keyboard(is_admin(user), load_settings().get("channels") or []),
            )
        except BadRequest:
            pass
        return

    if data.startswith("subneed:"):
        await query.answer(
            "У этого канала нет публичной ссылки. Админ должен добавить https://t.me/канал или инвайт.",
            show_alert=True,
        )
        return

    if data == "check_sub":
        channels = load_settings().get("channels") or []
        if not channels:
            await query.edit_message_text(
                "✅ Ограничений нет. Пришли ссылку на TikTok или Instagram.",
                reply_markup=user_home_keyboard(is_admin(user), channels),
            )
            return
        missing, broken = await required_missing(context.bot, user.id, channels)
        if broken and not missing:
            await query.answer(
                f"Бот не админ канала — не видит подписку. Добавь @{BOT_USERNAME} админом.",
                show_alert=True,
            )
            await reply_subscribe_gate(update, channels, missing, broken)
            return
        if missing or broken:
            ok = [c for c in channels if c not in missing and c not in broken]
            if ok:
                record_user_on_channels(user, ok, "check")
            await query.answer("Ещё не на всех каналах. Подпишись и нажми снова.", show_alert=True)
            await reply_subscribe_gate(update, channels, missing, broken)
            return
        record_user_on_channels(user, channels, "check")
        await query.edit_message_text(
            "✅ Подписка есть. Кидай ссылку на TikTok или Instagram — пришлю видео.",
            reply_markup=user_home_keyboard(is_admin(user), channels),
        )
        return

    if not is_admin(user):
        await query.answer("Только для админа.", show_alert=True)
        return

    if data == "admin":
        pending_action.pop(user.id, None)
        await show_admin(query, context)
        return

    if data.startswith("subs:"):
        try:
            offset = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            offset = 0
        text, total = subs_overview_text(offset)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=subs_keyboard(offset, total),
            disable_web_page_preview=True,
        )
        return

    if data == "ads":
        pending_action.pop(user.id, None)
        await show_ads_admin(query)
        return

    if data == "adstoggle":
        settings = load_settings()
        ads = settings.setdefault("ads", default_settings()["ads"])
        ads["enabled_after_download"] = not bool(ads.get("enabled_after_download"))
        save_settings(settings)
        await show_ads_admin(query)
        return

    if data == "adstext":
        pending_action[user.id] = "ads_text"
        await query.edit_message_text(
            "✏️ Пришли текст рекламы, который будет под каждым скачанным видео.\n\n"
            f"Сейчас:\n<code>{(load_settings().get('ads') or {}).get('text') or '—'}</code>\n\n"
            "Чтобы убрать текст — пришли <code>-</code>\nОтмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="ads")]]),
        )
        return

    if data == "adsaddbtn":
        pending_action[user.id] = "ads_add_btn"
        await query.edit_message_text(
            "➕ Пришли кнопку на канал.\n\n"
            "Формат:\n<code>Наш канал | https://t.me/имя</code>\n"
            "или просто <code>@имя</code> / <code>https://t.me/имя</code>\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="ads")]]),
        )
        return

    if data.startswith("adsdel:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        settings = load_settings()
        buttons = list((settings.get("ads") or {}).get("buttons") or [])
        if 0 <= idx < len(buttons):
            buttons.pop(idx)
            settings.setdefault("ads", default_settings()["ads"])["buttons"] = buttons
            save_settings(settings)
            await query.answer("Удалил.")
        await show_ads_admin(query)
        return

    if data.startswith("adsbtn:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        buttons = (load_settings().get("ads") or {}).get("buttons") or []
        btn = buttons[idx] if 0 <= idx < len(buttons) else None
        if not isinstance(btn, dict):
            await show_ads_admin(query)
            return
        await query.edit_message_text(
            f"🔗 <b>{btn.get('text') or 'кнопка'}</b>\n{btn.get('url') or '—'}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🗑 Удалить", callback_data=f"adsdel:{idx}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="ads")],
                ]
            ),
        )
        return

    if data == "adspreview":
        settings = load_settings()
        ads = settings.get("ads") or {}
        text = video_caption("Пример видео", "Автор", 15, settings)
        if not ads.get("enabled_after_download"):
            text += "\n\n⚪️ Сейчас реклама после скачивания выключена — пользователи этот текст не увидят."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                ads_buttons(settings) + [[InlineKeyboardButton("⬅️ Назад", callback_data="ads")]]
            ),
            disable_web_page_preview=True,
        )
        return

    if data == "adscast":
        pending_action[user.id] = "ads_cast"
        users_n = len(load_stats().get("users") or [])
        await query.edit_message_text(
            f"📤 Пришли текст рассылки. Уйдёт <b>{users_n}</b> пользователям.\n\n"
            "Можно вставить ссылку на канал. Чтобы добавить кнопку — напиши второй строкой\n"
            "<code>Кнопка | https://t.me/имя</code>\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="ads")]]),
        )
        return

    if data == "subpreview":
        channels = load_settings().get("channels") or []
        if not channels:
            await query.answer("Сначала добавь канал.", show_alert=True)
            return
        await query.edit_message_text(
            "Так кнопку «Подписаться» видят пользователи. Нажми — откроется канал.",
            reply_markup=InlineKeyboardMarkup(
                subscribe_rows(channels) + [[InlineKeyboardButton("⬅️ Назад в админку", callback_data="admin")]]
            ),
            disable_web_page_preview=True,
        )
        return

    if data == "chadd":
        pending_action[user.id] = "add_channel"
        await query.edit_message_text(
            "➕ Пришли ссылку на канал:\n\n"
            "<code>https://t.me/имя</code>\n"
            "<code>@имя</code>\n\n"
            "Для приватного канала: сделай бота админом и "
            "<b>перешли сюда любой пост</b> из канала.\n"
            "Можно также прислать инвайт <code>https://t.me/+…</code> "
            "и сразу переслать пост.\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]),
        )
        return

    if data.startswith("chdel:"):
        ch_id = data.split(":", 1)[1]
        settings = load_settings()
        before = len(settings["channels"])
        settings["channels"] = [c for c in settings["channels"] if str(c.get("id")) != ch_id]
        save_settings(settings)
        removed = before - len(settings["channels"])
        await query.answer("Удалил." if removed else "Уже нет.")
        await show_admin(query, context)
        return

    if data.startswith("chinfo:"):
        ch_id = data.split(":", 1)[1]
        ch = next((c for c in load_settings()["channels"] if str(c.get("id")) == ch_id), None)
        if not ch:
            await query.answer("Канал не найден.")
            await show_admin(query, context)
            return
        chat_id = ch.get("chat_id") or ""
        status = "не проверял"
        if chat_id:
            try:
                me = await context.bot.get_chat_member(chat_id, context.bot.id)
                status = f"бот в канале: {me.status}"
            except Exception as e:
                status = f"ошибка проверки: {e}"
        text = (
            f"📢 <b>{channel_button_title(ch, 1)}</b>\n\n"
            f"кнопка: <code>{subscribe_button_label(ch, 1)}</code>\n"
            f"id: <code>{ch.get('id')}</code>\n"
            f"chat_id: <code>{chat_id or '—'}</code>\n"
            f"ссылка: {ch.get('url') or '—'}\n"
            f"{status}\n\n"
            "Бот должен быть администратором канала."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ Текст кнопки", callback_data=f"chbtn:{ch_id}")],
                    [InlineKeyboardButton("🗑 Убрать из обязательных", callback_data=f"chdel:{ch_id}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="admin")],
                ]
            ),
        )
        return

    if data.startswith("chbtn:"):
        ch_id = data.split(":", 1)[1]
        ch = next((c for c in load_settings()["channels"] if str(c.get("id")) == ch_id), None)
        if not ch:
            await query.answer("Канал не найден.")
            await show_admin(query, context)
            return
        pending_action[user.id] = f"rename_btn:{ch_id}"
        await query.edit_message_text(
            "✏️ Пришли новый текст кнопки.\n\n"
            f"Сейчас: <code>{subscribe_button_label(ch, 1)}</code>\n\n"
            "Например: <code>Подписаться на наш канал</code>\n"
            "Не больше 64 символов.\n\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]),
        )
        return

    if data == "checkbtn":
        pending_action[user.id] = "rename_check"
        await query.edit_message_text(
            "✏️ Пришли текст кнопки проверки подписки.\n\n"
            f"Сейчас: <code>{check_button_label()}</code>\n\n"
            "Например: <code>✅ Я подписался</code>\n"
            "Отмена: /start",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]),
        )
        return

    if data == "restart_ask":
        await query.edit_message_text(
            "♻️ Перезапустить бота сейчас?\nНа Render процесс завершится и сервис поднимется заново.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Да, рестарт", callback_data="restart_now")],
                    [InlineKeyboardButton("Отмена", callback_data="admin")],
                ]
            ),
        )
        return

    if data == "restart_now":
        await query.edit_message_text("♻️ Перезапускаю… Напиши /start через 20–40 секунд.")
        log.info("Админ %s запросил рестарт", user.username)
        await asyncio.sleep(0.4)
        os._exit(0)


async def resolve_channel(bot, parsed: dict[str, Any]) -> dict[str, Any]:
    chat_id = parsed.get("chat_id")
    if not chat_id and parsed.get("url"):
        try:
            chat = await bot.get_chat(parsed["url"])
            parsed["chat_id"] = chat.id
            parsed["title"] = chat.title or parsed.get("title")
            if chat.username:
                parsed["username"] = chat.username
                parsed["url"] = f"https://t.me/{chat.username}"
                parsed["id"] = chat.username.lower()
            else:
                parsed["id"] = str(chat.id)
            return parsed
        except TelegramError as e:
            parsed["resolve_error"] = str(e)
    chat_id = parsed.get("chat_id")
    if not chat_id:
        return parsed
    try:
        chat = await bot.get_chat(chat_id)
        parsed["title"] = chat.title or parsed.get("title") or str(chat_id)
        if chat.username:
            parsed["username"] = chat.username
            parsed["chat_id"] = "@" + chat.username
            parsed["url"] = f"https://t.me/{chat.username}"
            parsed["id"] = chat.username.lower()
        else:
            parsed["chat_id"] = chat.id
            parsed["id"] = str(chat.id)
            if not parsed.get("url"):
                try:
                    invite = await bot.export_chat_invite_link(chat.id)
                    parsed["url"] = invite
                except TelegramError:
                    pass
        return parsed
    except TelegramError as e:
        parsed["resolve_error"] = str(e)
        return parsed


async def broadcast_ad(bot, text: str, button: dict[str, str] | None) -> tuple[int, int]:
    users = [int(x) for x in (load_stats().get("users") or []) if str(x).lstrip("-").isdigit()]
    markup = None
    if button:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(button["text"], url=button["url"])]])
    ok = 0
    fail = 0
    for uid in users:
        try:
            await bot.send_message(
                uid,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=False,
            )
            ok += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.2)
            try:
                await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
                ok += 1
            except TelegramError:
                fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    user = update.effective_user
    if not user or not is_admin(user):
        return False
    action = pending_action.get(user.id)
    if not action:
        return False

    if action == "ads_text":
        label = text.strip()
        if label in {"-", "—", "нет", "удалить"}:
            label = ""
        if len(label) > 800:
            await update.message.reply_text("Слишком длинно. До 800 символов.")
            return True
        settings = load_settings()
        settings.setdefault("ads", default_settings()["ads"])["text"] = label
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html("✅ Текст рекламы сохранён.")
        await update.message.reply_html(ads_admin_text(), reply_markup=ads_admin_keyboard())
        return True

    if action == "ads_add_btn":
        parsed = parse_ad_button(text)
        if not parsed:
            await update.message.reply_html(
                "Не понял. Пришли <code>Текст | https://t.me/канал</code> или <code>@канал</code>."
            )
            return True
        settings = load_settings()
        ads = settings.setdefault("ads", default_settings()["ads"])
        buttons = list(ads.get("buttons") or [])
        buttons.append(parsed)
        ads["buttons"] = buttons[:8]
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html(
            f"✅ Добавил кнопку <b>{parsed['text']}</b>.",
            reply_markup=ads_keyboard(settings),
            disable_web_page_preview=True,
        )
        await update.message.reply_html(ads_admin_text(), reply_markup=ads_admin_keyboard())
        return True

    if action == "ads_cast":
        raw = (text or "").strip()
        if not raw:
            await update.message.reply_text("Пусто. Пришли текст рассылки.")
            return True
        lines = raw.splitlines()
        button = None
        body_lines = list(lines)
        if len(lines) >= 2:
            maybe = parse_ad_button(lines[-1])
            if maybe:
                button = maybe
                body_lines = lines[:-1]
        body = "\n".join(body_lines).strip()
        if not body:
            await update.message.reply_text("Нужен текст, не только кнопка.")
            return True
        pending_action.pop(user.id, None)
        status = await update.message.reply_text("📤 Рассылаю…")
        ok, fail = await broadcast_ad(context.bot, body, button)
        try:
            await status.edit_text(f"✅ Рассылка готова. Доставлено: {ok}, ошибок: {fail}.")
        except BadRequest:
            await update.message.reply_text(f"✅ Рассылка готова. Доставлено: {ok}, ошибок: {fail}.")
        await update.message.reply_html(ads_admin_text(), reply_markup=ads_admin_keyboard())
        return True

    if action.startswith("rename_btn:"):
        ch_id = action.split(":", 1)[1]
        label = " ".join((text or "").split())
        if not label or len(label) > 64:
            await update.message.reply_text("Текст пустой или длиннее 64 символов. Пришли короче.")
            return True
        settings = load_settings()
        found = False
        for ch in settings["channels"]:
            if str(ch.get("id")) == ch_id:
                ch["button_text"] = label
                found = True
                break
        if not found:
            pending_action.pop(user.id, None)
            await update.message.reply_text("Канал не найден.")
            return True
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html(
            f"✅ Кнопка теперь: <code>{label}</code>\nТак её видят пользователи:",
            reply_markup=subscribe_keyboard(settings["channels"]),
        )
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if action == "rename_check":
        label = " ".join((text or "").split())
        if not label or len(label) > 64:
            await update.message.reply_text("Текст пустой или длиннее 64 символов. Пришли короче.")
            return True
        settings = load_settings()
        settings["check_button"] = label
        save_settings(settings)
        pending_action.pop(user.id, None)
        await update.message.reply_html(
            f"✅ Кнопка проверки теперь: <code>{label}</code>",
            reply_markup=subscribe_keyboard(settings["channels"]),
        )
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if action != "add_channel":
        return False

    parsed = parse_channel_input(text)
    if not parsed:
        await update.message.reply_html(
            "Не понял ссылку. Пришли <code>https://t.me/канал</code> или <code>@канал</code>."
        )
        return True

    parsed = await resolve_channel(context.bot, parsed)
    settings = load_settings()
    existing_ids = {str(c.get("id")) for c in settings["channels"]}
    existing_chats = {str(c.get("chat_id")) for c in settings["channels"]}
    if str(parsed.get("id")) in existing_ids or str(parsed.get("chat_id")) in existing_chats:
        pending_action.pop(user.id, None)
        await update.message.reply_text("Этот канал уже в списке.")
        await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
        return True

    if not parsed.get("url") and parsed.get("username"):
        parsed["url"] = f"https://t.me/{parsed['username']}"

    if not parsed.get("url"):
        await update.message.reply_html(
            "Сохранил канал, но нет публичной ссылки. "
            "Пользователь не сможет нажать «Подписаться». "
            "Добавь бота админом и пришли ссылку-приглашение ещё раз."
        )

    warn = ""
    chat_id = parsed.get("chat_id")
    if chat_id:
        try:
            me = await context.bot.get_chat_member(chat_id, context.bot.id)
            if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                warn = (
                    f"\n\n⚠️ Бот не админ этого канала — проверка подписки не сработает. "
                    f"Добавь @{BOT_USERNAME} админом."
                )
        except TelegramError:
            warn = (
                f"\n\n⚠️ Не смог заглянуть в канал. Добавь бота админом "
                f"(@{BOT_USERNAME}), иначе подписку не проверить."
            )
    if not has_numeric_chat(parsed) and ("+" in str(parsed.get("url") or "") or str(parsed.get("id") or "").startswith("+")):
        warn += (
            "\n\n⚠️ Это приватный канал. Чтобы проверка заработала, "
            "<b>перешли боту любой пост из этого канала</b> "
            "(не ссылку, а именно пересланное сообщение)."
        )

    if not parsed.get("button_text"):
        parsed["button_text"] = f"Подписаться на {channel_button_title(parsed, 1)}"[:64]
    settings["channels"].append(parsed)
    save_settings(settings)
    pending_action.pop(user.id, None)
    title = channel_button_title(parsed, len(settings["channels"]))
    await update.message.reply_html(
        f"✅ Добавил <b>{title}</b>. Пользователи должны на него подписаться.{warn}\n\n"
        "Так выглядит кнопка у пользователей:",
        reply_markup=subscribe_keyboard(settings["channels"]),
        disable_web_page_preview=True,
    )
    await update.message.reply_html(admin_text(), reply_markup=admin_keyboard(settings["channels"]))
    return True


def extract_media_link(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    raw = text.strip().strip("<>")
    tik = TIKTOK_RE.search(raw)
    if tik:
        url = tik.group("url").rstrip(").,]\"'")
        if not url.lower().startswith("http"):
            url = "https://" + url
        return url, "tiktok"
    insta = INSTA_RE.search(raw)
    if insta:
        url = insta.group("url").rstrip(").,]\"'")
        if not url.lower().startswith("http"):
            url = "https://" + url
        return url, "instagram"
    return None


def media_id_from_url(url: str, platform: str) -> str:
    if platform == "tiktok":
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/photo/(\d+)", url)
        if m:
            return m.group(1)
    if platform == "instagram":
        m = re.search(r"/(?:reel|p|tv|reels)/([A-Za-z0-9_-]+)", url, re.IGNORECASE)
        if m:
            return m.group(1)
    path = urlparse(url).path.strip("/")
    return re.sub(r"[^A-Za-z0-9_-]+", "_", path)[:80] or url[-40:]


class DownloadProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "start"
        self.percent = 0
        self.title = ""

    def set(self, phase: str, percent: int, title: str | None = None) -> None:
        with self._lock:
            self.phase = phase
            self.percent = max(0, min(100, int(percent)))
            if title:
                self.title = title[:80]

    def snapshot(self) -> tuple[str, int, str]:
        with self._lock:
            return self.phase, self.percent, self.title


def progress_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(percent / 10)
    return "█" * filled + "░" * (10 - filled)


def format_progress(prog: DownloadProgress) -> str:
    phase, percent, title = prog.snapshot()
    labels = {
        "start": "Готовлю",
        "convert": "Собираю файл",
        "download": "Скачиваю",
        "send": "Отправляю",
    }
    line = f"⬇️ {labels.get(phase, 'Качаю')} {progress_bar(percent)} {percent}%"
    if title:
        return f"{line}\n🎬 {title}"
    return line


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 25,
) -> dict[str, Any]:
    hdrs = {"User-Agent": HTTP_UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, headers=hdrs, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8", "replace"))
    return parsed if isinstance(parsed, dict) else {}


def http_download(
    url: str,
    dest: Path,
    timeout: int = 90,
    progress: DownloadProgress | None = None,
    extra_headers: dict | None = None,
) -> None:
    hdrs = {"User-Agent": HTTP_UA, "Accept": "*/*"}
    if extra_headers:
        hdrs.update(extra_headers)
    req = Request(url, headers=hdrs)
    limit = MAX_FILE_MB * 1024 * 1024
    with urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        n = 0
        last_pct = -1
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"Файл слишком большой. Лимит Telegram — {MAX_FILE_MB} МБ.")
            out.write(chunk)
            if progress:
                if total > 0:
                    pct = 20 + int(70 * n / total)
                else:
                    pct = min(90, 20 + n // 400_000)
                if pct != last_pct:
                    progress.set("download", pct)
                    last_pct = pct
    if not dest.exists() or dest.stat().st_size < 2000:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Пустой файл, источник не отдал видео.")
    if progress:
        progress.set("download", 92)


def resolve_redirect(url: str, timeout: int = 15) -> str:
    try:
        req = Request(url, headers={"User-Agent": HTTP_UA}, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            return resp.geturl() or url
    except Exception:
        return url


def find_file(folder: Path, exts: set[str]) -> Path | None:
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[0]


def probe_video(path: Path) -> dict[str, int]:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {"width": 0, "height": 0, "duration": 0}
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = data.get("format") or {}
    try:
        duration = float(stream.get("duration") or fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": int(duration or 0),
    }


def ensure_mp4(src: Path, workdir: Path, progress: DownloadProgress | None = None) -> Path:
    if src.suffix.lower() == ".mp4" and src.stat().st_size >= 2000:
        return src
    if progress:
        progress.set("convert", 94)
    dest = workdir / "video.mp4"
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    copy_cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(copy_cmd, capture_output=True, timeout=90)
    if proc.returncode == 0 and dest.exists() and dest.stat().st_size >= 2000:
        return dest
    dest.unlink(missing_ok=True)
    re_cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(re_cmd, capture_output=True, timeout=180)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size < 2000:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"Не смог собрать MP4. {err}".strip())
    return dest


def cookies_file() -> str | None:
    env_path = os.getenv("MEDIA_COOKIES_FILE", "").strip() or os.getenv("YT_COOKIES_FILE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    bundled = DATA_DIR / "cookies.txt"
    if bundled.exists() and bundled.stat().st_size > 20:
        return str(bundled)
    raw = os.getenv("MEDIA_COOKIES", "").strip() or os.getenv("YT_COOKIES", "").strip()
    if raw:
        tmp = DATA_DIR / "cookies.env.txt"
        tmp.write_text(raw.replace("\\n", "\n"), encoding="utf-8")
        return str(tmp)
    return None


def result_from_file(
    path: Path,
    *,
    title: str,
    author: str,
    platform: str,
    media_id: str,
    webpage_url: str,
    duration: int = 0,
) -> dict[str, Any]:
    probe = probe_video(path)
    return {
        "path": path,
        "title": (title or "Видео").strip(),
        "author": (author or platform).strip(),
        "duration": duration or probe["duration"],
        "width": probe["width"],
        "height": probe["height"],
        "platform": platform,
        "id": media_id,
        "webpage_url": webpage_url,
    }


def download_via_tikwm(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any] | None:
    if progress:
        progress.set("start", 8)
    api = "https://www.tikwm.com/api/?hd=1&url=" + quote(url, safe="")
    try:
        data = http_json("GET", api, headers={"Referer": "https://www.tikwm.com/"}, timeout=25)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("tikwm: %s", e)
        return None
    if int(data.get("code") or -1) != 0:
        log.warning("tikwm отказ: %s", data.get("msg") or data)
        return None
    info = data.get("data") if isinstance(data.get("data"), dict) else {}
    video_url = info.get("hdplay") or info.get("play")
    if not video_url or "watermark" in str(video_url).lower():
        return None
    title = (info.get("title") or "TikTok").strip()
    author_info = info.get("author") if isinstance(info.get("author"), dict) else {}
    author = (author_info.get("nickname") or author_info.get("unique_id") or "TikTok").strip()
    if progress:
        progress.set("download", 15, title)
    dest = workdir / "tikwm.mp4"
    http_download(video_url, dest, progress=progress, extra_headers={"Referer": "https://www.tikwm.com/"})
    duration = int(info.get("duration") or 0)
    if duration and duration > MAX_DURATION_SEC:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Видео длиннее {MAX_DURATION_SEC // 60} минут.")
    return result_from_file(
        dest,
        title=title,
        author=author,
        platform="tiktok",
        media_id=str(info.get("id") or media_id_from_url(url, "tiktok")),
        webpage_url=url,
        duration=duration,
    )


def download_via_cobalt(url: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any] | None:
    instances = (
        "https://api.cobalt.tools/",
        "https://cobalt-api.hyper.lol/",
    )
    payload = {
        "url": url,
        "videoQuality": "max",
        "filenameStyle": "basic",
        "downloadMode": "auto",
        "tiktokFullAudio": False,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": HTTP_UA,
    }
    last_err = ""
    for base in instances:
        try:
            if progress:
                progress.set("start", 10)
            data = http_json("POST", base, payload, headers=headers, timeout=20)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            log.warning("cobalt %s: %s", base, e)
            continue
        status = str(data.get("status") or "")
        if status == "error":
            last_err = str((data.get("error") or {}).get("code") or data)
            continue
        file_url = data.get("url")
        if status in {"redirect", "tunnel", "stream"} and file_url:
            dest = workdir / "cobalt.bin"
            if progress:
                progress.set("download", 18, str(data.get("filename") or ""))
            http_download(file_url, dest, progress=progress)
            video = ensure_mp4(dest, workdir, progress)
            return result_from_file(
                video,
                title=Path(str(data.get("filename") or "video")).stem,
                author="video",
                platform="tiktok" if "tiktok" in url.lower() else "instagram",
                media_id=media_id_from_url(url, "tiktok" if "tiktok" in url.lower() else "instagram"),
                webpage_url=url,
            )
        picker = data.get("picker") if isinstance(data.get("picker"), list) else []
        videos = [p for p in picker if isinstance(p, dict) and p.get("url") and str(p.get("type") or "") != "photo"]
        if videos:
            dest = workdir / "cobalt.bin"
            http_download(videos[0]["url"], dest, progress=progress)
            video = ensure_mp4(dest, workdir, progress)
            return result_from_file(
                video,
                title="Instagram",
                author="Instagram",
                platform="instagram",
                media_id=media_id_from_url(url, "instagram"),
                webpage_url=url,
            )
    if last_err:
        log.warning("cobalt fail: %s", last_err)
    return None


def download_via_ytdlp(
    url: str,
    platform: str,
    workdir: Path,
    progress: DownloadProgress | None = None,
) -> dict[str, Any] | None:
    import yt_dlp
    from yt_dlp.utils import DownloadError, YoutubeDLError

    def match_filter(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "Это прямой эфир, скачать нельзя"
        duration = info.get("duration")
        if duration and int(duration) > MAX_DURATION_SEC:
            return f"Видео длиннее {MAX_DURATION_SEC // 60} минут"
        return None

    outtmpl = str(workdir / "ytdlp.%(ext)s")
    if platform == "tiktok":
        fmt = "download/download_addr/h264_540p_1/best[ext=mp4]/best"
    else:
        fmt = "best[ext=mp4]/bestvideo*+bestaudio/best"
    opts: dict[str, Any] = {
        "format": fmt,
        "outtmpl": {"default": outtmpl},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "overwrites": True,
        "cachedir": False,
        "retries": 1,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "socket_timeout": 15,
        "geo_bypass": True,
        "merge_output_format": "mp4",
        "match_filter": match_filter,
    }
    cookie = cookies_file()
    if cookie:
        opts["cookiefile"] = cookie
    if progress:
        def _hook(event: dict[str, Any]) -> None:
            if event.get("status") != "downloading":
                return
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            got = event.get("downloaded_bytes") or 0
            if total:
                progress.set("download", int(100 * got / total), str(event.get("info_dict", {}).get("title") or ""))

        opts["progress_hooks"] = [_hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next((e for e in info["entries"] if e), {}) or {}
    except (DownloadError, YoutubeDLError) as e:
        log.warning("yt-dlp: %s", e)
        raise RuntimeError(str(e)) from e
    video = find_file(workdir, {".mp4", ".webm", ".mkv", ".mov", ".m4v"})
    if not video:
        return None
    video = ensure_mp4(video, workdir, progress)
    return result_from_file(
        video,
        title=(info.get("title") or platform).strip(),
        author=(info.get("uploader") or info.get("channel") or info.get("creator") or platform).strip(),
        platform=platform,
        media_id=str(info.get("id") or media_id_from_url(url, platform)),
        webpage_url=info.get("webpage_url") or url,
        duration=int(info.get("duration") or 0),
    )


def humanize_error(msg: str) -> str:
    low = (msg or "").lower()
    if "too long" in low or "длиннее" in low:
        return f"Видео длиннее {MAX_DURATION_SEC // 60} минут."
    if "live" in low or "эфир" in low:
        return "Это прямой эфир — скачать нельзя."
    if "login" in low or "cookies" in low or "not available" in low or "private" in low:
        return "Ролик приватный или недоступен без входа. Попробуй публичную ссылку."
    if "unavailable" in low or "removed" in low or "удалено" in low:
        return "Это видео недоступно (удалено, приватное или заблокировано)."
    if "photo" in low and "video" in low:
        return "Это фото, не видео."
    if "http error 403" in low or "blocked" in low:
        return "Сервер временно не отдал файл. Попробуй ещё раз через минуту."
    if "слишком большой" in low:
        return msg
    return "Не смог скачать это видео. Проверь ссылку и попробуй ещё раз."


def download_video(url: str, platform: str, workdir: Path, progress: DownloadProgress | None = None) -> dict[str, Any]:
    if platform == "tiktok" and any(p in url.lower() for p in ("vm.tiktok.com", "vt.tiktok.com", "tiktok.com/t/")):
        url = resolve_redirect(url)
        platform = "tiktok"
    last_err = ""
    backends: list[tuple[str, Any]] = []
    if platform == "tiktok":
        backends.append(("tikwm", lambda: download_via_tikwm(url, workdir / "tikwm", progress)))
    backends.append(("cobalt", lambda: download_via_cobalt(url, workdir / "cobalt", progress)))
    backends.append(("yt-dlp", lambda: download_via_ytdlp(url, platform, workdir / "ytdlp", progress)))
    if platform == "instagram":
        backends.append(("tikwm", lambda: download_via_tikwm(url, workdir / "tikwm2", progress)))

    for name, fn in backends:
        dest = workdir / name
        dest.mkdir(parents=True, exist_ok=True)
        try:
            got = fn()
            if got and got.get("path") and Path(got["path"]).exists():
                log.info("Скачал через %s: %s", name, got.get("title"))
                return got
        except RuntimeError as e:
            last_err = str(e)
            log.warning("%s: %s", name, e)
            if "длиннее" in last_err:
                raise
        except Exception as e:
            last_err = str(e)
            log.warning("%s unexpected: %s", name, e)
    raise RuntimeError(humanize_error(last_err) if last_err else "Не получилось скачать. Попробуй другую ссылку.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = msg.text or msg.caption or ""

    if is_start_text(text):
        await cmd_start(update, context)
        return
    if is_admin_btn_text(text):
        await cmd_admin(update, context)
        return

    if await handle_admin_text(update, context, text):
        return

    found = extract_media_link(text)
    if not found:
        if is_admin(user) and pending_action.get(user.id) in {
            "add_channel",
            "ads_text",
            "ads_add_btn",
            "ads_cast",
        }:
            return
        await msg.reply_html(
            "Пришли ссылку на TikTok или Instagram.\n"
            "Например: <code>https://www.tiktok.com/@user/video/123</code>\n"
            "или <code>https://www.instagram.com/reel/XXXX</code>"
        )
        return

    url, platform = found
    if not await ensure_subscribed(update, context):
        return

    key = cache_key(platform, media_id_from_url(url, platform))
    cached = get_cached_video(key)
    settings = load_settings()
    if cached:
        try:
            kwargs: dict[str, Any] = {
                "video": cached["file_id"],
                "caption": video_caption(cached.get("title") or "Видео", cached.get("author") or "", int(cached.get("duration") or 0), settings),
                "supports_streaming": True,
            }
            if cached.get("duration"):
                kwargs["duration"] = int(cached["duration"])
            if cached.get("width"):
                kwargs["width"] = int(cached["width"])
            if cached.get("height"):
                kwargs["height"] = int(cached["height"])
            markup = ads_keyboard(settings) if (settings.get("ads") or {}).get("enabled_after_download") else None
            if markup:
                kwargs["reply_markup"] = markup
            await msg.reply_video(**kwargs)
            bump_stats(user.id)
            return
        except TelegramError:
            log.warning("Кэш file_id не сработал для %s — качаю заново", key)
            drop_cached_video(key)

    lock = get_lock(user.id)
    if lock.locked():
        await msg.reply_text("Уже качаю твоё предыдущее видео. Подожди немного.")
        return

    async with lock:
        progress = DownloadProgress()
        progress.set("start", 2)
        status = await msg.reply_text(format_progress(progress))
        workdir = Path(tempfile.mkdtemp(prefix="ttig_", dir=str(DATA_DIR)))
        try:
            dl_task = asyncio.create_task(asyncio.to_thread(download_video, url, platform, workdir, progress))
            last_text = ""
            deadline = time.time() + 180
            while not dl_task.done():
                if time.time() > deadline:
                    dl_task.cancel()
                    raise asyncio.TimeoutError
                shown = format_progress(progress)
                if shown != last_text:
                    try:
                        await status.edit_text(shown)
                    except BadRequest:
                        pass
                    last_text = shown
                await asyncio.sleep(1)
            result = dl_task.result()
            progress.set("send", 97, result.get("title") or "")
            try:
                await status.edit_text(format_progress(progress))
            except BadRequest:
                pass

            video_path = Path(result["path"])
            size_mb = video_path.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                raise RuntimeError(f"Файл слишком большой ({size_mb:.0f} МБ). Лимит Telegram — {MAX_FILE_MB} МБ.")

            caption = video_caption(result.get("title") or "Видео", result.get("author") or "", int(result.get("duration") or 0), settings)
            markup = ads_keyboard(settings) if (settings.get("ads") or {}).get("enabled_after_download") else None
            send_kwargs: dict[str, Any] = {
                "video": video_path.open("rb"),
                "filename": f"{safe_filename(result.get('title') or 'video')}.mp4",
                "caption": caption,
                "supports_streaming": True,
            }
            if result.get("duration"):
                send_kwargs["duration"] = int(result["duration"])
            if result.get("width"):
                send_kwargs["width"] = int(result["width"])
            if result.get("height"):
                send_kwargs["height"] = int(result["height"])
            if markup:
                send_kwargs["reply_markup"] = markup
            try:
                sent = await msg.reply_video(**send_kwargs)
            finally:
                try:
                    send_kwargs["video"].close()
                except Exception:
                    pass

            video_obj = getattr(sent, "video", None)
            mid = result.get("id") or media_id_from_url(url, platform)
            if mid and video_obj and video_obj.file_id:
                save_cached_video(
                    cache_key(platform, str(mid)),
                    {
                        "file_id": video_obj.file_id,
                        "title": result.get("title") or "Видео",
                        "author": result.get("author") or "",
                        "duration": video_obj.duration or result.get("duration") or 0,
                        "width": video_obj.width or result.get("width") or 0,
                        "height": video_obj.height or result.get("height") or 0,
                        "filename": f"{safe_filename(result.get('title') or 'video')}.mp4",
                    },
                )
            bump_stats(user.id)
            try:
                await status.delete()
            except TelegramError:
                pass
        except asyncio.TimeoutError:
            await status.edit_text("⏱ Слишком долго качается. Попробуй другое видео или ещё раз.")
        except RuntimeError as e:
            try:
                await status.edit_text(f"❌ {e}")
            except BadRequest:
                await msg.reply_text(f"❌ {e}")
        except TelegramError as e:
            log.exception("Telegram send failed")
            try:
                await status.edit_text(f"❌ Не смог отправить файл: {e}")
            except BadRequest:
                pass
        except Exception:
            log.exception("Download failed")
            try:
                await status.edit_text("❌ Ошибка при скачивании. Попробуй другую ссылку.")
            except BadRequest:
                pass
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def handle_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.document or not is_admin(user):
        return
    name = (msg.document.file_name or "").lower()
    if "cookie" not in name and name != "cookies.txt":
        await msg.reply_text("Нужен файл cookies.txt (Netscape), если Instagram режет сервер.")
        return
    if msg.document.file_size and msg.document.file_size > 2_000_000:
        await msg.reply_text("Файл слишком большой.")
        return
    tg_file = await msg.document.get_file()
    dest = DATA_DIR / "cookies.txt"
    await tg_file.download_to_drive(custom_path=str(dest))
    raw = dest.read_text(encoding="utf-8", errors="replace")
    if "# Netscape" not in raw and "instagram.com" not in raw and "tiktok.com" not in raw:
        dest.unlink(missing_ok=True)
        await msg.reply_text("Это не cookies. Экспортируй cookies.txt расширением Get cookies.txt LOCALLY.")
        return
    await msg.reply_text("Сохранил cookies. Сложные ролики Instagram можно пробовать ещё раз.")


def _member_counts_as_in(member) -> bool:
    if member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", True))
    return False


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.chat_member
    if not event:
        return
    ch = find_required_channel_for_chat(event.chat)
    if not ch:
        return
    new = event.new_chat_member
    old = event.old_chat_member
    person = new.user
    if person.is_bot:
        return
    was = _member_counts_as_in(old)
    now = _member_counts_as_in(new)
    if now == was:
        return
    name = " ".join(x for x in (person.first_name, person.last_name) if x).strip()
    record_subscription(
        user_id=person.id,
        username=person.username or "",
        name=name,
        channel_key=channel_store_key(ch),
        channel_title=channel_button_title(ch, 1),
        action="join" if now else "leave",
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.my_chat_member
    if not event:
        return
    chat = event.chat
    new = event.new_chat_member
    if new.user.id != context.bot.id:
        return
    if new.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP):
        return
    bound = bind_known_chat(chat)
    log.info("Бот стал админом %s (%s)", chat.title, chat.id)
    invite = bound.get("url") or ""
    if not invite:
        try:
            invite = await context.bot.export_chat_invite_link(chat.id)
            settings = load_settings()
            for ch in settings["channels"]:
                if str(ch.get("chat_id")) == str(chat.id):
                    ch["url"] = invite
            save_settings(settings)
        except TelegramError as e:
            log.warning("Не смог сделать инвайт для %s: %s", chat.id, e)


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    if is_admin(user):
        origin = getattr(msg, "forward_origin", None)
        chat = None
        if isinstance(origin, MessageOriginChannel):
            chat = origin.chat
        elif getattr(msg, "forward_from_chat", None):
            chat = msg.forward_from_chat
        if chat and getattr(chat, "type", "") in {"channel", "supergroup", ""}:
            bound = bind_known_chat(chat, invite_url=channel_public_url({"url": "", "username": getattr(chat, "username", "")}))
            pending_action.pop(user.id, None)
            await msg.reply_html(
                f"✅ Привязал канал <b>{bound.get('title') or chat.id}</b>.\n"
                f"id: <code>{bound.get('chat_id')}</code>\n"
                "Теперь проверку подписки можно делать.\n\n"
                "Так выглядит кнопка у пользователей:",
                reply_markup=subscribe_keyboard(load_settings()["channels"]),
                disable_web_page_preview=True,
            )
            return

    await handle_link(update, context)


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _ok(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/health", "/api/ping", "/api/health"):
            self._ok(
                {
                    "ok": True,
                    "service": "tiktokinsta",
                    "uptime_sec": int(time.time() - STARTED_AT),
                }
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/health", "/api/ping", "/api/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def start_http() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    thread.start()
    log.info("HTTP health on :%s", PORT)


def main() -> None:
    if not BOT_TOKEN:
        print("Нет BOT_TOKEN. Пропиши его в .env, токен3.txt или в переменных Render.", file=sys.stderr)
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg не найден в PATH — сборка MP4 может не сработать")

    async def _post_init(application: Application) -> None:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Старт"),
                BotCommand("help", "Как скачать"),
                BotCommand("admin", "Админка"),
            ]
        )

    start_http()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_file))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forward))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.Entity("url") | filters.CaptionEntity("url"), handle_link))

    def _stop(*_args) -> None:
        log.info("stop signal")

    signal.signal(signal.SIGTERM, _stop)
    chans = load_settings().get("channels") or []
    log.info(
        "Бот запущен. Админы: %s. Каналы: %s",
        ", ".join(sorted(ADMIN_USERNAMES)) or "—",
        ", ".join(channel_public_url(c) or str(c.get("id")) for c in chans) or "нет",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
