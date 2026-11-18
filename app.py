"""
Chernivtsioblenergo Web Replica & Tracker Telegram Bot (Render / HF Edition)

Replicates the exact layout of photo_2026-11-18_14-21-44:
- Top bar with large blue date badge, status, legend, and logo
- Olive green banner
- Group rows: "Г Р У П А" + bold number
- 00:00 to 23:00 hour markers (no :30 sub-labels)
- Zero footer texts
- Round C4 channel logo, semi-transparent "Чернівці | С4" watermark

Channel autoposting: add the bot as an administrator (Post Messages) of a
channel and it posts the current table immediately, then re-posts on every
change of the schedule on the site.
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gradio as gr
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# =========================================================================== #
# Configuration & Constants
# =========================================================================== #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUBSCRIBERS_FILE = Path(os.environ.get("SUBSCRIBERS_FILE", "subscribers.json"))
CHANNELS_FILE = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OBLENERGO_URL = os.environ.get("OBLENERGO_URL", "https://oblenergo.cv.ua/shutdowns/")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", "30"))
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

# Visual Colors (Exact match to Screenshot 2)
COLOR_CARD_BG = (255, 255, 255)     # Inner card
COLOR_BLUE_BADGE = (101, 140, 226)  # Date badge (#658CE2)
COLOR_GREEN_BANNER = (116, 140, 66) # Olive banner (#748C42)
COLOR_HOURS_BG = (245, 203, 166)    # Peach / skin hours bar (#F5CBA6)
COLOR_ON = (172, 196, 116)          # Powered - rich sage green (#ACC474)
COLOR_OFF = (225, 98, 84)           # Outage - warm brick red (#E16254)
COLOR_MAYBE = (122, 168, 198)       # Maybe powered - steel blue (#7AA8C6)
COLOR_BORDER = (255, 255, 255)      # White separators
COLOR_DARK_TEXT = (33, 37, 41)
COLOR_MUTED_GRAY = (120, 144, 156)  # Blue-gray for "Г Р У П А"
COLOR_LINE_MARKER = (220, 30, 30)   # Red time indicator line
LINE_MARKER_ALPHA = 77              # 70% transparent (~30% opaque)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full Telegram URLs (which contain the bot token) at INFO level
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("svitlo-replica-bot")


# =========================================================================== #
# Persistence (Subscribers & Channels)
# =========================================================================== #
class SubscriberStore:
    """Persistent set of chat IDs (users have positive IDs, channels negative)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.subscribers: set[int] = set()
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.subscribers = set()
                for uid in data:
                    try:
                        self.subscribers.add(int(uid))
                    except (TypeError, ValueError):
                        pass
                log.info("Loaded %d entries from %s.", len(self.subscribers), self.path)
            except Exception as e:
                log.warning("Could not read %s: %s", self.path, e)

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
        try:
            self.path.write_text(json.dumps(list(self.subscribers), indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed saving %s: %s", self.path, e)


subscribers = SubscriberStore(SUBSCRIBERS_FILE)

channels = SubscriberStore(CHANNELS_FILE)
# Render's disk is ephemeral, so channels can also be pinned via env:
# CHANNEL_IDS="-1001234567890,-100987..."
for _cid in os.environ.get("CHANNEL_IDS", "").split(","):
    _cid = _cid.strip()
    if _cid.lstrip("-").isdigit():
        channels.subscribers.add(int(_cid))


# =========================================================================== #
# Website Scraper Model
# =========================================================================== #
@dataclass
class ScrapedSchedule:
    date_str: str                           # e.g. "18.11.2026"
    upd_time: str                           # e.g. "07:51"
    month_name: str                         # e.g. "Листопад"
    slots: dict[int, list[str]]             # group_id (1..12) -> 48 slots ("з", "в", "мз")
    html_hash: str
    active_slot: int = -1                   # 0..47


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

        return self.parse_html(html_text)

    def parse_html(self, html_text: str) -> Optional[ScrapedSchedule]:
        soup = BeautifulSoup(html_text, "html.parser")
        gsv = soup.select_one("#gsv")
        if not gsv:
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

            if len(q_slots) < 48:
                start_offset = 48 - len(q_slots)
                q_slots = ["з"] * start_offset + q_slots
            group_slots[q] = q_slots[:48]

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
# Replica Image Generator (Pillow) - Replicating Screenshot #2
# =========================================================================== #
# C4 channel logo, pre-rendered as a circular PNG (embedded, so no extra files to deploy)
LOGO_C4_B64 = (
    "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"
)

WATERMARK_TEXT = "Чернівці | С4"
WATERMARK_ALPHA = 48  # 0..255, very transparent

_logo_cache: dict[int, Image.Image] = {}


def get_logo(diameter: int) -> Optional[Image.Image]:
    if diameter in _logo_cache:
        return _logo_cache[diameter]
    try:
        raw = Image.open(io.BytesIO(base64.b64decode(LOGO_C4_B64))).convert("RGBA")
        _logo_cache[diameter] = raw.resize((diameter, diameter), Image.LANCZOS)
        return _logo_cache[diameter]
    except Exception as e:
        log.warning("Logo load failed: %s", e)
        return None


def apply_watermark(base: Image.Image, text: str, alpha: int) -> Image.Image:
    """Overlay a large, slightly rotated, semi-transparent text in the middle of the image."""
    w, h = base.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    wm_font = load_font(96, bold=True)
    ld.text((w // 2, h // 2 + 30), text, font=wm_font, fill=(33, 37, 41, alpha), anchor="mm")
    layer = layer.rotate(8, resample=Image.BICUBIC, center=(w // 2, h // 2))
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
    """
    Renders the exact replica from screenshot #2:
    - No :30 sub-labels on hour bars
    - No footer text
    - Legend repositioned in top bar with zero text drop-out
    - Authentic blue date badge, olive banner, and group formatting
    """
    width = 1440
    height = 660

    # Plain white background (no outer canvas / rounded card)
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

    # ======================================================================= #
    # 1. TOP NAVIGATION / HEADER BAR (y: 28 to 92)
    # ======================================================================= #
    # Left: Big Royal Blue Date Badge
    draw.rounded_rectangle([32, 28, 250, 92], radius=12, fill=COLOR_BLUE_BADGE)
    draw.text((141, 60), sched.date_str, font=font_date_big, fill=(255, 255, 255), anchor="mm")

    # Center-Left: Clock icon + Status text
    clock_cx, clock_cy = 280, 60
    draw.ellipse([clock_cx - 11, clock_cy - 11, clock_cx + 11, clock_cy + 11], outline=COLOR_DARK_TEXT, width=2)
    draw.line([(clock_cx, clock_cy), (clock_cx, clock_cy - 6)], fill=COLOR_DARK_TEXT, width=2)
    draw.line([(clock_cx, clock_cy), (clock_cx + 5, clock_cy)], fill=COLOR_DARK_TEXT, width=2)

    status_str = f"Графік актуальний станом на {sched.upd_time}"
    draw.text((302, 60), status_str, font=font_status_txt, fill=COLOR_DARK_TEXT, anchor="lm")

    # Center-Right: Legend (Repositioned to ensure zero text drop-out)
    leg_start_x = 680
    badges = [
        ("В", "Відключені", COLOR_OFF, 30),
        ("З", "Заживлені", COLOR_ON, 30),
        ("МЗ", "Можливо заживлені", COLOR_MAYBE, 38),
    ]

    bx = leg_start_x
    for tag, label, color, box_w in badges:
        draw.rounded_rectangle([bx, 45, bx + box_w, 75], radius=4, fill=color)
        draw.text((bx + box_w // 2, 60), tag, font=font_legend_box, fill=COLOR_DARK_TEXT, anchor="mm")
        bx += box_w + 8
        draw.text((bx, 60), label, font=font_legend_lbl, fill=COLOR_DARK_TEXT, anchor="lm")
        bx += int(len(label) * 8.0) + 18

    # Far-Right: round C4 channel logo
    logo_d = 60
    logo_cx, logo_cy = width - 68, 60
    logo_img = get_logo(logo_d)
    if logo_img is not None:
        im.paste(logo_img, (logo_cx - logo_d // 2, logo_cy - logo_d // 2), logo_img)
    else:
        draw.ellipse([logo_cx - 30, logo_cy - 30, logo_cx + 30, logo_cy + 30], fill=(237, 28, 36))
        draw.text((logo_cx, logo_cy), "C4", font=font_logo_txt, fill=(255, 255, 255), anchor="mm")

    # ======================================================================= #
    # 2. GREEN BANNER (y: 108 to 152)
    # ======================================================================= #
    draw.rounded_rectangle([32, 108, width - 32, 152], radius=8, fill=COLOR_GREEN_BANNER)
    draw.text((52, 130), sched.month_name.capitalize(), font=font_banner_txt, fill=COLOR_DARK_TEXT, anchor="lm")
    draw.text((width // 2 + 50, 130), "Графік погодинних відключень", font=font_banner_txt, fill=COLOR_DARK_TEXT, anchor="mm")

    # ======================================================================= #
    # 3. TABLE GRID GEOMETRY
    # ======================================================================= #
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

    # Top Hours Row Background
    draw.rectangle([left_col_x0, top_hour_y0, grid_x1, top_hour_y1], fill=COLOR_HOURS_BG)
    # Left Header Date above "ГРУПА"
    draw.text(
        (left_col_x0 + left_col_w // 2, (top_hour_y0 + top_hour_y1) // 2),
        sched.date_str, font=font_header_h, fill=COLOR_DARK_TEXT, anchor="mm",
    )

    # Top Hours: ONLY full hours (00:00 .. 23:00), NO :30 sub-labels
    for h in range(24):
        hx = grid_x0 + h * hour_w
        draw.line([(hx, top_hour_y0), (hx, top_hour_y1)], fill=COLOR_BORDER, width=1)
        draw.text((hx + hour_w / 2, (top_hour_y0 + top_hour_y1) // 2), f"{h:02d}:00", font=font_hour_h, fill=COLOR_DARK_TEXT, anchor="mm")

    # ======================================================================= #
    # 4. RENDER 12 GROUP ROWS
    # ======================================================================= #
    for q in range(1, 13):
        ry0 = grid_y0 + (q - 1) * row_h
        ry1 = ry0 + row_h

        # Left Column: "Г Р У П А" + bold group number
        bg_grp = (255, 255, 255) if q % 2 != 0 else (248, 250, 252)
        draw.rectangle([left_col_x0, ry0, grid_x0, ry1], fill=bg_grp)
        draw.line([(left_col_x0, ry1), (grid_x1, ry1)], fill=COLOR_BORDER, width=1)

        # Spaced out "Г Р У П А" on left, large number on right
        draw.text((left_col_x0 + 12, (ry0 + ry1) // 2), "Г Р У П А", font=font_grp_lbl, fill=COLOR_MUTED_GRAY, anchor="lm")
        draw.text((grid_x0 - 18, (ry0 + ry1) // 2), str(q), font=font_grp_num, fill=COLOR_DARK_TEXT, anchor="rm")

        # 48 Grid Cells
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

            # Cell with clean 1px white borders
            draw.rectangle([sx0, ry0, sx1, ry1], fill=fill_c, outline=COLOR_BORDER, width=1)
            # Centered text abbreviation
            draw.text(((sx0 + sx1) // 2, (ry0 + ry1) // 2), state, font=font_cell, fill=COLOR_DARK_TEXT, anchor="mm")

    # ======================================================================= #
    # 5. BOTTOM HOURS ROW (No :30 sub-labels)
    # ======================================================================= #
    draw.rectangle([left_col_x0, bot_hour_y0, grid_x1, bot_hour_y1], fill=COLOR_HOURS_BG)
    for h in range(24):
        hx = grid_x0 + h * hour_w
        draw.line([(hx, bot_hour_y0), (hx, bot_hour_y1)], fill=COLOR_BORDER, width=1)
        draw.text((hx + hour_w / 2, (bot_hour_y0 + bot_hour_y1) // 2), f"{h:02d}:00", font=font_hour_h, fill=COLOR_DARK_TEXT, anchor="mm")

    # ======================================================================= #
    # 5b. WATERMARK (very transparent)
    # ======================================================================= #
    im = apply_watermark(im, WATERMARK_TEXT, WATERMARK_ALPHA)
    draw = ImageDraw.Draw(im)

    # ======================================================================= #
    # 6. CURRENT-TIME LINE (red, 70% transparent, table rows only - not on hour bars)
    # ======================================================================= #
    if 0 <= sched.active_slot < 48:
        mark_x = int(grid_x0 + sched.active_slot * slot_w + slot_w // 2)
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
async def broadcast(bot, sched: ScrapedSchedule, changed: bool = True) -> None:
    png_bytes = render_replica_table(sched)
    caption = build_caption(sched, changed)

    targets = [(uid, subscribers) for uid in list(subscribers.subscribers)] + \
              [(cid, channels) for cid in list(channels.subscribers)]

    for chat_id, store in targets:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(png_bytes),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except (Forbidden, BadRequest) as e:
            log.warning("Removing %s: %s", chat_id, e)
            store.remove(chat_id)
        except Exception as e:
            log.warning("Failed posting to %s: %s", chat_id, e)
        await asyncio.sleep(0.05)


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
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


async def send_schedule_replica(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    sched = scraper.last_schedule
    if not sched:
        sched = await scraper.fetch()
        if sched:
            scraper.last_schedule = sched

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
        sched = await scraper.fetch()
        if sched:
            scraper.last_schedule = sched
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
                # "message is not modified" -> nothing changed, that's fine
                if "not modified" not in str(e).lower():
                    await send_schedule_replica(update, ctx)
            except Exception:
                await send_schedule_replica(update, ctx)


async def on_text_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text in ("📊 Актуальний графік", "/table"):
        await send_schedule_replica(update, ctx)
    elif text in ("🔄 Оновити", "/check"):
        sched = await scraper.fetch()
        if sched:
            scraper.last_schedule = sched
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
    if not cm or cm.chat.type != "channel":
        return

    chat_id = cm.chat.id
    status = cm.new_chat_member.status

    if status == "administrator":
        channels.add(chat_id)
        log.info("Added to channel %s (%s)", chat_id, cm.chat.title)

        sched = scraper.last_schedule or await scraper.fetch()
        if sched:
            scraper.last_schedule = sched
            try:
                await ctx.bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(render_replica_table(sched)),
                    caption=build_caption(sched),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.warning("Initial channel post failed: %s", e)
    elif status in ("left", "kicked"):
        channels.remove(chat_id)
        log.info("Removed from channel %s", chat_id)


# =========================================================================== #
# Background Monitoring Job
# =========================================================================== #
async def check_site_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    new_sched = await scraper.fetch()
    if not new_sched:
        return

    old_sched = scraper.last_schedule
    if old_sched is None:
        scraper.last_schedule = new_sched
        log.info("Initialized scraper: date=%s, upd=%s", new_sched.date_str, new_sched.upd_time)
        return

    has_changed = (
        new_sched.date_str != old_sched.date_str
        or new_sched.upd_time != old_sched.upd_time
        or new_sched.html_hash != old_sched.html_hash
    )

    if not has_changed:
        return

    log.info("🔔 Schedule updated on site! Date: %s, Upd: %s", new_sched.date_str, new_sched.upd_time)
    scraper.last_schedule = new_sched

    await broadcast(ctx.bot, new_sched, changed=True)


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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("table", send_schedule_replica))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(check_site_job, interval=CHECK_INTERVAL_SECONDS, first=5)
    else:
        log.warning("JobQueue not initialized. Ensure python-telegram-bot[job-queue] is installed.")

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
        f"Перевірка: кожні {CHECK_INTERVAL_SECONDS}с"
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
    gr.Markdown("Моніторить сайт `oblenergo.cv.ua/shutdowns/` та надсилає візуальну репліку таблиці в Telegram при кожній зміні.")

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