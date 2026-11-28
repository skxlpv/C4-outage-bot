"""
Chernivtsioblenergo Web Replica & Tracker Telegram Bot (Render / HF Edition)

- Replicates the oblenergo schedule table as an image
- Pushes an updated table to all subscribers AND channels as soon as the
  schedule on the site changes (no button press needed)
- Also tracks tomorrow's schedule (site link '?next'): announces it when published,
  re-posts it when it changes, and shows it on request (/tomorrow)
- Channels: add the bot as administrator (Post Messages) and it posts the
  current table immediately, then re-posts on every change.

State persistence (Render's disk is ephemeral):
  Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (free Upstash Redis)
  and subscribers, channels and the last seen schedule survive restarts.
  Channels can additionally be pinned via CHANNEL_IDS="-100123...,-100456..."
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
import gradio as gr
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from configs import LOGO_C4_B64 as logo

load_dotenv()

# =========================================================================== #
# Configuration & Constants
# =========================================================================== #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUBSCRIBERS_FILE = Path(os.environ.get("SUBSCRIBERS_FILE", "subscribers.json"))
CHANNELS_FILE = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OBLENERGO_URL = os.environ.get("OBLENERGO_URL", "https://oblenergo.cv.ua/shutdowns/")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", "15"))
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
TZ = ZoneInfo("Europe/Kyiv")
BOT_STARTED_AT = datetime.now(TZ)

CONFLICT_COUNT = 0
LAST_CONFLICT_AT: Optional[datetime] = None

# Scraper Headers from cURL
BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "uk,en;q=0.9,en-GB;q=0.8,en-US;q=0.7,uk-UA;q=0.6",
    "cache-control": "max-age=0",
    "dnt": "1",
    "priority": "u=0, i",
    "sec-ch-ua": '"Not-A.Brand";v="99", "Microsoft Edge";v="157", "Chromium";v="157"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0",
}

BROWSER_COOKIES = {
    "hlgrp": "0",
}

# Visual Colors
COLOR_CARD_BG = (255, 255, 255)
COLOR_DATE_BADGE = (240, 236, 225)
COLOR_GREEN_BANNER = (119, 136, 66)  # #778842
COLOR_HOURS_BG = (245, 206, 172)     # #f5ceac
COLOR_ON = (199, 211, 159)           # #c7d39f
COLOR_OFF = (220, 116, 99)           # #dc7463
COLOR_MAYBE = (176, 190, 197)        # #b0bec5
COLOR_BORDER = (255, 255, 255)
COLOR_DARK_TEXT = (33, 37, 41)
COLOR_MUTED_GRAY = (120, 144, 156)
COLOR_ACTIVE_HOUR = (40, 63, 159)    # #283f9f
COLOR_ACTIVE_DOTS = (240, 244, 255)

# Render at 3840x1760 (1.5x supersampled) and downsample to 2560x1174
RENDER_W = 3840
RENDER_H = 1760
OUT_W = 2560
OUT_H = 1174
IMG_SCALE = RENDER_W / 1440  # 8/3

UK_MONTHS_GENITIVE = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full Telegram URLs (which contain the bot token) at INFO level
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("svitlo-replica-bot")


def _s(val: float) -> int:
    return int(round(val * IMG_SCALE))


def format_uk_date(date_str: str) -> str:
    """Convert '27.11.2026' -> '27 листопада'."""
    s = (date_str or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return f"{dt.day} {UK_MONTHS_GENITIVE[dt.month]}"
        except ValueError:
            pass
    parts = s.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        day = int(parts[0])
        month = int(parts[1])
        if month in UK_MONTHS_GENITIVE:
            return f"{day} {UK_MONTHS_GENITIVE[month]}"
    return s


# =========================================================================== #
# Remote persistence (Upstash Redis REST) - optional
# =========================================================================== #
class RemoteKV:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.enabled = bool(url and token)
        self.last_error: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=8),
            )
        return self._session

    async def _cmd(self, *args):
        s = await self._get_session()
        async with s.post(self.url, json=list(args)) as r:
            data = await r.json()
            if r.status >= 400 or data.get("error"):
                raise RuntimeError(data.get("error") or f"HTTP {r.status}")
            self.last_error = None
            return data.get("result")

    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            return await self._cmd("GET", key)
        except Exception as e:
            self.last_error = str(e)
            log.warning("Remote GET %s failed: %s", key, e)
            return None

    async def set(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        try:
            await self._cmd("SET", key, value)
        except Exception as e:
            self.last_error = str(e)
            log.warning("Remote SET %s failed: %s", key, e)

    async def ping(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "вимкнено (змінні UPSTASH не задані)"
        try:
            res = await self._cmd("PING")
            return True, f"підключено ({res})"
        except Exception as e:
            self.last_error = str(e)
            return False, f"помилка: {e}"


remote = RemoteKV(UPSTASH_URL, UPSTASH_TOKEN)


def _fire_and_forget(coro) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()  # no running loop (e.g. at import time)


# =========================================================================== #
# Persistence (Subscribers & Channels)
# =========================================================================== #
class SubscriberStore:
    """Persistent set of chat IDs (users have positive IDs, channels negative)."""

    def __init__(self, path: Path, remote_key: str) -> None:
        self.path = path
        self.remote_key = remote_key
        self.subscribers: set[int] = set()
        self._save_lock: Optional[asyncio.Lock] = None
        self.load()

    def _get_lock(self) -> asyncio.Lock:
        if self._save_lock is None:
            self._save_lock = asyncio.Lock()
        return self._save_lock

    def _merge(self, data) -> None:
        for uid in data:
            try:
                self.subscribers.add(int(uid))
            except (TypeError, ValueError):
                pass

    def load(self) -> None:
        if self.path.exists():
            try:
                self._merge(json.loads(self.path.read_text(encoding="utf-8")))
                log.info("Loaded %d entries from %s.", len(self.subscribers), self.path)
            except Exception as e:
                log.warning("Could not read %s: %s", self.path, e)

    async def load_remote(self) -> None:
        raw = await remote.get(self.remote_key)
        if raw:
            try:
                self._merge(json.loads(raw))
                log.info("Loaded %s from remote: now %d entries.", self.remote_key, len(self.subscribers))
            except Exception as e:
                log.warning("Bad remote data for %s: %s", self.remote_key, e)

    def _write_local(self, payload: str) -> None:
        try:
            self.path.write_text(payload, encoding="utf-8")
        except Exception as e:
            log.warning("Failed saving %s: %s", self.path, e)

    async def save_async(self) -> None:
        async with self._get_lock():
            payload = json.dumps(sorted(self.subscribers))
            self._write_local(payload)
            if remote.enabled:
                await remote.set(self.remote_key, payload)

    async def add_async(self, uid: int) -> bool:
        if uid not in self.subscribers:
            self.subscribers.add(uid)
            await self.save_async()
            return True
        return False

    async def remove_async(self, uid: int) -> None:
        if uid in self.subscribers:
            self.subscribers.discard(uid)
            await self.save_async()

    def add(self, uid: int) -> bool:
        if uid not in self.subscribers:
            self.subscribers.add(uid)
            self.save()
            return True
        return False

    def remove(self, uid: int) -> None:
        if uid in self.subscribers:
            self.subscribers.discard(uid)
            self.save()

    def save(self) -> None:
        payload = json.dumps(sorted(self.subscribers))
        self._write_local(payload)
        if remote.enabled:
            _fire_and_forget(remote.set(self.remote_key, payload))


subscribers = SubscriberStore(SUBSCRIBERS_FILE, "oblenergo:subscribers")

channels = SubscriberStore(CHANNELS_FILE, "oblenergo:channels")
# Channels can also be pinned via env: CHANNEL_IDS="-1001234567890,-100987..."
PINNED_CHANNELS: set[int] = set()
for _cid in os.environ.get("CHANNEL_IDS", "").split(","):
    _cid = _cid.strip()
    if _cid.lstrip("-").isdigit():
        PINNED_CHANNELS.add(int(_cid))
channels.subscribers |= PINNED_CHANNELS


# =========================================================================== #
# Website Scraper Model
# =========================================================================== #
@dataclass
class ScrapedSchedule:
    date_str: str  # e.g. "18.11.2026"
    upd_time: str  # e.g. "07:51"
    month_name: str  # e.g. "Листопад"
    slots: dict[int, list[str]]  # group_id (1..12) -> 48 slots ("з", "в", "мз")
    html_hash: str
    active_slot: int = -1  # 0..47
    start_slot: int = 0    # first slot index actually present in HTML (0..48)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "ScrapedSchedule":
        d = json.loads(raw)
        d["slots"] = {int(k): v for k, v in d["slots"].items()}
        d.setdefault("start_slot", 0)
        valid_fields = ScrapedSchedule.__dataclass_fields__
        return ScrapedSchedule(**{k: v for k, v in d.items() if k in valid_fields})


def schedule_changed(old: ScrapedSchedule, new: ScrapedSchedule) -> bool:
    """True only if the schedule date or any upcoming/future slot changed.

    - Ignores 'Станом на' (upd_time), because the site updates it every 30 min
      even when the schedule itself has not changed.
    - Ignores elapsed/past half-hour slots and any leading cells stripped by
      the site as time passes, comparing only upcoming slots actually present.
    """
    if old.date_str != new.date_str:
        return True

    # Never compare slots that were padded/preserved before start_slot
    start = max(old.start_slot, new.start_slot, 0)

    # For today's table (active_slot >= 0), also skip past half-hour slots plus
    # a 60-second boundary buffer so slight clock skew around :00 / :30 cannot
    # cause false positives.
    if old.active_slot >= 0 or new.active_slot >= 0:
        now = datetime.now(TZ)
        now_buf = now + timedelta(seconds=60)
        buf_slot = 48 if now_buf.date() != now.date() else (
            now_buf.hour * 2 + (1 if now_buf.minute >= 30 else 0)
        )
        start = max(start, old.active_slot, new.active_slot, buf_slot)

    if start >= 48:
        return False

    for q in set(old.slots) | set(new.slots):
        if old.slots.get(q, [])[start:] != new.slots.get(q, [])[start:]:
            return True
    return False


@dataclass
class FetchResult:
    today: Optional[ScrapedSchedule]
    tomorrow: Optional[ScrapedSchedule]  # None = not published (when tomorrow_ok) or unknown
    tomorrow_ok: bool  # False if tomorrow could not be checked (network/HTTP error)


class OblenergoScraper:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_schedule: Optional[ScrapedSchedule] = None
        self.tomorrow_schedule: Optional[ScrapedSchedule] = None
        self.tomorrow_initialized = False  # becomes True after the first successful tomorrow check
        self.last_fetch_error: Optional[str] = None
        self.last_fetch_at: Optional[datetime] = None
        self.last_update_at: Optional[datetime] = None
        self.fetch_ok_count: int = 0
        self.fetch_err_count: int = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=BROWSER_HEADERS,
                cookies=BROWSER_COOKIES,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def _get_html(self, url: str) -> Optional[str]:
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.last_fetch_error = f"HTTP {resp.status}"
                    log.warning("Site returned status %s for %s", resp.status, url)
                    return None
                text = await resp.text()
                self.last_fetch_error = None
                return text
        except Exception as e:
            self.last_fetch_error = str(e)
            log.warning("Scrape error: %s", e)
            return None

    @staticmethod
    def find_tomorrow_link(html_text: str) -> Optional[tuple[str, str]]:
        """The site shows a link to tomorrow's date next to today's date once it is published."""
        soup = BeautifulSoup(html_text, "html.parser")
        a = soup.select_one("#gsv_t a")
        if not a:
            return None
        date_txt = a.get_text().strip()
        if not date_txt:
            return None
        href = (a.get("href") or "").strip()
        url = urljoin(OBLENERGO_URL, href) if href else OBLENERGO_URL.rstrip("/") + "/?next"
        return date_txt, url

    async def fetch_all(self) -> FetchResult:
        self.last_fetch_at = datetime.now(TZ)
        html_text = await self._get_html(OBLENERGO_URL)
        if html_text is None:
            self.fetch_err_count += 1
            return FetchResult(None, None, False)

        today = self.parse_html(html_text, prev_schedule=self.last_schedule)
        if today is None:
            self.fetch_err_count += 1
            return FetchResult(None, None, False)

        link = self.find_tomorrow_link(html_text)
        if link is None:
            self.fetch_ok_count += 1
            return FetchResult(today, None, True)

        date_txt, url = link
        t_html = await self._get_html(url)
        if t_html is None:
            self.fetch_err_count += 1
            return FetchResult(today, None, False)

        tomorrow = self.parse_html(t_html, prev_schedule=self.tomorrow_schedule)
        self.last_fetch_error = None  # a missing tomorrow table is not an error
        self.fetch_ok_count += 1
        if tomorrow is None or tomorrow.date_str != date_txt or tomorrow.date_str == today.date_str:
            log.info("Tomorrow link present (%s) but no valid table behind it", date_txt)
            return FetchResult(today, None, True)

        tomorrow.active_slot = -1  # no "current hour" marker on tomorrow's table
        return FetchResult(today, tomorrow, True)

    def parse_html(
        self,
        html_text: str,
        prev_schedule: Optional[ScrapedSchedule] = None,
    ) -> Optional[ScrapedSchedule]:
        soup = BeautifulSoup(html_text, "html.parser")
        gsv = soup.select_one("#gsv")
        if not gsv:
            self.last_fetch_error = "Schedule block #gsv not found on page"
            return None

        # 1. Date
        date_el = soup.select_one("#gsv_t b") or soup.select_one("#gsv ul p")
        date_str = date_el.get_text().strip() if date_el else datetime.now(TZ).strftime("%d.%m.%Y")

        # 2. Update time ("Станом на HH:MM")
        upd_el = soup.select_one("#gsv_a b")
        upd_time = upd_el.get_text().strip() if upd_el else "--:--"

        # 3. Month header
        month_el = soup.select_one("#gsv_h p")
        month_name = month_el.get_text().strip() if month_el else "Листопад"

        # 4. Extract slots for all 12 groups
        group_slots: dict[int, list[str]] = {}
        max_start_offset = 0
        for q in range(1, 13):
            div = soup.select_one(f"#inf{q}") or soup.select_one(f'div[data-id="{q}"]')
            if not div:
                continue

            cells = div.find_all(recursive=False)
            q_slots: list[str] = []

            for cell in cells:
                txt = cell.get_text().strip().lower()
                tag = cell.name.lower()
                if "мз" in txt or tag == "s":
                    q_slots.append("мз")
                elif "в" in txt or tag == "o":
                    q_slots.append("в")
                elif "з" in txt or tag == "u":
                    q_slots.append("з")
                else:
                    q_slots.append("з")

            # As the day progresses, the site removes elapsed cells.
            # Preserve earlier slots from prev_schedule (same date).
            if len(q_slots) < 48:
                start_offset = 48 - len(q_slots)
                max_start_offset = max(max_start_offset, start_offset)
                if (
                    prev_schedule is not None
                    and prev_schedule.date_str == date_str
                    and q in prev_schedule.slots
                ):
                    prefix = list(prev_schedule.slots[q][:start_offset])
                    if len(prefix) < start_offset:
                        prefix.extend(["з"] * (start_offset - len(prefix)))
                else:
                    prefix = ["з"] * start_offset
                q_slots = prefix + q_slots
            group_slots[q] = q_slots[:48]

        if not group_slots:
            self.last_fetch_error = "No group rows found on page"
            return None

        now = datetime.now(TZ)
        active_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)

        # Hash only date + slots (not upd_time, which changes every 30 min on its own)
        content_repr = f"{date_str}_{json.dumps(group_slots, sort_keys=True)}"
        content_hash = hashlib.md5(content_repr.encode()).hexdigest()

        return ScrapedSchedule(
            date_str=date_str,
            upd_time=upd_time,
            month_name=month_name,
            slots=group_slots,
            html_hash=content_hash,
            active_slot=active_slot,
            start_slot=max_start_offset,
        )


scraper = OblenergoScraper()

# =========================================================================== #
# Replica Image Generator (Pillow)
# =========================================================================== #
LOGO_C4_B64 = logo

WATERMARK_TEXT = "Чернівці | С4"
WATERMARK_ALPHA = 24  # 0..255

_logo_cache: dict[int, Image.Image] = {}


def get_logo(diameter: int) -> Optional[Image.Image]:
    if diameter in _logo_cache:
        return _logo_cache[diameter]
    if not LOGO_C4_B64:
        return None
    try:
        raw = Image.open(io.BytesIO(base64.b64decode(LOGO_C4_B64))).convert("RGBA")
        _logo_cache[diameter] = raw.resize((diameter, diameter), Image.LANCZOS)
        return _logo_cache[diameter]
    except Exception as e:
        log.warning("Logo load failed: %s", e)
        return None


def apply_watermark(base: Image.Image, text: str, alpha: int, center_y: Optional[int] = None) -> Image.Image:
    """Overlay a large, horizontal, semi-transparent text centered on the image."""
    w, h = base.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    wm_font = load_font(_s(96), bold=True)
    cy = center_y if center_y is not None else h // 2
    ld.text((w // 2, cy), text, font=wm_font, fill=(33, 37, 41, alpha), anchor="mm")
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_replica_table(sched: ScrapedSchedule) -> bytes:
    width = RENDER_W
    height = RENDER_H

    im = Image.new("RGB", (width, height), COLOR_CARD_BG)
    draw = ImageDraw.Draw(im)

    # Fonts (original sizes scaled cleanly to 3840x1760 then LANCZOS-downsampled to 2560x1174)
    font_date_big = load_font(_s(28), bold=True)
    font_status_txt = load_font(_s(15), bold=True)
    font_legend_box = load_font(_s(12), bold=True)
    font_legend_lbl = load_font(_s(13), bold=False)
    font_logo_txt = load_font(_s(20), bold=True)
    font_banner_txt = load_font(_s(19), bold=True)
    font_hour_h = load_font(_s(12), bold=True)
    font_grp_lbl = load_font(_s(9), bold=False)
    font_grp_num = load_font(_s(16), bold=True)
    font_cell = load_font(_s(11), bold=True)
    font_header_h = load_font(_s(13), bold=True)

    margin_x = _s(32)
    border_w = max(2, _s(1))

    # 1. TOP HEADER BAR
    logo_d = _s(60)
    logo_cx, logo_cy = margin_x + logo_d // 2, _s(60)
    logo_img = get_logo(logo_d)
    if logo_img is not None:
        im.paste(logo_img, (logo_cx - logo_d // 2, logo_cy - logo_d // 2), logo_img)
    else:
        r_logo = _s(30)
        draw.ellipse([logo_cx - r_logo, logo_cy - r_logo, logo_cx + r_logo, logo_cy + r_logo], fill=(237, 28, 36))
        draw.text((logo_cx, logo_cy), "C4", font=font_logo_txt, fill=(255, 255, 255), anchor="mm")

    badge_x0 = margin_x + logo_d + _s(16)
    badge_x1 = badge_x0 + _s(218)
    draw.rounded_rectangle([badge_x0, _s(28), badge_x1, _s(92)], radius=_s(12), fill=COLOR_DATE_BADGE)
    draw.text(((badge_x0 + badge_x1) // 2, _s(60)), sched.date_str, font=font_date_big, fill=(0, 0, 0), anchor="mm")

    clock_cx, clock_cy = badge_x1 + _s(30), _s(60)
    clock_r = _s(11)
    clock_w = max(2, _s(2))
    draw.ellipse([clock_cx - clock_r, clock_cy - clock_r, clock_cx + clock_r, clock_cy + clock_r],
                 outline=COLOR_DARK_TEXT, width=clock_w)
    draw.line([(clock_cx, clock_cy), (clock_cx, clock_cy - _s(6))], fill=COLOR_DARK_TEXT, width=clock_w)
    draw.line([(clock_cx, clock_cy), (clock_cx + _s(5), clock_cy)], fill=COLOR_DARK_TEXT, width=clock_w)

    status_str = f"Графік актуальний станом на {sched.upd_time}"
    draw.text((clock_cx + _s(22), _s(60)), status_str, font=font_status_txt, fill=COLOR_DARK_TEXT, anchor="lm")

    badges = [
        ("В", "Відключені", COLOR_OFF, _s(30)),
        ("З", "Заживлені", COLOR_ON, _s(30)),
        ("МЗ", "Можливо заживлені", COLOR_MAYBE, _s(38)),
    ]
    item_gap = _s(22)
    box_pad = _s(8)
    widths = [bw + box_pad + int(draw.textlength(label, font=font_legend_lbl)) for _, label, _, bw in badges]
    legend_w = sum(widths) + item_gap * (len(badges) - 1)
    bx = width - margin_x - legend_w
    for tag, label, color, box_w in badges:
        draw.rounded_rectangle([bx, _s(45), bx + box_w, _s(75)], radius=_s(4), fill=color)
        draw.text((bx + box_w // 2, _s(60)), tag, font=font_legend_box, fill=COLOR_DARK_TEXT, anchor="mm")
        bx += box_w + box_pad
        draw.text((bx, _s(60)), label, font=font_legend_lbl, fill=COLOR_DARK_TEXT, anchor="lm")
        bx += int(draw.textlength(label, font=font_legend_lbl)) + item_gap

    # 2. GREEN BANNER
    draw.rounded_rectangle([margin_x, _s(108), width - margin_x, _s(152)], radius=_s(8), fill=COLOR_GREEN_BANNER)
    draw.text((_s(52), _s(130)), sched.month_name.capitalize(), font=font_banner_txt, fill=COLOR_DARK_TEXT, anchor="lm")
    draw.text((width // 2 + _s(50), _s(130)), "Графік погодинних відключень", font=font_banner_txt, fill=COLOR_DARK_TEXT,
              anchor="mm")

    # 3. TABLE GRID GEOMETRY
    left_col_x0 = margin_x
    left_col_w = _s(120)
    grid_x0 = left_col_x0 + left_col_w
    grid_x1 = width - margin_x
    grid_w = grid_x1 - grid_x0
    slot_w = grid_w / 48
    hour_w = slot_w * 2

    top_hour_y0 = _s(162)
    top_hour_y1 = _s(202)
    marker_bar_h = _s(10)
    row_h = _s(33)
    grid_y0 = top_hour_y1
    grid_y1 = grid_y0 + 12 * row_h
    bot_hour_y0 = grid_y1
    bot_hour_y1 = bot_hour_y0 + _s(34)

    draw.rectangle([left_col_x0, top_hour_y0, grid_x1, top_hour_y1], fill=COLOR_HOURS_BG)
    draw.text(
        (left_col_x0 + left_col_w // 2, (top_hour_y0 + top_hour_y1) // 2),
        sched.date_str, font=font_header_h, fill=COLOR_DARK_TEXT, anchor="mm",
    )

    hour_text_y = top_hour_y0 + (top_hour_y1 - top_hour_y0 - marker_bar_h) // 2
    for h in range(24):
        hx = int(round(grid_x0 + h * hour_w))
        draw.line([(hx, top_hour_y0), (hx, top_hour_y1)], fill=COLOR_BORDER, width=border_w)
        draw.text((grid_x0 + h * hour_w + hour_w / 2, hour_text_y), f"{h:02d}:00", font=font_hour_h,
                  fill=COLOR_DARK_TEXT, anchor="mm")

    # 4. 12 GROUP ROWS
    for q in range(1, 13):
        ry0 = grid_y0 + (q - 1) * row_h
        ry1 = ry0 + row_h

        bg_grp = (255, 255, 255) if q % 2 != 0 else (248, 250, 252)
        draw.rectangle([left_col_x0, ry0, grid_x0, ry1], fill=bg_grp)

        draw.text((left_col_x0 + _s(12), (ry0 + ry1) // 2), "Г Р У П А", font=font_grp_lbl, fill=COLOR_MUTED_GRAY,
                  anchor="lm")
        draw.text((grid_x0 - _s(18), (ry0 + ry1) // 2), str(q), font=font_grp_num, fill=COLOR_DARK_TEXT, anchor="rm")

        q_slots = sched.slots.get(q, ["з"] * 48)
        for s in range(48):
            sx0 = int(round(grid_x0 + s * slot_w))
            sx1 = int(round(grid_x0 + (s + 1) * slot_w))
            state = q_slots[s]

            if state == "в":
                fill_c = COLOR_OFF
            elif state == "мз":
                fill_c = COLOR_MAYBE
            else:
                fill_c = COLOR_ON

            draw.rectangle([sx0, ry0, sx1, ry1], fill=fill_c)
            draw.text(((sx0 + sx1) // 2, (ry0 + ry1) // 2), state, font=font_cell,
                      fill=COLOR_DARK_TEXT, anchor="mm")

    # Uniform white cell borders (vertical + horizontal)
    for s in range(49):
        sx = int(round(grid_x0 + s * slot_w))
        draw.line([(sx, grid_y0), (sx, grid_y1)], fill=COLOR_BORDER, width=border_w)
    for q in range(13):
        ry = grid_y0 + q * row_h
        draw.line([(left_col_x0, ry), (grid_x1, ry)], fill=COLOR_BORDER, width=border_w)

    # 5. BOTTOM HOURS ROW
    draw.rectangle([left_col_x0, bot_hour_y0, grid_x1, bot_hour_y1], fill=COLOR_HOURS_BG)
    for h in range(24):
        hx = int(round(grid_x0 + h * hour_w))
        draw.line([(hx, bot_hour_y0), (hx, bot_hour_y1)], fill=COLOR_BORDER, width=border_w)
        draw.text((grid_x0 + h * hour_w + hour_w / 2, (bot_hour_y0 + bot_hour_y1) // 2), f"{h:02d}:00",
                  font=font_hour_h, fill=COLOR_DARK_TEXT, anchor="mm")

    # 5b. WATERMARK
    im = apply_watermark(im, WATERMARK_TEXT, WATERMARK_ALPHA, center_y=(grid_y0 + grid_y1) // 2)
    draw = ImageDraw.Draw(im)

    # 6. CURRENT-HOUR BLUE MARKER (#283f9f, moves by whole hour)
    if 0 <= sched.active_slot < 48:
        active_hour = sched.active_slot // 2
        hx0 = int(round(grid_x0 + (active_hour * 2) * slot_w))
        hx1 = int(round(grid_x0 + ((active_hour + 1) * 2) * slot_w))

        # Bold top bar above Group 1 with 3 centered white dots
        bar_y0 = grid_y0 - marker_bar_h
        bar_y1 = grid_y0
        draw.rounded_rectangle([hx0, bar_y0, hx1, bar_y1], radius=_s(3), fill=COLOR_ACTIVE_HOUR)

        dot_cx = (hx0 + hx1) // 2
        dot_cy = (bar_y0 + bar_y1) // 2
        dot_r = max(2, _s(2))
        dot_gap = _s(8)
        for dx in (-dot_gap, 0, dot_gap):
            draw.ellipse(
                [dot_cx + dx - dot_r, dot_cy - dot_r, dot_cx + dx + dot_r, dot_cy + dot_r],
                fill=COLOR_ACTIVE_DOTS,
            )

        # Lighter/thinner blue separator lines between each group across the active hour
        sep_w = max(2, _s(2))
        for q in range(1, 13):
            ry1 = grid_y0 + q * row_h
            draw.line([(hx0, ry1), (hx1, ry1)], fill=COLOR_ACTIVE_HOUR, width=sep_w)

    # Downsample 3840x1760 -> 2560x1174 for crisp anti-aliased edges and native Telegram max res
    im = im.resize((OUT_W, OUT_H), Image.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# =========================================================================== #
# Telegram Bot Keyboards & Views
# =========================================================================== #
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Графік на сьогодні"), KeyboardButton("Графік на завтра")],
            [KeyboardButton("Довідка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def help_text() -> str:
    return (
        "💡 <b>Бот-реплікатор сайту Чернівціобленерго</b>\n\n"
        "Бот підключається напряму до <code>oblenergo.cv.ua/shutdowns/</code>, "
        "відстежує зміни графіків та генерує таблицю.\n\n"
        "<b>Кнопки меню:</b>\n"
        "• <b>Графік на сьогодні</b> — отримати поточну таблицю з сайту.\n"
        "• <b>Графік на завтра</b> — таблиця на завтра, якщо вона вже опублікована.\n"
        "• <b>Довідка</b> — інформація про роботу бота.\n"
        "• 🔔 Користувачі та канали автоматично отримують сповіщення при кожній зміні графіка "
        "(на сьогодні та на завтра) та коли публікується графік на завтра."
    )


def build_caption(
    sched: ScrapedSchedule,
    changed: bool = False,
    tomorrow: bool = False,
    published: bool = False,
) -> str:
    uk_date = format_uk_date(sched.date_str)
    if tomorrow:
        if changed:
            return f"⚡️Оновлений графік знеструмлень на завтра, {uk_date}"
        return f"⚡️Орієнтовний графік знеструмлень на завтра, {uk_date}"

    if changed:
        return "💡 Оновлений графік знеструмлень на сьогодні"
    return f"💡 Графік знеструмлень на сьогодні, {uk_date}"


# =========================================================================== #
# Broadcasting (users + channels)
# =========================================================================== #
async def _send_one(bot, chat_id: int, png_bytes: bytes, caption: str) -> None:
    await bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(png_bytes),
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


async def broadcast(bot, sched: ScrapedSchedule, caption: str) -> None:
    png_bytes = render_replica_table(sched)

    targets = [(uid, subscribers) for uid in list(subscribers.subscribers)] + \
              [(cid, channels) for cid in list(channels.subscribers)]
    log.info("Broadcasting to %d targets", len(targets))

    for chat_id, store in targets:
        for _ in range(2):
            try:
                await _send_one(bot, chat_id, png_bytes, caption)
                break
            except RetryAfter as e:
                log.warning("Flood limit, sleeping %ss", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 1)
            except Forbidden as e:
                log.warning("Removing %s (forbidden): %s", chat_id, e)
                if chat_id not in PINNED_CHANNELS:
                    await store.remove_async(chat_id)
                break
            except BadRequest as e:
                if "chat not found" in str(e).lower() and chat_id not in PINNED_CHANNELS:
                    log.warning("Removing %s (chat not found)", chat_id)
                    await store.remove_async(chat_id)
                else:
                    log.warning("Bad request for %s: %s", chat_id, e)
                break
            except Exception as e:
                log.warning("Failed posting to %s: %s", chat_id, e)
                break
        await asyncio.sleep(0.05)


# =========================================================================== #
# Single entry point for every fetch: compare -> store -> broadcast
# =========================================================================== #
_update_lock: Optional[asyncio.Lock] = None


async def save_schedule_state(sched: ScrapedSchedule) -> None:
    if remote.enabled:
        await remote.set("oblenergo:schedule", sched.to_json())


async def save_tomorrow_state(sched: Optional[ScrapedSchedule]) -> None:
    if remote.enabled:
        await remote.set("oblenergo:schedule_tomorrow", sched.to_json() if sched else "null")


async def handle_tomorrow(bot, new_t: Optional[ScrapedSchedule]) -> None:
    """Announce a newly published tomorrow table, or a change of an existing one."""
    old_t = scraper.tomorrow_schedule
    first_check = not scraper.tomorrow_initialized
    scraper.tomorrow_schedule = new_t
    scraper.tomorrow_initialized = True

    if first_check:
        # Baseline only (no previous state known) - avoids re-announcing after every restart
        await save_tomorrow_state(new_t)
        log.info("Tomorrow baseline: %s", new_t.date_str if new_t else "not published")
        return

    if new_t is None:
        if old_t is not None:
            await save_tomorrow_state(None)
        return

    if old_t is None or old_t.date_str != new_t.date_str:
        log.info("🔔 Tomorrow schedule published: %s", new_t.date_str)
        scraper.last_update_at = datetime.now(TZ)
        await save_tomorrow_state(new_t)
        await broadcast(bot, new_t, build_caption(new_t, tomorrow=True, published=True))
    elif schedule_changed(old_t, new_t):
        log.info("🔔 Tomorrow schedule changed: %s (upd %s)", new_t.date_str, new_t.upd_time)
        scraper.last_update_at = datetime.now(TZ)
        await save_tomorrow_state(new_t)
        await broadcast(bot, new_t, build_caption(new_t, changed=True, tomorrow=True))


async def refresh_and_notify(bot) -> Optional[ScrapedSchedule]:
    """Fetch the site (today + tomorrow). Broadcast to everyone if anything changed.

    Used by the background loop AND by manual requests, so an update found by a
    button press is never "swallowed" without notifying subscribers/channels.
    Returns today's fresh schedule, or None if the site could not be read.
    """
    global _update_lock
    if _update_lock is None:
        _update_lock = asyncio.Lock()

    async with _update_lock:
        res = await scraper.fetch_all()
        today = res.today
        if not today:
            return None

        old = scraper.last_schedule
        prev_tomorrow = scraper.tomorrow_schedule
        scraper.last_schedule = today

        if old is None:
            log.info("Initialized scraper: date=%s, upd=%s", today.date_str, today.upd_time)
            await save_schedule_state(today)
        elif schedule_changed(old, today):
            await save_schedule_state(today)
            # Midnight rollover: if tomorrow's table was already announced and today's table
            # is identical to it, there is nothing new to tell people.
            rolled_over = old.date_str != today.date_str
            already_announced = (
                rolled_over
                and prev_tomorrow is not None
                and prev_tomorrow.date_str == today.date_str
                and not schedule_changed(prev_tomorrow, today)
            )
            if already_announced:
                log.info("New day %s already announced as tomorrow's schedule", today.date_str)
            else:
                log.info("🔔 Schedule updated! Date: %s, Upd: %s", today.date_str, today.upd_time)
                scraper.last_update_at = datetime.now(TZ)
                await broadcast(bot, today, build_caption(today, changed=True))

        if res.tomorrow_ok:
            await handle_tomorrow(bot, res.tomorrow)
        return today


async def monitor_loop(bot) -> None:
    log.info("Monitor loop started (every %ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await refresh_and_notify(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Monitor iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(app: Application) -> None:
    """Runs inside the bot's event loop right before polling starts."""
    await subscribers.load_remote()
    await channels.load_remote()
    channels.subscribers |= PINNED_CHANNELS

    raw = await remote.get("oblenergo:schedule")
    if raw:
        try:
            scraper.last_schedule = ScrapedSchedule.from_json(raw)
            log.info(
                "Restored last schedule: date=%s upd=%s",
                scraper.last_schedule.date_str, scraper.last_schedule.upd_time,
            )
        except Exception as e:
            log.warning("Could not restore schedule: %s", e)

    raw_t = await remote.get("oblenergo:schedule_tomorrow")
    if raw_t is not None:
        scraper.tomorrow_initialized = True
        if raw_t != "null":
            try:
                scraper.tomorrow_schedule = ScrapedSchedule.from_json(raw_t)
                log.info("Restored tomorrow schedule: date=%s", scraper.tomorrow_schedule.date_str)
            except Exception as e:
                log.warning("Could not restore tomorrow schedule: %s", e)

    if not remote.enabled:
        log.warning(
            "Remote persistence is OFF: subscribers/channels/schedule are lost on restart. "
            "Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (and/or CHANNEL_IDS)."
        )

    app.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(app.bot))


# =========================================================================== #
# Bot Handlers
# =========================================================================== #
async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        added = await subscribers.add_async(user.id)
        log.info("User /start: uid=%s (added=%s, total=%d)", user.id, added, len(subscribers.subscribers))

    msg = (
        "👋 <b>Вітаємо!</b>\n\n"
        "Ви успішно підписалися на сповіщення про графіки з сайту <b>Чернівціобленерго</b>.\n"
        "Щойно обленерго змінить графік відключень, бот надішле вам оновлену таблицю."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    await send_schedule_replica(update, ctx)


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        help_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
    )


async def send_schedule_replica(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    sched = scraper.last_schedule
    if not sched:
        sched = await refresh_and_notify(ctx.bot)

    if not sched:
        err_msg = scraper.last_fetch_error or "Сайт тимчасово недоступний"
        await ctx.bot.send_message(
            chat_id,
            f"🔴 <b>Не вдалося завантажити графік з сайту</b>\n<i>{err_msg}</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    png_bytes = render_replica_table(sched)
    await ctx.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(png_bytes),
        caption=build_caption(sched),
        parse_mode=ParseMode.HTML,
    )


async def send_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    if scraper.tomorrow_schedule is None:
        await refresh_and_notify(ctx.bot)

    sched = scraper.tomorrow_schedule
    if not sched:
        if scraper.last_fetch_error:
            await ctx.bot.send_message(
                chat_id,
                f"🔴 <b>Не вдалося перевірити графік на завтра</b>\n<i>{scraper.last_fetch_error}</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await ctx.bot.send_message(chat_id, "Графік на завтра не знайдено")
        return

    await ctx.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(render_replica_table(sched)),
        caption=build_caption(sched, tomorrow=True),
        parse_mode=ParseMode.HTML,
    )


async def on_debug_website_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sched = scraper.last_schedule
    last_fetch = scraper.last_fetch_at.strftime("%d.%m.%Y %H:%M:%S") if scraper.last_fetch_at else "ще не було"
    last_upd = scraper.last_update_at.strftime("%d.%m.%Y %H:%M:%S") if scraper.last_update_at else "ще не зафіксовано"
    err_str = scraper.last_fetch_error or "немає (OK)"

    if not sched:
        text = (
            f"🌐 <b>Debug: Статус сайту</b>\n\n"
            f"• URL: <code>{OBLENERGO_URL}</code>\n"
            f"• Стан: ⏳ дані ще не завантажені\n"
            f"• Останній запит: <b>{last_fetch}</b>\n"
            f"• Остання помилка: <b>{err_str}</b>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    t = scraper.tomorrow_schedule
    if t:
        tomorrow_block = (
            f"опубліковано на <b>{t.date_str}</b> ({format_uk_date(t.date_str)})\n"
            f"  └ станом на: <b>{t.upd_time}</b> | груп: <b>{len(t.slots)}</b> | "
            f"start_slot: <code>{t.start_slot}</code> | hash: <code>{t.html_hash[:8]}</code>"
        )
    else:
        tomorrow_block = (
            f"<b>не опубліковано</b> (initialized={scraper.tomorrow_initialized})"
        )

    text = (
        f"🌐 <b>Debug: Статус сайту Чернівціобленерго</b>\n\n"
        f"• URL: <code>{OBLENERGO_URL}</code>\n"
        f"• Останній запит: <b>{last_fetch}</b>\n"
        f"• Остання зміна графіка: <b>{last_upd}</b>\n"
        f"• Запитів OK / помилок: <b>{scraper.fetch_ok_count}</b> / <b>{scraper.fetch_err_count}</b>\n"
        f"• Остання помилка: <b>{err_str}</b>\n\n"
        f"📅 <b>Сьогодні:</b> <b>{sched.date_str}</b> ({format_uk_date(sched.date_str)})\n"
        f"• Станом на: <b>{sched.upd_time}</b> | місяць: <b>{sched.month_name}</b>\n"
        f"• Груп зчитано: <b>{len(sched.slots)}</b>\n"
        f"• active_slot: <code>{sched.active_slot}</code> | start_slot: <code>{sched.start_slot}</code>\n"
        f"• hash: <code>{sched.html_hash[:12]}</code>\n\n"
        f"🗓 <b>Завтра:</b> {tomorrow_block}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_debug_bot_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TZ)
    uptime = now - BOT_STARTED_AT
    total_sec = int(uptime.total_seconds())
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{days}д {hours:02d}:{mins:02d}:{secs:02d}" if days else f"{hours:02d}:{mins:02d}:{secs:02d}"

    # Check monitor background task
    task = ctx.application.bot_data.get("monitor_task")
    if task is None:
        monitor_status = "❌ не запущено"
    elif task.done():
        exc = task.exception() if not task.cancelled() else None
        monitor_status = f"❌ зупинено ({exc or 'cancelled'})"
    else:
        monitor_status = "✅ працює"

    # Live Redis check
    redis_ok, redis_msg = await remote.ping()
    redis_badge = "✅" if redis_ok else ("⚠️" if not remote.enabled else "❌")

    # HTTP session state
    sess = scraper._session
    http_state = "✅ відкрита" if (sess is not None and not sess.closed) else "⚪️ ще не створена / закрита"

    last_fetch = scraper.last_fetch_at.strftime("%d.%m.%Y %H:%M:%S") if scraper.last_fetch_at else "ще не було"
    err_str = scraper.last_fetch_error or "немає"

    conflict_info = (
        f"⚠️ {CONFLICT_COUNT} (останній: {LAST_CONFLICT_AT.strftime('%d.%m %H:%M:%S')})"
        if CONFLICT_COUNT > 0 and LAST_CONFLICT_AT
        else "✅ 0"
    )

    sub_file_exists = "✅ є" if SUBSCRIBERS_FILE.exists() else "⚪️ немає"
    ch_file_exists = "✅ є" if CHANNELS_FILE.exists() else "⚪️ немає"

    today_state = (
        f"✅ {scraper.last_schedule.date_str} (станом на {scraper.last_schedule.upd_time})"
        if scraper.last_schedule
        else "❌ немає даних"
    )
    tomorrow_state = (
        f"✅ {scraper.tomorrow_schedule.date_str} (станом на {scraper.tomorrow_schedule.upd_time})"
        if scraper.tomorrow_schedule
        else ("⚪️ не опубліковано" if scraper.tomorrow_initialized else "⏳ ще не перевірено")
    )

    text = (
        f"🛠 <b>Debug: Статус бота</b>\n\n"
        f"<b>Загальне:</b>\n"
        f"• Аптайм: <b>{uptime_str}</b> (старт: {BOT_STARTED_AT.strftime('%d.%m %H:%M:%S')})\n"
        f"• Часова зона: <code>{TZ.key}</code>\n"
        f"• Інтервал перевірки: <b>{CHECK_INTERVAL_SECONDS} сек.</b>\n"
        f"• BOT_TOKEN: <b>{'✅ задано' if BOT_TOKEN else '❌ не задано'}</b>\n"
        f"• Логотип C4: <b>{'✅ завантажено' if LOGO_C4_B64 else '⚠️ fallback'}</b>\n"
        f"• Веб-порт Gradio: <code>{os.environ.get('PORT', '7860')}</code>\n\n"
        f"<b>Моніторинг і мережа:</b>\n"
        f"• Фоновий цикл: <b>{monitor_status}</b>\n"
        f"• Конфлікти polling (Conflict): <b>{conflict_info}</b>\n"
        f"• HTTP-сесія скрапера: <b>{http_state}</b>\n"
        f"• Останній запит: <b>{last_fetch}</b>\n"
        f"• Запитів OK / помилок: <b>{scraper.fetch_ok_count}</b> / <b>{scraper.fetch_err_count}</b>\n"
        f"• Остання помилка скрапера: <b>{err_str}</b>\n\n"
        f"<b>Збереження стану (Persistence):</b>\n"
        f"• Upstash Redis: <b>{redis_badge} {redis_msg}</b>\n"
        f"• Остання помилка Redis: <b>{remote.last_error or 'немає'}</b>\n"
        f"• Локальний файл підписників: <b>{sub_file_exists}</b>\n"
        f"• Локальний файл каналів: <b>{ch_file_exists}</b>\n\n"
        f"<b>Аудиторія та кеш:</b>\n"
        f"• Підписників (ЛС): <b>{len(subscribers.subscribers)}</b>\n"
        f"• Каналів усього: <b>{len(channels.subscribers)}</b> (закріплених у env: <b>{len(PINNED_CHANNELS)}</b>)\n"
        f"• Кеш сьогодні: <b>{today_state}</b>\n"
        f"• Кеш завтра: <b>{tomorrow_state}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()


async def on_text_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text in ("Графік на сьогодні", "/table"):
        await send_schedule_replica(update, ctx)
    elif text in ("Графік на завтра", "/tomorrow"):
        await send_tomorrow(update, ctx)
    elif text in ("Довідка", "/help"):
        await on_help(update, ctx)
    else:
        await update.effective_message.reply_text(
            "Скористайтесь кнопками меню 👇",
            reply_markup=main_reply_keyboard(),
        )


async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when the bot's own status changes in a chat (e.g. made admin of a channel)."""
    cm = update.my_chat_member
    if not cm:
        return

    chat_id = cm.chat.id
    status = cm.new_chat_member.status

    # A user blocked the bot -> stop sending to them
    if cm.chat.type == "private":
        if status in ("kicked", "left"):
            await subscribers.remove_async(chat_id)
        return

    if cm.chat.type not in ("channel", "supergroup", "group"):
        return

    if status == "administrator":
        if getattr(cm.new_chat_member, "can_post_messages", True) is False and cm.chat.type == "channel":
            log.warning("Bot is admin in %s but cannot post messages", chat_id)
        await channels.add_async(chat_id)
        log.info("Added to chat %s (%s)", chat_id, cm.chat.title)

        sched = scraper.last_schedule or await refresh_and_notify(ctx.bot)
        if sched:
            try:
                await _send_one(ctx.bot, chat_id, render_replica_table(sched), build_caption(sched))
            except Exception as e:
                log.warning("Initial channel post failed: %s", e)
    elif status in ("left", "kicked", "member", "restricted"):
        if chat_id not in PINNED_CHANNELS:
            await channels.remove_async(chat_id)
        log.info("Removed from chat %s (status=%s)", chat_id, status)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFLICT_COUNT, LAST_CONFLICT_AT
    if isinstance(context.error, Conflict):
        CONFLICT_COUNT += 1
        LAST_CONFLICT_AT = datetime.now(TZ)
        log.warning("Conflict detected (#%d): another instance is polling with the same BOT_TOKEN!", CONFLICT_COUNT)
        return
    log.error("Update error: %s", context.error)


# =========================================================================== #
# Bot Thread & Gradio App (Render / HF Entrypoint)
# =========================================================================== #
def run_bot() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing! Set it in Environment Variables.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("table", send_schedule_replica))
    app.add_handler(CommandHandler("tomorrow", send_tomorrow))
    app.add_handler(CommandHandler("debug_website_status", on_debug_website_status))
    app.add_handler(CommandHandler("debug_bot_status", on_debug_bot_status))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    app.run_polling(
        drop_pending_updates=False,
        stop_signals=None,
        allowed_updates=["message", "callback_query", "my_chat_member"],
    )


def get_dashboard_data():
    sched = scraper.last_schedule
    date_val = sched.date_str if sched else "Немає даних"
    upd_val = sched.upd_time if sched else "Немає даних"
    sub_count = len(subscribers.subscribers)
    ch_count = len(channels.subscribers)
    status_str = (
        f"Активний | Підписників: {sub_count} | Каналів: {ch_count} | "
        f"Перевірка: кожні {CHECK_INTERVAL_SECONDS}с | "
        f"Збереження стану: {'Redis' if remote.enabled else 'вимкнено'}"
    )

    img_preview = None
    if sched:
        try:
            raw_png = render_replica_table(sched)
            img_preview = Image.open(io.BytesIO(raw_png))
        except Exception:
            log.exception("Dashboard render failed")

    return status_str, date_val, upd_val, img_preview


with gr.Blocks(title="Oblenergo Replica Bot Dashboard") as demo:
    gr.Markdown("# ⚡ Чернівціобленерго — Бот-реплікатор сайту")
    gr.Markdown(
        "Моніторить сайт `oblenergo.cv.ua/shutdowns/` та надсилає візуальну репліку таблиці в Telegram при кожній зміні."
    )

    with gr.Row():
        box_status = gr.Textbox(label="Стан сервісу", interactive=False)
        box_date = gr.Textbox(label="Дата на сайті", interactive=False)
        box_upd = gr.Textbox(label="Станом на", interactive=False)

    image_output = gr.Image(label="Попередній перегляд згенерованої таблиці-репліки", type="pil")
    btn_refresh = gr.Button("🔄 Оновити дані з сайту")
    btn_refresh.click(fn=get_dashboard_data, outputs=[box_status, box_date, box_upd, image_output])

    demo.load(fn=get_dashboard_data, outputs=[box_status, box_date, box_upd, image_output])

if __name__ == "__main__":
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)