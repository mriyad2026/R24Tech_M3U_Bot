#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
M3U Smart Toolkit — Telegram Bot (R24Tech)
Updated: Global multi‑user, URL input, live progress, channel lists,
add channels, preserve comments, admin panel with official playlists.
"""

import os
import re
import sys
import json
import csv
import asyncio
import aiohttp
import logging
import io
import time
import math
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any, Callable

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode

# --- Admin Config (change this) ---
ADMIN_ID = 6385435108  # Replace with your Telegram user ID (integer)
ADMIN_FILE = "admin_data.json"  # Stores bot settings and official playlists

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================================================================
# PARSER & CORE LOGIC (with comment preservation)
# =============================================================================

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp")
VIDEO_AUDIO_CT_PREFIX = ("video/", "audio/")
HLS_CTS = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "application/mpegurl",
)
TEXTY_CTS = ("text/plain", "text/html", "application/json", "application/xml")
OK_STATUSES = (200, 206)

def is_http_url(s: str) -> bool:
    s = s.strip().lower()
    return s.startswith("http://") or s.startswith("https://")

def is_commented(s: str) -> bool:
    return s.strip().startswith("#")

def looks_like_image_url(url: str) -> bool:
    u = url.split("?", 1)[0].split("#", 1)[0].strip().lower()
    return u.endswith(IMAGE_EXTS)

def first_valid_stream(urls: List[str]) -> Optional[str]:
    for u in urls:
        if is_http_url(u) and (not is_commented(u)) and (not looks_like_image_url(u)):
            return u.strip()
    return None

def extract_attr(extinf: str, attr: str) -> Optional[str]:
    m = re.search(rf'{re.escape(attr)}="([^"]*)"', extinf, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None

def extract_name_from_extinf(extinf: str) -> str:
    p = extinf.rsplit(",", 1)
    if len(p) == 2:
        return p[1].strip()
    return "Unknown"

def parse_m3u_from_content(filename: str, content: str) -> List[Dict[str, Any]]:
    blocks = []
    lines = content.splitlines()
    current_lines = []
    current_extinf = None
    current_urls = []

    def flush_block():
        nonlocal current_lines, current_extinf, current_urls
        if current_extinf:
            name = extract_name_from_extinf(current_extinf)
            blocks.append({
                "source": filename,
                "extinf": current_extinf,
                "name": name,
                "logo": extract_attr(current_extinf, "tvg-logo"),
                "group": extract_attr(current_extinf, "group-title"),
                "urls": [u for u in current_urls if u.strip()],
                "lines": current_lines.copy(),
            })
        current_lines = []
        current_extinf = None
        current_urls = []

    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("#EXTINF"):
            flush_block()
            current_extinf = line
            current_lines.append(line)
        else:
            if current_extinf is not None:
                current_lines.append(line)
                if is_http_url(line):
                    current_urls.append(line.strip())
            else:
                pass
    flush_block()
    return blocks

def merge_entries(list_of_lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged = []
    seen = set()
    for entries in list_of_lists:
        for e in entries:
            url = first_valid_stream(e["urls"]) or ""
            key = (e["name"].strip().lower(), url.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(e)
    return merged

def drop_group_title(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for e in entries:
        new_lines = []
        for line in e["lines"]:
            if line.startswith("#EXTINF"):
                new_line = re.sub(r'\s*group-title="[^"]*"', "", line, flags=re.IGNORECASE)
                new_line = re.sub(r'\s+,', ' ,', new_line)
                new_lines.append(new_line.strip())
                e["extinf"] = new_line.strip()
            else:
                new_lines.append(line)
        ee = dict(e)
        ee["lines"] = new_lines
        out.append(ee)
    return out

def toggle_comment(entries: List[Dict[str, Any]], contains: str, comment: bool) -> List[Dict[str, Any]]:
    kw = contains.lower()
    out = []
    for e in entries:
        if kw in e["name"].lower():
            new_lines = []
            for line in e["lines"]:
                if comment:
                    if not line.lstrip().startswith("#"):
                        new_lines.append("#" + line)
                    else:
                        new_lines.append(line)
                else:
                    if line.lstrip().startswith("#"):
                        new_lines.append(line.lstrip("#").lstrip())
                    else:
                        new_lines.append(line)
            ee = dict(e)
            ee["lines"] = new_lines
            for ln in new_lines:
                if ln.startswith("#EXTINF"):
                    ee["extinf"] = ln
                    ee["name"] = extract_name_from_extinf(ln)
                    ee["logo"] = extract_attr(ln, "tvg-logo")
                    ee["group"] = extract_attr(ln, "group-title")
                elif is_http_url(ln) and not ln.lstrip().startswith("#"):
                    ee["urls"].append(ln.strip())
            out.append(ee)
        else:
            out.append(e)
    return out

def compare_by_name(entries_a: List[Dict[str, Any]], entries_b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    names_a = {e["name"].strip().lower() for e in entries_a}
    uniq_b = []
    seen = set()
    for e in entries_b:
        key = e["name"].strip().lower()
        if key not in names_a and key not in seen:
            uniq_b.append(e)
            seen.add(key)
    return uniq_b

def render_clean_playlist(entries: List[Dict[str, Any]]) -> str:
    lines = ["#EXTM3U\n"]
    for e in entries:
        for ln in e["lines"]:
            lines.append(ln + ("\n" if not ln.endswith("\n") else ""))
        lines.append("\n")
    return "".join(lines)

def render_numbered_playlist(entries: List[Dict[str, Any]], only_active: Optional[Dict[str, str]] = None) -> str:
    filtered = []
    for e in entries:
        if only_active is not None:
            st = only_active.get(e["name"], "")
            if st != "🟢":
                continue
        is_movie = any(re.search(r'(#movie|#vod|#film)', ln, re.IGNORECASE) for ln in e["lines"])
        if is_movie:
            continue
        filtered.append(e)

    numbered = []
    for e in filtered:
        url = first_valid_stream(e["urls"])
        if not url:
            continue
        numbered.append(e)
    width = max(3, len(str(len(numbered))))
    out = ["#EXTM3U\n"]
    for idx, e in enumerate(numbered, start=1):
        out.append(f"#Channel → {idx:0{width}d}\n")
        for ln in e["lines"]:
            out.append(ln + ("\n" if not ln.endswith("\n") else ""))
        out.append("\n")
    return "".join(out)

def render_analyzed_report(entries: List[Dict[str, Any]], status_map: Dict[str, str]) -> str:
    out = ["#EXTM3U\n"]
    for e in entries:
        emoji = status_map.get(e["name"], "🟡")
        out.append(f"# {emoji} {e['name']}\n")
        for ln in e["lines"]:
            out.append(ln + ("\n" if not ln.endswith("\n") else ""))
        out.append("\n")
    return "".join(out)

# ---------------------- Analyzer with Progress ---------------------- #
async def sniff_url(session: aiohttp.ClientSession, url: str, timeout_s: float = 7.5) -> Tuple[str, str]:
    try:
        headers = {"Range": "bytes=0-1023"}
        async with session.get(url, headers=headers, timeout=timeout_s) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "").lower()
            content = await resp.content.read(1024)
            if status in OK_STATUSES:
                if ctype.startswith(VIDEO_AUDIO_CT_PREFIX) or ctype in HLS_CTS:
                    return "🟢", f"OK {status} {ctype or ''}".strip()
                sniff = content[:256].decode("latin-1", errors="ignore")
                if "#EXTM3U" in sniff:
                    return "🟢", f"OK {status} HLS playlist"
                if any(ctype.startswith(t) for t in TEXTY_CTS) or ("<html" in sniff.lower()) or ("{" in sniff[:5]):
                    return "🔵", f"OK {status} but not media"
                return "🟢", f"OK {status} unknown-media"
            else:
                return "🔴", f"HTTP {status}"
    except asyncio.TimeoutError:
        return "🔴", "timeout"
    except Exception as ex:
        return "🔴", f"error {type(ex).__name__}"

async def analyze_entries(
    entries: List[Dict[str, Any]],
    max_conn: int = 32,
    progress_callback: Optional[Callable[[int, int, str, float], None]] = None
) -> Tuple[Dict[str, str], Dict[str, str]]:
    status_map: Dict[str, str] = {}
    reason_map: Dict[str, str] = {}
    total = len(entries)
    processed = 0
    start_time = time.time()
    lock = asyncio.Lock()
    last_update_time = 0

    connector = aiohttp.TCPConnector(limit=max_conn, ssl=False)
    timeout = aiohttp.ClientTimeout(total=10)

    async def worker(name: str, url: Optional[str]):
        nonlocal processed, last_update_time
        if not url:
            status_map[name] = "🟡"
            reason_map[name] = "No stream URL"
        elif looks_like_image_url(url):
            status_map[name] = "🔵"
            reason_map[name] = "Looks like image URL"
        else:
            async with sem:
                emoji, reason = await sniff_url(session, url)
                status_map[name] = emoji
                reason_map[name] = reason

        async with lock:
            processed += 1
            now = time.time()
            if progress_callback and (now - last_update_time >= 1.0 or processed == total):
                elapsed = now - start_time
                eta = (elapsed / processed) * (total - processed) if processed > 0 else 0
                await progress_callback(processed, total, name, eta)
                last_update_time = now

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        sem = asyncio.Semaphore(max_conn)
        tasks = []
        for e in entries:
            url = first_valid_stream(e["urls"])
            tasks.append(asyncio.create_task(worker(e["name"], url)))
        await asyncio.gather(*tasks, return_exceptions=True)

    return status_map, reason_map

# =============================================================================
# TELEGRAM BOT LOGIC
# =============================================================================

# --- States ---
(
    SELECTING_FILES,
    MAIN_MENU,
    AWAITING_COMMENT_FILTER,
    AWAITING_UNCOMMENT_FILTER,
    AWAITING_COMPARE_A,
    AWAITING_COMPARE_B,
    AWAITING_URL,
    AWAITING_ADD_CHANNEL_FILE,
    AWAITING_ADD_CHANNEL_POSITION,
    ADMIN_PANEL,
    ADMIN_MANAGE_PLAYLISTS,
    ADMIN_ADD_PLAYLIST_NAME,
    ADMIN_ADD_PLAYLIST_URL,
) = range(13)

# --- Admin data persistence ---
def load_admin_data():
    if os.path.exists(ADMIN_FILE):
        with open(ADMIN_FILE, 'r') as f:
            return json.load(f)
    return {
        "bot_on": True,
        "menus": {
            "1": {"enabled": True, "order": 1, "label": "Analyze channels 🟢🟡🔴🔵"},
            "2": {"enabled": True, "order": 2, "label": "Reformat numbered playlist"},
            "3": {"enabled": True, "order": 3, "label": "Merge & export clean"},
            "4": {"enabled": True, "order": 4, "label": "Remove categories"},
            "5": {"enabled": True, "order": 5, "label": "Comment by filter"},
            "6": {"enabled": True, "order": 6, "label": "UN-comment by filter"},
            "7": {"enabled": True, "order": 7, "label": "Compare (B not in A)"},
            "8": {"enabled": True, "order": 8, "label": "Export Active-only (needs Analyze)"},
            "9": {"enabled": True, "order": 9, "label": "Show All Channels"},
            "10": {"enabled": True, "order": 10, "label": "Show Active Channels"},
            "11": {"enabled": True, "order": 11, "label": "Add New Channels"},
        },
        "official_playlists": []  # list of {"name": "...", "url": "..."}
    }

def save_admin_data(data):
    with open(ADMIN_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# --- Helper functions for bot ---
def get_main_menu_keyboard(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build menu based on admin settings and include official playlists."""
    admin_data = context.bot_data.get('admin_data', load_admin_data())
    if not admin_data.get("bot_on", True):
        return ReplyKeyboardMarkup([["Bot is OFF (Admin)"]], resize_keyboard=True)
    menus = admin_data.get("menus", {})
    sorted_items = sorted(menus.items(), key=lambda x: x[1].get("order", 100))
    keyboard = []
    for key, val in sorted_items:
        if val.get("enabled", True):
            keyboard.append([f"{key}) {val['label']}"])
    
    # অফিসিয়াল প্লেলিস্ট যোগ করো (যদি থাকে)
    playlists = admin_data.get('official_playlists', [])
    if playlists:
        keyboard.append(["📺 Official Playlists"])
        for pl in playlists:
            keyboard.append([f"📺 {pl['name']}"])
    
    # Admin ইউজারের জন্য /admin বাটন
    if context._user_id == ADMIN_ID:
        keyboard.append(["/admin"])
    
    keyboard.append(["/start (Reset)"])
    keyboard.append(["/cancel"])
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, filename: str, content: str):
    with io.BytesIO(content.encode('utf-8')) as f:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f,
            filename=filename
        )

async def send_csv_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, filename: str, data: List[List[str]]):
    output = io.StringIO()
    writer = csv.writer(output)
    for row in data:
        writer.writerow(row)
    output.seek(0)
    with io.BytesIO(output.getvalue().encode('utf-8')) as f:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f,
            filename=filename
        )

async def send_progress_message(update: Update, context: ContextTypes.DEFAULT_TYPE, processed: int, total: int, current_name: str, eta: float):
    percent = (processed / total) * 100 if total > 0 else 0
    filled = int(percent / 5)
    bar = "█" * filled + "░" * (20 - filled)
    eta_str = f"{int(eta//60)}m {int(eta%60)}s" if eta < 3600 else ">1h"
    text = (
        f"<b>Analyzing channels...</b>\n"
        f"<code>[{bar}]</code> {processed}/{total} ({percent:.1f}%)\n"
        f"Current: <i>{current_name}</i>\n"
        f"ETA: {eta_str}"
    )
    msg_id = context.user_data.get('progress_msg_id')
    try:
        if msg_id:
            await context.bot.edit_message_text(
                text=text,
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                parse_mode=ParseMode.HTML
            )
        else:
            msg = await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            context.user_data['progress_msg_id'] = msg.message_id
    except Exception as e:
        logger.warning(f"Progress edit failed: {e}")
        msg = await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        context.user_data['progress_msg_id'] = msg.message_id

# --- Conversation Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data['files'] = {}
    if 'admin_data' not in context.bot_data:
        context.bot_data['admin_data'] = load_admin_data()
    await update.message.reply_text(
        "Welcome to the M3U Smart Toolkit Bot! 🤖\n\n"
        "Please upload one or more `.m3u` or `.m3u8` files, or send a direct URL to a playlist.\n"
        "You can also send /done when you've uploaded all files.",
        reply_markup=ReplyKeyboardRemove()
    )
    return SELECTING_FILES

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not (doc.file_name.lower().endswith(".m3u") or doc.file_name.lower().endswith(".m3u8")):
        await update.message.reply_text("That's not an .m3u or .m3u8 file. Please try again.")
        return SELECTING_FILES
    file = await context.bot.get_file(doc.file_id)
    with io.BytesIO() as b:
        await file.download_to_memory(b)
        b.seek(0)
        content = b.read().decode('utf-8', errors='ignore')
    context.user_data['files'][doc.file_name] = content
    await update.message.reply_text(
        f"✅ Received <b>{doc.file_name}</b>. Total files: {len(context.user_data['files'])}. "
        "Upload more, send a URL, or /done.",
        parse_mode=ParseMode.HTML
    )
    return SELECTING_FILES

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("Please send a valid HTTP/HTTPS URL.")
        return SELECTING_FILES
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    await update.message.reply_text(f"Failed to fetch URL: HTTP {resp.status}")
                    return SELECTING_FILES
                content = await resp.text()
    except Exception as e:
        await update.message.reply_text(f"Error fetching URL: {e}")
        return SELECTING_FILES
    filename = url.split('/')[-1] or "playlist.m3u"
    context.user_data['files'][filename] = content
    await update.message.reply_text(
        f"✅ Received playlist from URL: <b>{filename}</b>. Total files: {len(context.user_data['files'])}. "
        "Upload more or /done.",
        parse_mode=ParseMode.HTML
    )
    return SELECTING_FILES

async def done_uploading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get('files'):
        await update.message.reply_text("You haven't uploaded any files or URLs yet. Please upload at least one.")
        return SELECTING_FILES

    await update.message.reply_text(f"Processing {len(context.user_data['files'])} source(s)...")
    all_entries_per_file = []
    for name, content in context.user_data['files'].items():
        all_entries_per_file.append(parse_m3u_from_content(name, content))
    context.user_data['merged_entries'] = merge_entries(all_entries_per_file)
    context.user_data['status_map'] = None
    total_channels = len(context.user_data['merged_entries'])
    await update.message.reply_text(
        f"All sources loaded and merged!\nTotal unique channels: <b>{total_channels}</b>\n\nWhat would you like to do?",
        reply_markup=get_main_menu_keyboard(context),
        parse_mode=ParseMode.HTML
    )
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    merged_entries = context.user_data.get('merged_entries')
    
    # ---- অফিসিয়াল প্লেলিস্ট ডিটেক্ট করো ----
    if choice.startswith("📺 "):
        pl_name = choice[2:].strip()
        admin_data = context.bot_data.get('admin_data', load_admin_data())
        playlists = admin_data.get('official_playlists', [])
        matched = next((pl for pl in playlists if pl['name'] == pl_name), None)
        if not matched:
            await update.message.reply_text("❌ Playlist not found.", reply_markup=get_main_menu_keyboard(context))
            return MAIN_MENU
        url = matched['url']
        await update.message.reply_text(f"🔄 Loading official playlist: {pl_name}...", reply_markup=ReplyKeyboardRemove())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        await update.message.reply_text(f"❌ Failed to fetch: HTTP {resp.status}")
                        return MAIN_MENU
                    content = await resp.text()
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
            return MAIN_MENU
        filename = url.split('/')[-1] or "official.m3u"
        context.user_data['files'] = {filename: content}
        all_entries_per_file = [parse_m3u_from_content(filename, content)]
        context.user_data['merged_entries'] = merge_entries(all_entries_per_file)
        context.user_data['status_map'] = None
        total_channels = len(context.user_data['merged_entries'])
        await update.message.reply_text(
            f"✅ Loaded official playlist: <b>{pl_name}</b>\nTotal channels: <b>{total_channels}</b>\n\nWhat would you like to do?",
            reply_markup=get_main_menu_keyboard(context),
            parse_mode=ParseMode.HTML
        )
        return MAIN_MENU
    
    # ---- বাকি মেনু হ্যান্ডলিং ----
    if merged_entries is None:
        await update.message.reply_text("Error: No data loaded. Please /start over.")
        return await start(update, context)

    admin_data = context.bot_data.get('admin_data', load_admin_data())
    if not admin_data.get("bot_on", True):
        await update.message.reply_text("Bot is currently OFF (admin). Please try later.")
        return MAIN_MENU

    for key, val in admin_data.get("menus", {}).items():
        if choice.startswith(f"{key})") and val.get("enabled", True):
            if key == "1":
                return await menu_analyze(update, context)
            elif key == "2":
                return await menu_reformat(update, context)
            elif key == "3":
                return await menu_merge(update, context)
            elif key == "4":
                return await menu_remove_categories(update, context)
            elif key == "5":
                await update.message.reply_text("Enter name filter to COMMENT:", reply_markup=ReplyKeyboardRemove())
                return AWAITING_COMMENT_FILTER
            elif key == "6":
                await update.message.reply_text("Enter name filter to UNCOMMENT:", reply_markup=ReplyKeyboardRemove())
                return AWAITING_UNCOMMENT_FILTER
            elif key == "7":
                context.user_data['compare_a_entries'] = None
                await update.message.reply_text("Upload the 'A' file (main/old list):", reply_markup=ReplyKeyboardRemove())
                return AWAITING_COMPARE_A
            elif key == "8":
                return await menu_export_active(update, context)
            elif key == "9":
                return await menu_show_all(update, context)
            elif key == "10":
                return await menu_show_active(update, context)
            elif key == "11":
                return await menu_add_channels(update, context)
    
    await update.message.reply_text("Invalid choice. Please select from the menu.", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Starting analysis... This may take a while.", reply_markup=ReplyKeyboardRemove())
    entries = context.user_data['merged_entries']
    async def progress_cb(processed, total, name, eta):
        await send_progress_message(update, context, processed, total, name, eta)
    status_map, reason_map = await analyze_entries(entries, max_conn=32, progress_callback=progress_cb)
    context.user_data['status_map'] = status_map
    context.user_data['reason_map'] = reason_map
    context.user_data.pop('progress_msg_id', None)
    await update.message.reply_text("✅ Analysis complete.")
    report_txt = render_analyzed_report(entries, status_map)
    await send_file_to_user(update, context, "analyzed_report.m3u", report_txt)
    csv_data = [["Channel Name", "Status", "Reason"]]
    for e in entries:
        nm = e["name"]
        st = status_map.get(nm, "🟡")
        rs = reason_map.get(nm, "")
        csv_data.append([nm, st, rs])
    await send_csv_to_user(update, context, "analyzed_reasons.csv", csv_data)
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_reformat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Reformatting with numbering...", reply_markup=ReplyKeyboardRemove())
    entries = context.user_data['merged_entries']
    text = render_numbered_playlist(entries, only_active=None)
    await send_file_to_user(update, context, "reformatted_numbered.m3u", text)
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Exporting merged & clean playlist...", reply_markup=ReplyKeyboardRemove())
    entries = context.user_data['merged_entries']
    content = render_clean_playlist(entries)
    await send_file_to_user(update, context, "merged_clean.m3u", content)
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_remove_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Dropping group-title...", reply_markup=ReplyKeyboardRemove())
    entries = context.user_data['merged_entries']
    cleaned = drop_group_title(entries)
    content = render_clean_playlist(cleaned)
    await send_file_to_user(update, context, "no_categories.m3u", content)
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def handle_comment_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    filt = update.message.text
    if not filt:
        await update.message.reply_text("Empty filter. Nothing done.", reply_markup=get_main_menu_keyboard(context))
        return MAIN_MENU
    entries = context.user_data['merged_entries']
    commented = toggle_comment(entries, filt, comment=True)
    content = render_clean_playlist(commented)
    await send_file_to_user(update, context, "commented.m3u", content)
    await update.message.reply_text(f"Commented entries matching '<b>{filt}</b>'.", reply_markup=get_main_menu_keyboard(context), parse_mode=ParseMode.HTML)
    return MAIN_MENU

async def handle_uncomment_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    filt = update.message.text
    if not filt:
        await update.message.reply_text("Empty filter. Nothing done.", reply_markup=get_main_menu_keyboard(context))
        return MAIN_MENU
    entries = context.user_data['merged_entries']
    uncommented = toggle_comment(entries, filt, comment=False)
    content = render_clean_playlist(uncommented)
    await send_file_to_user(update, context, "uncommented.m3u", content)
    await update.message.reply_text(f"Un-commented entries matching '<b>{filt}</b>'.", reply_markup=get_main_menu_keyboard(context), parse_mode=ParseMode.HTML)
    return MAIN_MENU

async def handle_compare_a(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or not (doc.file_name.lower().endswith(".m3u") or doc.file_name.lower().endswith(".m3u8")):
        await update.message.reply_text("That's not an .m3u or .m3u8 file. Please upload file 'A'.")
        return AWAITING_COMPARE_A
    file = await context.bot.get_file(doc.file_id)
    with io.BytesIO() as b:
        await file.download_to_memory(b)
        b.seek(0)
        content_a = b.read().decode('utf-8', errors='ignore')
    entries_a = parse_m3u_from_content(doc.file_name, content_a)
    context.user_data['compare_a_entries'] = merge_entries([entries_a])
    await update.message.reply_text("✅ File 'A' received. Now upload the 'B' file.")
    return AWAITING_COMPARE_B

async def handle_compare_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or not (doc.file_name.lower().endswith(".m3u") or doc.file_name.lower().endswith(".m3u8")):
        await update.message.reply_text("That's not an .m3u or .m3u8 file. Please upload file 'B'.")
        return AWAITING_COMPARE_B
    file = await context.bot.get_file(doc.file_id)
    with io.BytesIO() as b:
        await file.download_to_memory(b)
        b.seek(0)
        content_b = b.read().decode('utf-8', errors='ignore')
    entries_b = parse_m3u_from_content(doc.file_name, content_b)
    entries_b_merged = merge_entries([entries_b])
    entries_a = context.user_data.get('compare_a_entries')
    if not entries_a:
        await update.message.reply_text("Error: File 'A' data lost. Start over.", reply_markup=get_main_menu_keyboard(context))
        return MAIN_MENU
    only_in_b = compare_by_name(entries_a, entries_b_merged)
    content = render_clean_playlist(only_in_b)
    await send_file_to_user(update, context, "compare_B_not_in_A.m3u", content)
    await update.message.reply_text(f"Found <b>{len(only_in_b)}</b> channels present in B but not in A.", reply_markup=get_main_menu_keyboard(context), parse_mode=ParseMode.HTML)
    return MAIN_MENU

async def menu_export_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status_map = context.user_data.get('status_map')
    if not status_map:
        await update.message.reply_text("⚠️ You must run 'Analyze' (option 1) first.", reply_markup=get_main_menu_keyboard(context))
        return MAIN_MENU
    await update.message.reply_text("Exporting Active-only numbered playlist...", reply_markup=ReplyKeyboardRemove())
    entries = context.user_data['merged_entries']
    text = render_numbered_playlist(entries, only_active=status_map)
    await send_file_to_user(update, context, "active_only_numbered.m3u", text)
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_show_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    entries = context.user_data['merged_entries']
    tv_radio = [e for e in entries if not any(re.search(r'(#movie|#vod|#film)', ln, re.IGNORECASE) for ln in e["lines"])]
    lines = [f"{i+1}. {e['name']}" for i, e in enumerate(tv_radio)]
    if not lines:
        await update.message.reply_text("No TV/Radio channels found.")
    else:
        content = "All Channels (TV/Radio):\n" + "\n".join(lines)
        with io.BytesIO(content.encode('utf-8')) as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename="all_channels.txt"
            )
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_show_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status_map = context.user_data.get('status_map')
    if not status_map:
        await update.message.reply_text("⚠️ You must run 'Analyze' first.", reply_markup=get_main_menu_keyboard(context))
        return MAIN_MENU
    entries = context.user_data['merged_entries']
    active = [e for e in entries if status_map.get(e["name"]) == "🟢" and not any(re.search(r'(#movie|#vod|#film)', ln, re.IGNORECASE) for ln in e["lines"])]
    lines = [f"{i+1}. {e['name']}" for i, e in enumerate(active)]
    if not lines:
        await update.message.reply_text("No active TV/Radio channels found.")
    else:
        content = "Active Channels:\n" + "\n".join(lines)
        with io.BytesIO(content.encode('utf-8')) as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename="active_channels.txt"
            )
    await update.message.reply_text("What's next?", reply_markup=get_main_menu_keyboard(context))
    return MAIN_MENU

async def menu_add_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "You are about to add new channels to the current playlist.\n"
        "Please upload a new M3U file or provide a URL with the new channels.\n"
        "Send /cancel to abort.",
        reply_markup=ReplyKeyboardRemove()
    )
    return AWAITING_ADD_CHANNEL_FILE

async def handle_add_channel_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.document:
        doc = update.message.document
        if not (doc.file_name.lower().endswith(".m3u") or doc.file_name.lower().endswith(".m3u8")):
            await update.message.reply_text("That's not an .m3u file. Please upload a valid file.")
            return AWAITING_ADD_CHANNEL_FILE
        file = await context.bot.get_file(doc.file_id)
        with io.BytesIO() as b:
            await file.download_to_memory(b)
            b.seek(0)
            content = b.read().decode('utf-8', errors='ignore')
        context.user_data['add_source'] = content
        context.user_data['add_source_name'] = doc.file_name
    elif update.message.text and update.message.text.startswith("http"):
        url = update.message.text.strip()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        await update.message.reply_text(f"Failed to fetch URL: HTTP {resp.status}")
                        return AWAITING_ADD_CHANNEL_FILE
                    content = await resp.text()
            context.user_data['add_source'] = content
            context.user_data['add_source_name'] = url.split('/')[-1] or "new_playlist.m3u"
        except Exception as e:
            await update.message.reply_text(f"Error fetching URL: {e}")
            return AWAITING_ADD_CHANNEL_FILE
    else:
        await update.message.reply_text("Please upload a file or send a valid URL.")
        return AWAITING_ADD_CHANNEL_FILE

    current_count = len(context.user_data['merged_entries'])
    await update.message.reply_text(
        f"Current playlist has {current_count} channels.\n"
        "Where to insert the new channels?\n"
        "Reply with: 'beginning', 'end', or a number (e.g., 201) to insert before that position.\n"
        "(Numbering starts at 1.)"
    )
    return AWAITING_ADD_CHANNEL_POSITION

async def handle_add_channel_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pos_str = update.message.text.strip().lower()
    entries = context.user_data['merged_entries']
    new_content = context.user_data.get('add_source')
    if not new_content:
        await update.message.reply_text("Error: no new data. Please start over.")
        return MAIN_MENU
    new_entries = parse_m3u_from_content(context.user_data['add_source_name'], new_content)
    new_entries_merged = merge_entries([new_entries])
    if pos_str == "beginning":
        insert_idx = 0
    elif pos_str == "end":
        insert_idx = len(entries)
    else:
        try:
            num = int(pos_str)
            if num < 1:
                num = 1
            insert_idx = num - 1
            if insert_idx > len(entries):
                insert_idx = len(entries)
        except ValueError:
            await update.message.reply_text("Invalid position. Use 'beginning', 'end', or a number.")
            return AWAITING_ADD_CHANNEL_POSITION
    new_list = entries[:insert_idx] + new_entries_merged + entries[insert_idx:]
    context.user_data['merged_entries'] = new_list
    numbered = render_numbered_playlist(new_list, only_active=None)
    await send_file_to_user(update, context, "updated_numbered.m3u", numbered)
    await update.message.reply_text(
        f"✅ Added {len(new_entries_merged)} new channels at position {insert_idx+1}.\n"
        f"Total channels now: {len(new_list)}.\n"
        "What's next?",
        reply_markup=get_main_menu_keyboard(context)
    )
    return MAIN_MENU

# --- Admin Panel Handlers ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return MAIN_MENU
    admin_data = context.bot_data.get('admin_data', load_admin_data())
    keyboard = [
        [InlineKeyboardButton("Bot ON/OFF", callback_data="admin_toggle_bot")],
        [InlineKeyboardButton("Manage Official Playlists", callback_data="admin_playlists")],
        [InlineKeyboardButton("Manage Menus", callback_data="admin_menus")],
        [InlineKeyboardButton("Close Admin", callback_data="admin_close")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Admin Panel\nBot status: {'ON' if admin_data.get('bot_on') else 'OFF'}\n"
        f"Official playlists: {len(admin_data.get('official_playlists', []))}",
        reply_markup=reply_markup
    )
    return ADMIN_PANEL

async def admin_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command – works from any state, only for admin."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    admin_data = context.bot_data.get('admin_data', load_admin_data())
    keyboard = [
        [InlineKeyboardButton("Bot ON/OFF", callback_data="admin_toggle_bot")],
        [InlineKeyboardButton("Manage Official Playlists", callback_data="admin_playlists")],
        [InlineKeyboardButton("Manage Menus", callback_data="admin_menus")],
        [InlineKeyboardButton("Close Admin", callback_data="admin_close")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"🔧 Admin Panel\nBot status: {'✅ ON' if admin_data.get('bot_on') else '❌ OFF'}\n"
        f"Official playlists: {len(admin_data.get('official_playlists', []))}",
        reply_markup=reply_markup
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    admin_data = context.bot_data.get('admin_data', load_admin_data())

    if data == "admin_toggle_bot":
        admin_data['bot_on'] = not admin_data.get('bot_on', True)
        save_admin_data(admin_data)
        context.bot_data['admin_data'] = admin_data
        await query.edit_message_text(
            f"Bot is now {'ON' if admin_data['bot_on'] else 'OFF'}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_back")]])
        )
        return ADMIN_PANEL

    elif data == "admin_playlists":
        playlists = admin_data.get('official_playlists', [])
        text = "Official Playlists:\n"
        for i, pl in enumerate(playlists, 1):
            text += f"{i}. {pl['name']} - {pl['url']}\n"
        keyboard = [
            [InlineKeyboardButton("Add Playlist", callback_data="admin_add_playlist")],
            [InlineKeyboardButton("Remove Playlist", callback_data="admin_remove_playlist")],
            [InlineKeyboardButton("Back", callback_data="admin_back")],
        ]
        await query.edit_message_text(text or "No playlists.", reply_markup=InlineKeyboardMarkup(keyboard))
        return ADMIN_PANEL

    elif data == "admin_add_playlist":
        await query.edit_message_text("Send me the name of the new playlist (e.g., 'UK Sports').")
        return ADMIN_ADD_PLAYLIST_NAME

    elif data == "admin_remove_playlist":
        playlists = admin_data.get('official_playlists', [])
        if not playlists:
            await query.edit_message_text("No playlists to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_back")]]))
            return ADMIN_PANEL
        keyboard = []
        for i, pl in enumerate(playlists, 1):
            keyboard.append([InlineKeyboardButton(f"{i}. {pl['name']}", callback_data=f"admin_remove_{i}")])
        keyboard.append([InlineKeyboardButton("Back", callback_data="admin_back")])
        await query.edit_message_text("Select playlist to remove:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ADMIN_PANEL

    elif data.startswith("admin_remove_"):
        idx = int(data.split('_')[2]) - 1
        playlists = admin_data.get('official_playlists', [])
        if 0 <= idx < len(playlists):
            removed = playlists.pop(idx)
            save_admin_data(admin_data)
            context.bot_data['admin_data'] = admin_data
            await query.edit_message_text(f"Removed '{removed['name']}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_back")]]))
        else:
            await query.edit_message_text("Invalid index.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_back")]]))
        return ADMIN_PANEL

    elif data == "admin_menus":
        menus = admin_data.get('menus', {})
        keyboard = []
        for key, val in sorted(menus.items(), key=lambda x: x[1].get('order', 100)):
            status = "ON" if val.get('enabled', True) else "OFF"
            keyboard.append([InlineKeyboardButton(f"{key}) {val['label']} [{status}]", callback_data=f"admin_menu_toggle_{key}")])
        keyboard.append([InlineKeyboardButton("Back", callback_data="admin_back")])
        await query.edit_message_text("Toggle menus (click to toggle ON/OFF):", reply_markup=InlineKeyboardMarkup(keyboard))
        return ADMIN_PANEL

    elif data.startswith("admin_menu_toggle_"):
        key = data.split('_')[-1]
        menus = admin_data.get('menus', {})
        if key in menus:
            menus[key]['enabled'] = not menus[key].get('enabled', True)
            save_admin_data(admin_data)
            context.bot_data['admin_data'] = admin_data
            await query.edit_message_text(f"Menu {key}) toggled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_menus")]]))
        return ADMIN_PANEL

    elif data == "admin_back":
        return await admin_panel(update, context)
    elif data == "admin_close":
        await query.edit_message_text("Admin panel closed.")
        return await start(update, context)

    return ADMIN_PANEL

async def admin_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return ADMIN_PANEL
    admin_data = context.bot_data.get('admin_data', load_admin_data())
    if context.user_data.get('admin_state') == 'adding_name':
        context.user_data['admin_new_playlist_name'] = update.message.text
        context.user_data['admin_state'] = 'adding_url'
        await update.message.reply_text("Now send the playlist URL (M3U link).")
        return ADMIN_ADD_PLAYLIST_URL
    elif context.user_data.get('admin_state') == 'adding_url':
        name = context.user_data.get('admin_new_playlist_name')
        url = update.message.text.strip()
        if name and url.startswith("http"):
            admin_data['official_playlists'].append({"name": name, "url": url})
            save_admin_data(admin_data)
            context.bot_data['admin_data'] = admin_data
            context.user_data.pop('admin_state', None)
            context.user_data.pop('admin_new_playlist_name', None)
            await update.message.reply_text(f"Playlist '{name}' added.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Admin", callback_data="admin_back")]]))
            return ADMIN_PANEL
        else:
            await update.message.reply_text("Invalid URL. Please send a valid http/https link.")
            return ADMIN_ADD_PLAYLIST_URL
    return ADMIN_PANEL

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def playlists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_data = context.bot_data.get('admin_data', load_admin_data())
    playlists = admin_data.get('official_playlists', [])
    if not playlists:
        await update.message.reply_text("No official playlists available.")
        return
    text = "Official Playlists:\n"
    for pl in playlists:
        text += f"• {pl['name']}: {pl['url']}\n"
    await update.message.reply_text(text)

# --- Main ---
def main() -> None:
    TOKEN = "7856875370:AAG4XRZaDVZd0kW0u_u2vdRST_W8nhQREkQ"
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    application = Application.builder().token(TOKEN).build()
    application.bot_data['admin_data'] = load_admin_data()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_FILES: [
                MessageHandler(filters.Document.ALL, handle_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url),
                CommandHandler("done", done_uploading)
            ],
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu)
            ],
            AWAITING_COMMENT_FILTER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_comment_filter)
            ],
            AWAITING_UNCOMMENT_FILTER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_uncomment_filter)
            ],
            AWAITING_COMPARE_A: [
                MessageHandler(filters.Document.ALL, handle_compare_a)
            ],
            AWAITING_COMPARE_B: [
                MessageHandler(filters.Document.ALL, handle_compare_b)
            ],
            AWAITING_ADD_CHANNEL_FILE: [
                MessageHandler(filters.Document.ALL, handle_add_channel_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_channel_file)
            ],
            AWAITING_ADD_CHANNEL_POSITION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_channel_position)
            ],
            ADMIN_PANEL: [
                CallbackQueryHandler(admin_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handle_text)
            ],
            ADMIN_ADD_PLAYLIST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handle_text)
            ],
            ADMIN_ADD_PLAYLIST_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handle_text)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        per_user=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin", admin_command_handler))
    application.add_handler(CommandHandler("playlists", playlists_command))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    print("Bot is running... Press Ctrl-C to stop.")
    application.run_polling()

if __name__ == "__main__":
    main()