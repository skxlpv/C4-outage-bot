"""
Chernivtsioblenergo Web Replica & Tracker Telegram Bot (Render / HF Edition)

Replicates the exact layout of photo_2026-11-18_14-21-44:
- Top bar with large blue date badge, status, legend, and logo
- Olive green banner
- Group rows: "Г Р У П А" + bold number
- 00:00 to 23:00 hour markers (no :30 sub-labels)
- Zero footer texts

Channel autoposting: add the bot as an administrator (Post Messages) of a
channel and it posts the current table immediately, then re-posts on every
change of the schedule on the site.
"""
from __future__ import annotations

import asyncio
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
COLOR_BG = (235, 238, 242)          # Page background
COLOR_CARD_BG = (255, 255, 255)     # Inner card
COLOR_BLUE_BADGE = (21, 101, 192)   # Deep Royal Blue for Date (#1565C0)
COLOR_GREEN_BANNER = (126, 157, 98) # Olive-Moss Green (#7E9D62)
COLOR_HOURS_BG = (252, 228, 214)    # Peach (#FCE4D6)
COLOR_ON = (199, 211, 159)          # Light Sage Green (#C7D39F)
COLOR_OFF = (239, 154, 154)         # Soft Coral Red (#EF9A9A)
COLOR_MAYBE = (176, 190, 197)       # Soft Slate Gray (#B0BEC5)
COLOR_BORDER = (255, 255, 255)      # White separators
COLOR_DARK_TEXT = (33, 37, 41)
COLOR_MUTED_GRAY = (120, 144, 156)  # Blue-gray for "Г Р У П А"
COLOR_LINE_MARKER = (25, 118, 210)  # Blue time indicator line

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
    height = 700

    # Background card canvas
    im = Image.new("RGB", (width, height), COLOR_BG)
    draw = ImageDraw.Draw(im)

    # Rounded white main container
    draw.rounded_rectangle([16, 16, width - 16, height - 16], radius=14, fill=COLOR_CARD_BG)

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

    # Far-Right: Circular Blue Brand Badge (Х·Ч / C4)
    logo_cx, logo_cy = width - 68, 60
    draw.ellipse([logo_cx - 28, logo_cy - 28, logo_cx + 28, logo_cy + 28], fill=COLOR_BLUE_BADGE)
    draw.text((logo_cx, logo_cy), "Х·Ч", font=font_logo_txt, fill=(255, 255, 255), anchor="mm")

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
    # 6. CURRENT-TIME VERTICAL BLUE LINE
    # ======================================================================= #
    if 0 <= sched.active_slot < 48:
        mark_x = int(grid_x0 + sched.active_slot * slot_w + slot_w // 2)
        draw.line([(mark_x, top_hour_y0), (mark_x, bot_hour_y1)], fill=COLOR_LINE_MARKER, width=2)

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