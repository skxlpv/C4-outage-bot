"""
Chernivtsioblenergo Web Replica & Tracker Telegram Bot (Render / HF Edition)

- Replicates the oblenergo schedule table as an image
- Pushes an updated table to all subscribers AND channels as soon as the
  schedule on the site changes (no button press needed)
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
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import gradio as gr
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
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
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", "30"))
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
TZ = ZoneInfo("Europe/Kyiv")

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
COLOR_GREEN_BANNER = (116, 140, 66)
COLOR_HOURS_BG = (245, 203, 166)
COLOR_ON = (172, 196, 116)
COLOR_OFF = (225, 98, 84)
COLOR_MAYBE = (122, 168, 198)
COLOR_BORDER = (255, 255, 255)
COLOR_DARK_TEXT = (33, 37, 41)
COLOR_MUTED_GRAY = (120, 144, 156)
COLOR_LINE_MARKER = (220, 30, 30)
LINE_MARKER_ALPHA = 77

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full Telegram URLs (which contain the bot token) at INFO level
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("svitlo-replica-bot")


# =========================================================================== #
# Remote persistence (Upstash Redis REST) - optional
# =========================================================================== #
class RemoteKV:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.enabled = bool(url and token)

    async def _cmd(self, *args):
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                    self.url,
                    json=list(args),
                    headers={"Authorization": f"Bearer {self.token}"},
            ) as r:
                data = await r.json()
                return data.get("result")

    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            return await self._cmd("GET", key)
        except Exception as e:
            log.warning("Remote GET %s failed: %s", key, e)
            return None

    async def set(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        try:
            await self._cmd("SET", key, value)
        except Exception as e:
            log.warning("Remote SET %s failed: %s", key, e)


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
        self.load()

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
        try:
            self.path.write_text(payload, encoding="utf-8")
        except Exception as e:
            log.warning("Failed saving %s: %s", self.path, e)
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

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "ScrapedSchedule":
        d = json.loads(raw)
        d["slots"] = {int(k): v for k, v in d["slots"].items()}
        return ScrapedSchedule(**d)


def schedule_changed(old: ScrapedSchedule, new: ScrapedSchedule) -> bool:
    """True if the site published something new.

    Elapsed half-hour cells are ignored (the site drops them as the day goes
    on), so only the date, the 'Станом на' time and upcoming slots matter.
    """
    if old.date_str != new.date_str or old.upd_time != new.upd_time:
        return True
    start = max(old.active_slot, new.active_slot, 0)
    for q in set(old.slots) | set(new.slots):
        if old.slots.get(q, [])[start:] != new.slots.get(q, [])[start:]:
            return True
    return False


class OblenergoScraper:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_schedule: Optional[ScrapedSchedule] = None
        self.last_fetch_error: Optional[str] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=BROWSER_HEADERS,
                cookies=BROWSER_COOKIES,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def fetch(self) -> Optional[ScrapedSchedule]:
        session = await self._get_session()
        try:
            async with session.get(OBLENERGO_URL) as resp:
                if resp.status != 200:
                    self.last_fetch_error = f"HTTP {resp.status}"
                    log.warning("Site returned status %s", resp.status)
                    return None
                html_text = await resp.text()
                self.last_fetch_error = None
        except Exception as e:
            self.last_fetch_error = str(e)
            log.warning("Scrape error: %s", e)
            return None

        return self.parse_html(html_text, prev_schedule=self.last_schedule)

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

            # As the day progresses, the site removes elapsed 30-min cells.
            # Preserve earlier slots from prev_schedule (same date).
            if len(q_slots) < 48:
                start_offset = 48 - len(q_slots)
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

        content_repr = f"{date_str}_{upd_time}_{json.dumps(group_slots, sort_keys=True)}"
        content_hash = hashlib.md5(content_repr.encode()).hexdigest()

        return ScrapedSchedule(
            date_str=date_str,
            upd_time=upd_time,
            month_name=month_name,
            slots=group_slots,
            html_hash=content_hash,
            active_slot=active_slot,
        )


scraper = OblenergoScraper()

# =========================================================================== #
# Replica Image Generator (Pillow)
# =========================================================================== #
LOGO_C4_B64 = logo

WATERMARK_TEXT = "Чернівці | С4"
WATERMARK_ALPHA = 48  # 0..255, very transparent

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
    wm_font = load_font(96, bold=True)
    cy = center_y if center_y is not None else h // 2
    ld.text((w // 2, cy), text, font=wm_font, fill=(33, 37, 41, alpha), anchor="mm")
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def render_replica_table(sched: ScrapedSchedule) -> bytes:
    width = 1440
    height = 660

    im = Image.new("RGB", (width, height), COLOR_CARD_BG)
    draw = ImageDraw.Draw(im)

    # Fonts
    font_date_big = load_font(28, bold=True)
    font_status_txt = load_font(15, bold=True)
    font_legend_box = load_font(12, bold=True)
    font_legend_lbl = load_font(13, bold=False)
    font_logo_txt = load_font(20, bold=True)
    font_banner_txt = load_font(19, bold=True)
    font_hour_h = load_font(12, bold=True)
    font_grp_lbl = load_font(9, bold=False)
    font_grp_num = load_font(16, bold=True)
    font_cell = load_font(11, bold=True)
    font_header_h = load_font(13, bold=True)

    # 1. TOP HEADER BAR
    logo_d = 60
    logo_cx, logo_cy = 32 + logo_d // 2, 60
    logo_img = get_logo(logo_d)
    if logo_img is not None:
        im.paste(logo_img, (logo_cx - logo_d // 2, logo_cy - logo_d // 2), logo_img)
    else:
        draw.ellipse([logo_cx - 30, logo_cy - 30, logo_cx + 30, logo_cy + 30], fill=(237, 28, 36))
        draw.text((logo_cx, logo_cy), "C4", font=font_logo_txt, fill=(255, 255, 255), anchor="mm")

    badge_x0 = 32 + logo_d + 16
    badge_x1 = badge_x0 + 218
    draw.rounded_rectangle([badge_x0, 28, badge_x1, 92], radius=12, fill=COLOR_DATE_BADGE)
    draw.text(((badge_x0 + badge_x1) // 2, 60), sched.date_str, font=font_date_big, fill=(0, 0, 0), anchor="mm")

    clock_cx, clock_cy = badge_x1 + 30, 60
    draw.ellipse([clock_cx - 11, clock_cy - 11, clock_cx + 11, clock_cy + 11], outline=COLOR_DARK_TEXT, width=2)
    draw.line([(clock_cx, clock_cy), (clock_cx, clock_cy - 6)], fill=COLOR_DARK_TEXT, width=2)
    draw.line([(clock_cx, clock_cy), (clock_cx + 5, clock_cy)], fill=COLOR_DARK_TEXT, width=2)

    status_str = f"Графік актуальний станом на {sched.upd_time}"
    draw.text((clock_cx + 22, 60), status_str, font=font_status_txt, fill=COLOR_DARK_TEXT, anchor="lm")

    badges = [
        ("В", "Відключені", COLOR_OFF, 30),
        ("З", "Заживлені", COLOR_ON, 30),
        ("МЗ", "Можливо заживлені", COLOR_MAYBE, 38),
    ]
    item_gap = 22
    widths = [bw + 8 + int(draw.textlength(label, font=font_legend_lbl)) for _, label, _, bw in badges]
    legend_w = sum(widths) + item_gap * (len(badges) - 1)
    bx = width - 32 - legend_w
    for tag, label, color, box_w in badges:
        draw.rounded_rectangle([bx, 45, bx + box_w, 75], radius=4, fill=color)
        draw.text((bx + box_w // 2, 60), tag, font=font_legend_box, fill=COLOR_DARK_TEXT, anchor="mm")
        bx += box_w + 8
        draw.text((bx, 60), label, font=font_legend_lbl, fill=COLOR_DARK_TEXT, anchor="lm")
        bx += int(draw.textlength(label, font=font_legend_lbl)) + item_gap

    # 2. GREEN BANNER
    draw.rounded_rectangle([32, 108, width - 32, 152], radius=8, fill=COLOR_GREEN_BANNER)
    draw.text((52, 130), sched.month_name.capitalize(), font=font_banner_txt, fill=COLOR_DARK_TEXT, anchor="lm")
    draw.text((width // 2 + 50, 130), "Графік погодинних відключень", font=font_banner_txt, fill=COLOR_DARK_TEXT,
              anchor="mm")

    # 3. TABLE GRID GEOMETRY
    left_col_x0 = 32
    left_col_w = 120
    grid_x0 = left_col_x0 + left_col_w
    grid_x1 = width - 32
    grid_w = grid_x1 - grid_x0
    slot_w = grid_w / 48
    hour_w = slot_w * 2

    top_hour_y0 = 162
    top_hour_y1 = 196
    row_h = 33
    grid_y0 = top_hour_y1
    grid_y1 = grid_y0 + 12 * row_h
    bot_hour_y0 = grid_y1
    bot_hour_y1 = bot_hour_y0 + 34

    draw.rectangle([left_col_x0, top_hour_y0, grid_x1, top_hour_y1], fill=COLOR_HOURS_BG)
    draw.text(
        (left_col_x0 + left_col_w // 2, (top_hour_y0 + top_hour_y1) // 2),
        sched.date_str, font=font_header_h, fill=COLOR_DARK_TEXT, anchor="mm",
    )

    for h in range(24):
        hx = grid_x0 + h * hour_w
        draw.line([(hx, top_hour_y0), (hx, top_hour_y1)], fill=COLOR_BORDER, width=1)
        draw.text((hx + hour_w / 2, (top_hour_y0 + top_hour_y1) // 2), f"{h:02d}:00", font=font_hour_h,
                  fill=COLOR_DARK_TEXT, anchor="mm")

    # 4. 12 GROUP ROWS
    for q in range(1, 13):
        ry0 = grid_y0 + (q - 1) * row_h
        ry1 = ry0 + row_h

        bg_grp = (255, 255, 255) if q % 2 != 0 else (248, 250, 252)
        draw.rectangle([left_col_x0, ry0, grid_x0, ry1], fill=bg_grp)
        draw.line([(left_col_x0, ry1), (grid_x1, ry1)], fill=COLOR_BORDER, width=1)

        draw.text((left_col_x0 + 12, (ry0 + ry1) // 2), "Г Р У П А", font=font_grp_lbl, fill=COLOR_MUTED_GRAY,
                  anchor="lm")
        draw.text((grid_x0 - 18, (ry0 + ry1) // 2), str(q), font=font_grp_num, fill=COLOR_DARK_TEXT, anchor="rm")

        q_slots = sched.slots.get(q, ["з"] * 48)
        for s in range(48):
            sx0 = int(grid_x0 + s * slot_w)
            sx1 = int(grid_x0 + (s + 1) * slot_w)
            state = q_slots[s]

            if state == "в":
                fill_c = COLOR_OFF
            elif state == "мз":
                fill_c = COLOR_MAYBE
            else:
                fill_c = COLOR_ON

            draw.rectangle([sx0, ry0, sx1, ry1], fill=fill_c, outline=COLOR_BORDER, width=1)
            draw.text(((sx0 + sx1) // 2, (ry0 + ry1) // 2), state, font=font_cell, fill=COLOR_DARK_TEXT, anchor="mm")

    # 5. BOTTOM HOURS ROW
    draw.rectangle([left_col_x0, bot_hour_y0, grid_x1, bot_hour_y1], fill=COLOR_HOURS_BG)
    for h in range(24):
        hx = grid_x0 + h * hour_w
        draw.line([(hx, bot_hour_y0), (hx, bot_hour_y1)], fill=COLOR_BORDER, width=1)
        draw.text((hx + hour_w / 2, (bot_hour_y0 + bot_hour_y1) // 2), f"{h:02d}:00", font=font_hour_h,
                  fill=COLOR_DARK_TEXT, anchor="mm")

    # 5b. WATERMARK
    im = apply_watermark(im, WATERMARK_TEXT, WATERMARK_ALPHA, center_y=(grid_y0 + grid_y1) // 2)
    draw = ImageDraw.Draw(im)

    # 6. CURRENT-TIME LINE
    if 0 <= sched.active_slot < 48:
        mark_x = int(round(grid_x0 + sched.active_slot * slot_w))
        line_layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(line_layer).line(
            [(mark_x, grid_y0), (mark_x, grid_y1)],
            fill=COLOR_LINE_MARKER + (LINE_MARKER_ALPHA,),
            width=3,
        )
        im = Image.alpha_composite(im.convert("RGBA"), line_layer).convert("RGB")

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# =========================================================================== #
# Telegram Bot Keyboards & Views
# =========================================================================== #
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Актуальний графік"), KeyboardButton("🔄 Оновити")],
            [KeyboardButton("⏱ Статус сайту"), KeyboardButton("ℹ️ Довідка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def refresh_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Оновити", callback_data="btn_refresh")]])


def help_text() -> str:
    return (
        "💡 <b>Бот-реплікатор сайту Чернівціобленерго</b>\n\n"
        "Бот підключається напряму до <code>oblenergo.cv.ua/shutdowns/</code>, "
        "відстежує дату та рядок <b>«Станом на»</b>, і генерує візуально ідентичну копію таблиці.\n\n"
        "<b>Команди:</b>\n"
        "• 📊 <b>Актуальний графік</b> — отримати поточну таблицю з сайту.\n"
        "• ⏱ <b>Статус сайту</b> — перевірити дату та час публікації.\n"
        "• 🔄 <b>Оновити</b> — примусово перевірити зміни прямо зараз.\n"
        "• 🔔 Користувачі та канали автоматично отримують сповіщення при кожній зміні графіка на сайті."
    )


def build_caption(sched: ScrapedSchedule, changed: bool = False) -> str:
    head = (
        "🔄 <b>Графік на сайті Обленерго змінився!</b>\n\n"
        if changed
        else "📊 <b>Графік відключень з сайту Обленерго</b>\n"
    )
    return f"{head}📅 Дата: <b>{sched.date_str}</b>\n⏱ Станом на: <b>{sched.upd_time}</b>"


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


async def broadcast(bot, sched: ScrapedSchedule, changed: bool = True) -> None:
    png_bytes = render_replica_table(sched)
    caption = build_caption(sched, changed)

    targets = [(uid, subscribers) for uid in list(subscribers.subscribers)] + \
              [(cid, channels) for cid in list(channels.subscribers)]
    log.info("Broadcasting to %d targets", len(targets))

    for chat_id, store in targets:
        for attempt in range(2):
            try:
                await _send_one(bot, chat_id, png_bytes, caption)
                break
            except RetryAfter as e:
                log.warning("Flood limit, sleeping %ss", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 1)
            except Forbidden as e:
                log.warning("Removing %s (forbidden): %s", chat_id, e)
                if chat_id not in PINNED_CHANNELS:
                    store.remove(chat_id)
                break
            except BadRequest as e:
                if "chat not found" in str(e).lower() and chat_id not in PINNED_CHANNELS:
                    log.warning("Removing %s (chat not found)", chat_id)
                    store.remove(chat_id)
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


async def refresh_and_notify(bot) -> Optional[ScrapedSchedule]:
    """Fetch the site. If the schedule changed, broadcast to everyone.

    Used by the background loop AND by buttons, so an update found by a button
    press is never "swallowed" without notifying subscribers/channels.
    Returns the fresh schedule, or None if the site could not be read.
    """
    global _update_lock
    if _update_lock is None:
        _update_lock = asyncio.Lock()

    async with _update_lock:
        new = await scraper.fetch()
        if not new:
            return None

        old = scraper.last_schedule
        scraper.last_schedule = new

        if old is None:
            log.info("Initialized scraper: date=%s, upd=%s", new.date_str, new.upd_time)
            await save_schedule_state(new)
            return new

        if schedule_changed(old, new):
            log.info("🔔 Schedule updated! Date: %s, Upd: %s", new.date_str, new.upd_time)
            await save_schedule_state(new)
            await broadcast(bot, new, changed=True)
        return new


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
    uid = update.effective_user.id
    subscribers.add(uid)

    msg = (
        "👋 <b>Вітаємо!</b>\n\n"
        "Ви успішно підписалися на сповіщення про графіки з сайту <b>Чернівціобленерго</b>.\n"
        "Щойно обленерго оновить статус або змінить години відключень, бот надішле вам оновлену таблицю."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    await send_schedule_replica(update, ctx)


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML,
                                              reply_markup=main_reply_keyboard())


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
        reply_markup=refresh_inline_keyboard(),
    )


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sched = scraper.last_schedule
    if not sched:
        await update.effective_message.reply_text("⏳ Дані ще завантажуються, спробуйте через кілька секунд.")
        return

    text = (
        f"🌐 <b>Статус сайту Чернівціобленерго:</b>\n\n"
        f"• 📅 Дата в графіку: <b>{sched.date_str}</b>\n"
        f"• ⏱ Актуально станом на: <b>{sched.upd_time}</b>\n"
        f"• 👥 Підписників бота: <b>{len(subscribers.subscribers)}</b>\n"
        f"• 📣 Каналів: <b>{len(channels.subscribers)}</b>\n"
        f"• 🔄 Період моніторингу: <b>кожні {CHECK_INTERVAL_SECONDS} сек.</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Перевіряємо оновлення...")
    if q.data == "btn_refresh":
        sched = await refresh_and_notify(ctx.bot) or scraper.last_schedule
        if sched:
            png_bytes = render_replica_table(sched)
            try:
                await q.edit_message_media(
                    media=InputMediaPhoto(
                        io.BytesIO(png_bytes),
                        caption=build_caption(sched),
                        parse_mode=ParseMode.HTML,
                    ),
                    reply_markup=refresh_inline_keyboard(),
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    await send_schedule_replica(update, ctx)
            except Exception:
                await send_schedule_replica(update, ctx)


async def on_text_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text in ("📊 Актуальний графік", "/table"):
        await send_schedule_replica(update, ctx)
    elif text in ("🔄 Оновити", "/check"):
        await refresh_and_notify(ctx.bot)
        await send_schedule_replica(update, ctx)
    elif text in ("⏱ Статус сайту", "/status"):
        await on_status(update, ctx)
    elif text in ("ℹ️ Довідка", "/help"):
        await on_help(update, ctx)
    else:
        await update.effective_message.reply_text("Скористайтесь кнопками меню 👇", reply_markup=main_reply_keyboard())


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
            subscribers.remove(chat_id)
        return

    if cm.chat.type not in ("channel", "supergroup", "group"):
        return

    if status == "administrator":
        if getattr(cm.new_chat_member, "can_post_messages", True) is False and cm.chat.type == "channel":
            log.warning("Bot is admin in %s but cannot post messages", chat_id)
        channels.add(chat_id)
        log.info("Added to chat %s (%s)", chat_id, cm.chat.title)

        sched = scraper.last_schedule or await refresh_and_notify(ctx.bot)
        if sched:
            try:
                await _send_one(ctx.bot, chat_id, render_replica_table(sched), build_caption(sched))
            except Exception as e:
                log.warning("Initial channel post failed: %s", e)
    elif status in ("left", "kicked", "member", "restricted"):
        if chat_id not in PINNED_CHANNELS:
            channels.remove(chat_id)
        log.info("Removed from chat %s (status=%s)", chat_id, status)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.warning("Conflict detected: waiting for previous container shutdown...")
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
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    app.run_polling(
        drop_pending_updates=True,
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
        "Моніторить сайт `oblenergo.cv.ua/shutdowns/` та надсилає візуальну репліку таблиці в Telegram при кожній зміні.")

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