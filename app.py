"""
Chernivtsioblenergo Web Replica & Tracker Telegram Bot (HF Space Edition)

Tracks https://oblenergo.cv.ua/shutdowns/ by monitoring:
- Schedule Date (<div id="gsv_t">)
- Update Timestamp "Станом на" (<div id="gsv_a">)
- Grid State for Groups 1..12 across 48 half-hour slots

Renders an authentic, identical image replica of their website table and sends it to Telegram.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gradio as gr
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
HF_TOKEN = os.environ.get("HF_TOKEN")
DATASET_REPO = os.environ.get("DATASET_REPO", "")
SUBSCRIBERS_FILE = Path(os.environ.get("SUBSCRIBERS_FILE", "subscribers.json"))
OBLENERGO_URL = os.environ.get("OBLENERGO_URL", "https://oblenergo.cv.ua/shutdowns/")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", "30"))
TZ = ZoneInfo("Europe/Kyiv")

# Scraper Headers from user's cURL
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

# Website Color Palette (Matching their CSS)
COLOR_ON = (199, 211, 159)      # #C7D39F - Заживлені (Light Green)
COLOR_OFF = (229, 115, 115)     # #E57373 - Відключені (Warm Coral/Red)
COLOR_MAYBE = (144, 164, 174)   # #90A4AE - Можливо заживлені (Slate Gray)
COLOR_HEADER_BG = (67, 96, 45)  # #43602D - Olive Green Top Bar
COLOR_HOURS_BG = (248, 224, 208)# #F8E0D0 - Peach Hour Header Bar
COLOR_BORDER = (255, 255, 255)  # #FFFFFF - Cell Separator Lines
COLOR_DARK_TEXT = (43, 44, 48)  # Charcoal
COLOR_MUTED_TEXT = (100, 105, 115)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("svitlo-replica-bot")


# =========================================================================== #
# Persistence (Subscribers)
# =========================================================================== #
class SubscriberStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.subscribers: set[int] = set()
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.subscribers = {int(uid) for uid in data if str(uid).isdigit()}
                log.info("Loaded %d subscribers.", len(self.subscribers))
            except Exception as e:
                log.warning("Could not read subscribers: %s", e)

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
            log.warning("Failed saving subscribers: %s", e)

subscribers = SubscriberStore(SUBSCRIBERS_FILE)


# =========================================================================== #
# Website Scraper Model
# =========================================================================== #
@dataclass
class ScrapedSchedule:
    date_str: str                           # e.g. "18.11.2026"
    upd_time: str                           # e.g. "13:30"
    opened_time: str                        # e.g. "13:42"
    month_name: str                         # e.g. "Листопад"
    slots: dict[int, list[str]]             # group_id (1..12) -> list of 48 tokens ("з", "в", "мз")
    html_hash: str
    active_slot: int = -1                   # current time slot index (0..47)

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
                timeout=aiohttp.ClientTimeout(total=10)
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

        # 3. Page open timestamp
        actual_el = soup.select_one("#gsv_actual")
        actual_txt = actual_el.get_text() if actual_el else ""
        m_act = re.search(r"(\d{1,2}:\d{2})", actual_txt)
        opened_time = m_act.group(1) if m_act else ""

        # 4. Month header
        month_el = soup.select_one("#gsv_h p")
        month_name = month_el.get_text().strip() if month_el else "Графік відключень"

        # 5. Extract slots for all 12 groups
        # In HTML: <div id="inf{q}" data-id="{q}"> with children <u>, <o>, <s>
        group_slots: dict[int, list[str]] = {}

        for q in range(1, 13):
            div = soup.select_one(f"#inf{q}") or soup.select_one(f'div[data-id="{q}"]')
            if not div:
                continue

            # Child elements in exact sequential order
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

            # Pad or trim to exactly 48 slots
            if len(q_slots) < 48:
                # If page opened midday and earlier slots were truncated
                start_offset = 48 - len(q_slots)
                q_slots = ["з"] * start_offset + q_slots
            group_slots[q] = q_slots[:48]

        # Calculate active current slot
        now = datetime.now(TZ)
        active_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)

        # Hash of data to detect genuine state changes
        content_repr = f"{date_str}_{upd_time}_{json.dumps(group_slots, sort_keys=True)}"
        content_hash = hashlib.md5(content_repr.encode()).hexdigest()

        return ScrapedSchedule(
            date_str=date_str,
            upd_time=upd_time,
            opened_time=opened_time,
            month_name=month_name,
            slots=group_slots,
            html_hash=content_hash,
            active_slot=active_slot,
        )

scraper = OblenergoScraper()


# =========================================================================== #
# Replica Image Generator (Pillow)
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
    Renders an authentic, visually identical replica of the Oblenergo HTML grid.
    """
    width = 1260
    height = 570
    im = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(im)

    font_banner = load_font(18, bold=True)
    font_sub = load_font(13, bold=False)
    font_bold = load_font(13, bold=True)
    font_header_h = load_font(10, bold=True)
    font_header_sub = load_font(8, bold=False)
    font_cell = load_font(11, bold=True)
    font_grp = load_font(12, bold=True)

    # 1. Top Green Banner (#43602D)
    draw.rectangle([0, 0, width, 48], fill=COLOR_HEADER_BG)
    draw.text((20, 24), sched.month_name.upper(), font=font_banner, fill=(255, 255, 255), anchor="lm")
    draw.text((width - 20, 24), "ГРАФІК ПОГОДИННИХ ВІДКЛЮЧЕНЬ", font=font_banner, fill=(255, 255, 255), anchor="rm")

    # 2. Date Badge & "Станом на" Sub-Banner
    draw.rectangle([0, 48, width, 92], fill=(245, 247, 250))
    # Date chip
    draw.rounded_rectangle([20, 56, 140, 84], radius=4, fill=(255, 255, 255), outline=(200, 205, 215), width=1)
    draw.text((80, 70), sched.date_str, font=font_bold, fill=COLOR_DARK_TEXT, anchor="mm")

    # Updated timestamp
    upd_text = f"Графік актуальний станом на {sched.upd_time}"
    draw.text((155, 70), upd_text, font=font_bold, fill=COLOR_DARK_TEXT, anchor="lm")

    # 3. Legend (В, З, МЗ)
    leg_x = width - 420
    draw.text((leg_x, 70), "Умовна позначка:", font=font_sub, fill=COLOR_MUTED_TEXT, anchor="lm")

    badges = [
        ("В", "Відключені", COLOR_OFF),
        ("З", "Заживлені", COLOR_ON),
        ("МЗ", "Можливо заживлені", COLOR_MAYBE),
    ]
    cur_bx = leg_x + 125
    for tag, label, color in badges:
        bw = 28 if len(tag) > 1 else 20
        draw.rectangle([cur_bx, 60, cur_bx + bw, 80], fill=color)
        draw.text((cur_bx + bw // 2, 70), tag, font=font_cell, fill=COLOR_DARK_TEXT, anchor="mm")
        cur_bx += bw + 6
        draw.text((cur_bx, 70), label, font=font_sub, fill=COLOR_DARK_TEXT, anchor="lm")
        cur_bx += int(len(label) * 7.5) + 14

    # 4. Table Grid Geometry
    left_col_w = 96
    grid_x0 = left_col_w
    grid_x1 = width - 14
    grid_w = grid_x1 - grid_x0
    slot_w = grid_w / 48

    hours_h = 32
    row_h = 28
    grid_y0 = 96 + hours_h

    # Top Hours Header Background
    draw.rectangle([0, 96, width, grid_y0], fill=COLOR_HOURS_BG)
    draw.rectangle([0, 96, left_col_w, grid_y0], fill=(235, 210, 195))
    draw.text((left_col_w // 2, 96 + hours_h // 2), "ГРУПА", font=font_bold, fill=COLOR_DARK_TEXT, anchor="mm")

    # Draw Hour Markers 00..23
    for h in range(24):
        hx = grid_x0 + h * (slot_w * 2)
        # Hour divider
        draw.line([(hx, 96), (hx, grid_y0)], fill=COLOR_BORDER, width=1)
        # Hour text
        draw.text((hx + slot_w, 104), f"{h:02d}:00", font=font_header_h, fill=COLOR_DARK_TEXT, anchor="mm")
        draw.text((hx + slot_w * 1.5, 118), f"{h:02d}:30", font=font_header_sub, fill=COLOR_MUTED_TEXT, anchor="mm")

    # 5. Render 12 Group Rows
    for q in range(1, 13):
        ry0 = grid_y0 + (q - 1) * row_h
        ry1 = ry0 + row_h

        # Group Label on Left
        bg_grp = (255, 255, 255) if q % 2 != 0 else (248, 250, 252)
        draw.rectangle([0, ry0, left_col_w, ry1], fill=bg_grp)
        draw.line([(0, ry1), (width, ry1)], fill=COLOR_BORDER, width=1)
        draw.text((left_col_w // 2, (ry0 + ry1) // 2), f"Група {q}", font=font_grp, fill=COLOR_DARK_TEXT, anchor="mm")

        # 48 Slots
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

            # Cell rectangle with white borders
            draw.rectangle([sx0, ry0, sx1, ry1], fill=fill_c, outline=COLOR_BORDER, width=1)
            # Centered text abbreviation
            draw.text(((sx0 + sx1) // 2, (ry0 + ry1) // 2), state, font=font_cell, fill=COLOR_DARK_TEXT, anchor="mm")

    # 6. Bottom Hours Bar (Repeat for easy reading)
    bot_y0 = grid_y0 + 12 * row_h
    bot_y1 = bot_y0 + 26
    draw.rectangle([0, bot_y0, width, bot_y1], fill=COLOR_HOURS_BG)
    for h in range(24):
        hx = grid_x0 + h * (slot_w * 2)
        draw.line([(hx, bot_y0), (hx, bot_y1)], fill=COLOR_BORDER, width=1)
        draw.text((hx + slot_w, bot_y0 + 13), f"{h:02d}:00", font=font_header_h, fill=COLOR_DARK_TEXT, anchor="mm")

    # 7. Current Time Red Indicator Line
    if 0 <= sched.active_slot < 48:
        mark_x = int(grid_x0 + sched.active_slot * slot_w + slot_w // 2)
        draw.line([(mark_x, 96), (mark_x, bot_y1)], fill=(211, 47, 47), width=3)

    # 8. Footer Info
    footer_y = bot_y1 + 4
    draw.text((20, footer_y + 10), "Офіційне джерело: АТ «Чернівціобленерго» (oblenergo.cv.ua)", font=font_sub, fill=COLOR_MUTED_TEXT, anchor="lm")
    if sched.opened_time:
        draw.text((width - 20, footer_y + 10), f"Зріз сторінки: {sched.opened_time}", font=font_sub, fill=COLOR_MUTED_TEXT, anchor="rm")

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

def help_text() -> str:
    return (
        "💡 <b>Бот моніторингу графіків Чернівціобленерго</b>\n\n"
        "Бот підключається напряму до сайту <code>oblenergo.cv.ua/shutdowns/</code>, "
        "відстежує дату та рядок <b>«Станом на»</b>, і генерує точну копію офіційної таблиці відключень.\n\n"
        "<b>Команди:</b>\n"
        "• 📊 <b>Актуальний графік</b> — отримати поточну копію таблиці з сайту.\n"
        "• ⏱ <b>Статус сайту</b> — перевірити дату та час публікації.\n"
        "• 🔄 <b>Оновити</b> — примусово перевірити зміни прямо зараз.\n"
        "• 🔔 Всі користувачі, які натиснули /start, автоматично отримують сповіщення при будь-якій зміні графіка."
    )


# =========================================================================== #
# Bot Handlers
# =========================================================================== #
async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    is_new = subscribers.add(uid)

    msg = (
        "👋 <b>Вітаємо!</b>\n\n"
        "Ви успішно підписалися на сповіщення про зміни графіка з сайту <b>Чернівціобленерго</b>.\n"
        "Щойно обленерго оновить статус або змінить години відключень, бот надішле вам точну копію таблиці."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    # Send current table right away
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
        await ctx.bot.send_message(chat_id, f"🔴 <b>Не вдалося завантажити графік з сайту</b>\n<i>{err_msg}</i>", parse_mode=ParseMode.HTML)
        return

    png_bytes = render_replica_table(sched)
    caption = (
        f"📊 <b>Графік відключень з сайту Обленерго</b>\n"
        f"📅 Дата: <b>{sched.date_str}</b>\n"
        f"⏱ Станом на: <b>{sched.upd_time}</b>"
    )

    inline_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Оновити", callback_data="btn_refresh")
    ]])

    await ctx.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(png_bytes),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb
    )

async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sched = scraper.last_schedule
    if not sched:
        await update.effective_message.reply_text("⏳ Дані ще завантажуються, спробуйте через декілька секунд.")
        return

    text = (
        f"🌐 <b>Статус сайту Чернівціобленерго:</b>\n\n"
        f"• 📅 Дата в графіку: <b>{sched.date_str}</b>\n"
        f"• ⏱ Актуально станом на: <b>{sched.upd_time}</b>\n"
        f"• 🕒 Зріз даних: <b>{sched.opened_time or 'нещодавно'}</b>\n"
        f"• 👥 Підписників бота: <b>{len(subscribers.subscribers)}</b>\n"
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
            caption = (
                f"📊 <b>Графік відключень з сайту Обленерго</b>\n"
                f"📅 Дата: <b>{sched.date_str}</b>\n"
                f"⏱ Станом на: <b>{sched.upd_time}</b>"
            )
            try:
                await q.edit_message_media(
                    media=io.BytesIO(png_bytes),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
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


# =========================================================================== #
# Background Monitoring Job
# =========================================================================== #
async def check_site_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every CHECK_INTERVAL_SECONDS. Detects changes in date, timestamp, or grid."""
    new_sched = await scraper.fetch()
    if not new_sched:
        return

    old_sched = scraper.last_schedule

    # First run: initialize without broadcasting
    if old_sched is None:
        scraper.last_schedule = new_sched
        log.info("Initialized scraper: date=%s, upd=%s", new_sched.date_str, new_sched.upd_time)
        return

    # Change detection: date changed, 'Станом на' timestamp changed, or cell contents changed
    has_changed = (
        new_sched.date_str != old_sched.date_str or
        new_sched.upd_time != old_sched.upd_time or
        new_sched.html_hash != old_sched.html_hash
    )

    if not has_changed:
        return

    log.info("🔔 Schedule updated on site! Date: %s, Upd: %s", new_sched.date_str, new_sched.upd_time)
    scraper.last_schedule = new_sched

    # Render image once
    png_bytes = render_replica_table(new_sched)
    caption = (
        f"🔄 <b>Графік на сайті Обленерго змінився!</b>\n\n"
        f"📅 Дата: <b>{new_sched.date_str}</b>\n"
        f"⏱ Станом на: <b>{new_sched.upd_time}</b>"
    )

    # Broadcast to all registered subscribers
    uids = list(subscribers.subscribers)
    for uid in uids:
        try:
            await ctx.bot.send_photo(
                chat_id=uid,
                photo=io.BytesIO(png_bytes),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Forbidden:
            # User blocked the bot
            subscribers.remove(uid)
        except Exception as e:
            log.warning("Failed alerting %s: %s", uid, e)
        await asyncio.sleep(0.05)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.warning("Conflict detected: waiting for previous container shutdown...")
        return
    log.error("Update error: %s", context.error)


# =========================================================================== #
# Bot Thread & Gradio App (HF Space Entrypoint)
# =========================================================================== #
def run_bot() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing! Set it in Space Secrets.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("table", send_schedule_replica))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    # Repeating job queue
    app.job_queue.run_repeating(check_site_job, interval=CHECK_INTERVAL_SECONDS, first=5)
    app.run_polling(drop_pending_updates=True, stop_signals=None)

def get_dashboard_data():
    sched = scraper.last_schedule
    date_val = sched.date_str if sched else "Немає даних"
    upd_val = sched.upd_time if sched else "Немає даних"
    sub_count = len(subscribers.subscribers)
    status_str = f"Активний | Підписників: {sub_count} | Перевірка: кожні {CHECK_INTERVAL_SECONDS}с"

    img_preview = None
    if sched:
        try:
            raw_png = render_replica_table(sched)
            img_preview = Image.open(io.BytesIO(raw_png))
        except Exception:
            pass

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
    demo.launch(server_name="0.0.0.0", server_port=7860)