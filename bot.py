import os
import re
import sys
import glob
import json
import uuid
import time
import socket
import logging
import asyncio
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from aiohttp import web
from telegram import (
    BotCommand,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters
)
import yt_dlp

FFMPEG_PATH = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = 'ffmpeg'

# Load environment variables from .env if present
env_file = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_file):
    try:
        with open(env_file, 'r', encoding='utf-8') as ef:
            for eline in ef:
                eline = eline.strip()
                if eline and not eline.startswith('#') and '=' in eline:
                    ek, ev = eline.split('=', 1)
                    os.environ.setdefault(ek.strip(), ev.strip().strip('"').strip("'"))
    except Exception:
        pass

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
COFFEE_URL = os.environ.get('COFFEE_URL', '').strip()
POLAR_URL = os.environ.get('POLAR_URL', '').strip()
PAYSTACK_URL = os.environ.get('PAYSTACK_URL', '').strip()
TON_WALLET = os.environ.get('TON_WALLET', 'UQC0oMFoDoiMx5LTBhUOIhOm7ObRx6Sm1xB53f6LZtorkhrO').strip()

COOKIE_FILE_PATH = os.path.join(os.path.dirname(__file__), 'cookies.txt')
COOKIES_ENV = os.environ.get('COOKIES_CONTENT', '').strip()
if COOKIES_ENV:
    try:
        with open(COOKIE_FILE_PATH, 'w', encoding='utf-8') as cf:
            cf.write(COOKIES_ENV)
        logger.info("🍪 Loaded custom cookies from COOKIES_CONTENT env var!")
    except Exception as e:
        logger.warning(f"Could not write COOKIES_CONTENT: {e}")

PROXY_URL = os.environ.get('PROXY_URL') or os.environ.get('HTTP_PROXY') or os.environ.get('HTTPS_PROXY')
if PROXY_URL:
    logger.info(f"🛡️ Routing downloads via proxy: {PROXY_URL[:25]}...")

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
CACHE_FILE = os.path.join(os.path.dirname(__file__), 'media_cache.json')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LOCAL_IP = get_local_ip()
SERVER_PORT = int(os.environ.get('PORT', '8080'))
BASE_URL = os.environ.get('BASE_URL') or f'http://{LOCAL_IP}:{SERVER_PORT}'

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r'(https?://[^\s]+)')

ACTIVE_TASKS = {}
ACTIVE_SUBPROCESSES = {}

class ProgressFileReader:
    def __init__(self, filename, cache_id=None, callback=None):
        self._file = open(filename, 'rb')
        self._total_size = os.path.getsize(filename)
        self._read_so_far = 0
        self._cache_id = cache_id
        self._callback = callback
        self._start_time = time.time()
        self._last_call = 0

    def read(self, size=-1):
        if self._cache_id and self._cache_id in ACTIVE_TASKS:
            task = ACTIVE_TASKS[self._cache_id]
            if task and task.cancelled():
                raise asyncio.CancelledError('Upload cancelled by user')

        data = self._file.read(size)
        if data:
            self._read_so_far += len(data)
            now = time.time()
            if self._callback and (now - self._last_call > 2.0 or self._read_so_far >= self._total_size):
                self._last_call = now
                elapsed = max(now - self._start_time, 0.1)
                speed = self._read_so_far / elapsed / (1024 * 1024)
                self._callback(self._read_so_far, self._total_size, speed)
        return data

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()

    def close(self):
        return self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    try:
        if len(cache) > 100:
            keys = list(cache.keys())[-100:]
            cache = {k: cache[k] for k in keys}
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

MEDIA_CACHE = load_cache()

FILE_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'file_cache.json')

def load_file_cache():
    if os.path.exists(FILE_CACHE_FILE):
        try:
            with open(FILE_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_file_cache(cache):
    try:
        if len(cache) > 500:
            keys = list(cache.keys())[-500:]
            cache = {k: cache[k] for k in keys}
        with open(FILE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

FILE_CACHE = load_file_cache()

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get('MAX_CONCURRENT_DOWNLOADS', '5'))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
COMPRESS_SEMAPHORE = asyncio.Semaphore(2)
QUEUE_WAITING = []

def extract_url(text: str):
    if not text:
        return None
    match = URL_REGEX.search(text)
    return match.group(0) if match else None

def is_tiktok_url(url: str) -> bool:
    u = url.lower()
    return 'tiktok.com' in u or 'douyin.com' in u

def format_progress_bar(percent, length=12):
    percent = max(0.0, min(100.0, percent))
    filled = int(length * (percent / 100))
    return '█' * filled + '░' * (length - filled)

def probe_video_metadata(file_path: str):
    width, height, duration = None, None, 0
    if not FFMPEG_PATH or not os.path.exists(file_path):
        return {'width': width, 'height': height, 'duration': duration}

    try:
        cmd = [FFMPEG_PATH, '-i', file_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors='replace')
        output = res.stderr

        dim_match = re.search(r'Stream.*Video:.*,\s*(\d{2,5})x(\d{2,5})', output)
        if dim_match:
            width = int(dim_match.group(1))
            height = int(dim_match.group(2))

        dur_match = re.search(r'Duration:\s*(\d{2}):(\d{2}):(\d{2})', output)
        if dur_match:
            h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), int(dur_match.group(3))
            duration = h * 3600 + m * 60 + s
    except Exception as e:
        logger.warning(f'Metadata probe failed: {e}')

    return {'width': width, 'height': height, 'duration': duration}

def cleanup_files(cache_id):
    pattern = os.path.join(DOWNLOAD_DIR, f'*{cache_id}*')
    for f in glob.glob(pattern):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass

async def cancel_task_and_cleanup(cache_id, message=None):
    global QUEUE_WAITING
    if cache_id in QUEUE_WAITING:
        QUEUE_WAITING.remove(cache_id)
    task = ACTIVE_TASKS.pop(cache_id, None)
    if task and not task.done():
        task.cancel()

    proc = ACTIVE_SUBPROCESSES.pop(cache_id, None)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass

    cleanup_files(cache_id)
    logger.info(f"❌ [CANCELLED] Operation {cache_id} cancelled by user. Cleaned up files.")

    if message:
        try:
            await message.edit_text("❌ **Operation cancelled.** Cleaned up all temporary files.", parse_mode="Markdown")
        except Exception:
            pass

def get_support_button():
    return InlineKeyboardButton("💖 Help Keep Boltrip Free", callback_data="show_support")

def get_cancel_button(cache_id, label="❌ Cancel"):
    return InlineKeyboardButton(label, callback_data=f"cancel:{cache_id}")

def get_cancel_keyboard(cache_id):
    return InlineKeyboardMarkup([[get_cancel_button(cache_id, "❌ Cancel")]])

def get_post_download_keyboard():
    return InlineKeyboardMarkup([[get_support_button()]])

def get_large_file_keyboard(cache_id, is_audio=False):
    direct_link = f"{BASE_URL}/download/{cache_id}"
    buttons = [
        [InlineKeyboardButton("📥 📱 Download to Phone (Direct)", url=direct_link)]
    ]
    if not is_audio:
        buttons.append([InlineKeyboardButton("🗜️ Auto-Compress & Send Here", callback_data=f"compress:{cache_id}")])
        buttons.append([InlineKeyboardButton("🎵 Send as MP3 Audio", callback_data=f"aud:{cache_id}")])
    buttons.append([get_cancel_button(cache_id, "❌ Cancel")])
    return InlineKeyboardMarkup(buttons)

SUPPORT_POPUP_TEXT = (
    "❤️ <b>Help Us Keep Boltrip 100% Free & Ad-Free!</b>\n\n"
    "Unlike other downloaders, <b>Boltrip has ZERO ads, no subscriptions, and no limits</b>.\n\n"
    "⚡ Every 1080p stream, audio extraction, and large file delivery runs on dedicated high-speed cloud servers that cost real money to maintain every month.\n\n"
    "If Boltrip saved your time, preserved your data, or helped you in groups, <b>even a small donation directly funds server bandwidth</b> so we can keep it 100% free and fast for everyone worldwide!\n\n"
    "🪙 <b>Crypto Support (USDT / TON):</b>\n"
    "• <b>Network:</b> TON (The Open Network)\n"
    "• <b>Accepted Assets:</b> USDT (TON) or TON (Toncoin)\n"
    "• <b>Wallet Address:</b> <i>(Tap address below to copy)</i>\n"
    f"<code>{TON_WALLET}</code>\n\n"
    "<i>Thank you so much for keeping free tools alive! ❤️</i>"
)

def get_support_keyboard():
    ton_url = f"ton://transfer/{TON_WALLET}?text=BoltripSupport"
    buttons = [
        [InlineKeyboardButton("🪙 Pay via Telegram / TON Wallet", url=ton_url)],
        [InlineKeyboardButton("📢 Share Boltrip with Friends", switch_inline_query="Check out @Boltrip_bot for fast, ad-free video downloads! ⚡")]
    ]
    return InlineKeyboardMarkup(buttons)

def download_tiktok_direct(url, cache_id, progress_hook=None):
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        if data.get("code") == 0:
            play_url = data["data"].get("play")
            if play_url:
                out_path = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_tiktok.mp4")
                dl_req = urllib.request.Request(play_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=30) as r, open(out_path, "wb") as f:
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    chunk_size = 64 * 1024
                    start_time = time.time()
                    while True:
                        chunk = r.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_hook:
                            elapsed = max(time.time() - start_time, 0.1)
                            speed = downloaded / elapsed
                            progress_hook({
                                "status": "downloading",
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                                "speed": speed,
                                "eta": int((total - downloaded) / speed) if speed > 0 and total > downloaded else 0
                            })
                return out_path
    except Exception as e:
        logger.warning(f"TikTok direct fetch error: {e}")
    return None

async def handle_direct_download(request):
    file_id = request.match_info.get("file_id")
    matching = glob.glob(os.path.join(DOWNLOAD_DIR, f"*{file_id}*"))
    matching = [m for m in matching if not m.endswith(".part") and os.path.isfile(m)]
    if not matching:
        return web.Response(text="Download expired or not found. Please request from @Boltrip_bot again.", status=404)

    file_path = matching[0]
    filename = os.path.basename(file_path)
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"🌐 [PHONE DOWNLOAD] Client downloaded '{filename}' ({size_mb:.1f} MB)")
    return web.FileResponse(file_path, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "there"
    welcome_text = (
        f"**Welcome to Boltrip, {user_name}.**\n\n"
        "A fast, clean media downloader with zero ads and no subscription walls.\n\n"
        "**Supported Platforms:**\n"
        "• YouTube (Videos & Shorts)\n"
        "• TikTok (No watermark)\n"
        "• Instagram (Reels & Posts)\n"
        "• X / Twitter\n"
        "• Pinterest\n"
        "• Facebook & Reddit\n\n"
        "**How to Use:**\n"
        "1. Paste any media link into this chat.\n"
        "2. Choose your format: 1080p Full HD, 720p, or MP3 Audio.\n"
        "3. Track live download and upload progress.\n\n"
        "**Group Chats:**\n"
        "Add Boltrip to any group and use `/dl <link>`, `/video <link>`, or `/audio <link>`.\n\n"
        "Send a link to begin."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "⚡ **Boltrip Commands & Feature Guide:**\n\n"
        "**Personal Chat:**\n"
        "• Send any link directly ➔ Choose 1080p, 720p, or MP3\n\n"
        "**Group Chats:**\n"
        "• `/dl <link>` ➔ Download any video/audio\n"
        "• `/video <link>` ➔ Fetch 1080p Full HD video\n"
        "• `/audio <link>` ➔ Extract 320kbps MP3 audio\n"
        "• Or tag `@Boltrip_bot <link>`\n\n"
        "**Bot Commands:**\n"
        "• `/start` ➔ Welcome message & feature overview\n"
        "• `/help` ➔ This command guide\n"
        "• `/support` or `/donate` ➔ Help us keep servers free & ad-free\n"
        "• `/cancel` ➔ Abort ongoing download or upload\n\n"
        "**Features:**\n"
        "• ❌ 1-Tap Cancel on every step\n"
        "• 📊 Real-time progress bars & download speed\n"
        "• 📱 Direct phone browser download for large files (>50 MB)\n"
        "• 🗜️ In-chat smart compressor"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = len(ACTIVE_TASKS)
    for cid in list(ACTIVE_TASKS.keys()):
        await cancel_task_and_cleanup(cid)
    for pid in list(ACTIVE_SUBPROCESSES.keys()):
        try:
            ACTIVE_SUBPROCESSES[pid].kill()
        except Exception:
            pass
    ACTIVE_SUBPROCESSES.clear()
    await update.message.reply_text(f"🛑 Cancelled all active operations ({count} stopped).", parse_mode="Markdown")

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        SUPPORT_POPUP_TEXT,
        reply_markup=get_support_keyboard(),
        parse_mode="HTML"
    )

async def dl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("💡 **Please provide a link after the command:**\nExample: `/dl https://youtu.be/...`", parse_mode="Markdown")
        return
    await process_url(update, context, url, force_audio=False, force_video=False)

async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("💡 **Please provide a link after the command:**\nExample: `/video https://youtu.be/...`", parse_mode="Markdown")
        return
    await process_url(update, context, url, force_audio=False, force_video=True)

async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("💡 **Please provide a link after the command:**\nExample: `/audio https://youtu.be/...`", parse_mode="Markdown")
        return
    await process_url(update, context, url, force_audio=True, force_video=False)

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

    url = extract_url(query)
    if not url:
        return

    cache_id = str(abs(hash(url)) % 10000000)
    MEDIA_CACHE[cache_id] = {"url": url, "title": "Shared Media", "author": "Boltrip"}
    save_cache(MEDIA_CACHE)

    keyboard = [
        [
            InlineKeyboardButton("🎬 1080p Full HD", callback_data=f"vid1080:{cache_id}"),
            InlineKeyboardButton("📱 720p Fast", callback_data=f"vid720:{cache_id}"),
        ],
        [
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"aud:{cache_id}"),
            get_cancel_button(cache_id),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    clean_url_display = url[:60] + "..." if len(url) > 60 else url

    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="⚡ Download via Boltrip",
            description=f"Send download buttons for: {clean_url_display}",
            input_message_content=InputTextMessageContent(
                f"🎬 **Download Media with Boltrip**\n🔗 `{clean_url_display}`\n\nChoose format below:",
                parse_mode="Markdown"
            ),
            reply_markup=reply_markup
        )
    ]
    try:
        await update.inline_query.answer(results, cache_time=10)
    except Exception as e:
        logger.warning(f"Inline query error: {e}")

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, force_audio=False, force_video=False):
    global MEDIA_CACHE
    user = update.effective_user
    user_str = f"@{user.username}" if user and user.username else f"User {user.id if user else 'Unknown'}"
    logger.info(f"📥 [REQUEST] {user_str} sent link: {url}")
    cache_id = str(abs(hash(url)) % 10000000)
    cancel_markup = InlineKeyboardMarkup([[get_cancel_button(cache_id, "❌ Cancel")]])
    status_msg = await update.message.reply_text("⚡ **Analyzing link...**", reply_markup=cancel_markup, parse_mode="Markdown")

    loop = asyncio.get_running_loop()

    def fetch_meta():
        if is_tiktok_url(url):
            try:
                api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get("code") == 0:
                        d = data["data"]
                        return {
                            "title": d.get("title", "TikTok Video")[:80],
                            "author": d.get("author", {}).get("nickname", "TikTok Creator"),
                            "is_tiktok": True
                        }
            except Exception:
                pass

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "extractor_args": {"youtube": {"player_client": ["default", "-android_sdkless"]}, "twitter": {"api": ["syndication"]}},
        }
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            opts["cookiefile"] = COOKIE_FILE_PATH
        if PROXY_URL:
            opts["proxy"] = PROXY_URL
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": (info.get("title") or "Media")[:80],
                "author": info.get("uploader", info.get("channel", "Creator")),
                "is_tiktok": False
            }

    try:
        meta = await loop.run_in_executor(None, fetch_meta)
    except Exception as e:
        await status_msg.edit_text(f"❌ **Failed to load link:**\n`{str(e)[:200]}`", parse_mode="Markdown")
        return

    title = meta.get("title", "Media")
    author = meta.get("author", "Creator")
    logger.info(f"🔍 [METADATA] Extracted: '{title[:45]}' | Creator: '{author}'")
    MEDIA_CACHE[cache_id] = {"url": url, "title": title, "author": author}
    save_cache(MEDIA_CACHE)

    if force_audio:
        t = asyncio.create_task(download_and_send(update, context, status_msg, cache_id, quality="audio"))
        ACTIVE_TASKS[cache_id] = t
        return
    elif force_video:
        t = asyncio.create_task(download_and_send(update, context, status_msg, cache_id, quality="1080"))
        ACTIVE_TASKS[cache_id] = t
        return

    keyboard = [
        [
            InlineKeyboardButton("🎬 1080p Full HD", callback_data=f"vid1080:{cache_id}"),
            InlineKeyboardButton("📱 720p Fast", callback_data=f"vid720:{cache_id}"),
        ],
        [
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"aud:{cache_id}"),
            get_cancel_button(cache_id, "❌ Cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        f"🎬 **{title}**\n👤 _{author}_\n\nSelect desired download quality:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_incoming_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    url = extract_url(text)
    if not url:
        return

    force_audio = text.lower().startswith("mp3") or text.lower().startswith("audio")
    force_video = text.lower().startswith("vid") or text.lower().startswith("video")
    await process_url(update, context, url, force_audio=force_audio, force_video=force_video)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "show_support":
        await query.message.reply_text(SUPPORT_POPUP_TEXT, reply_markup=get_support_keyboard(), parse_mode="HTML")
        return

    if ":" not in data:
        return
    action, cache_id = data.split(":", 1)

    if action == "cancel":
        await cancel_task_and_cleanup(cache_id, query.message)
        return

    cached = MEDIA_CACHE.get(cache_id)
    if not cached:
        await query.edit_message_text("⚠️ Session expired or link not found. Please paste the link again.")
        return

    title = cached.get("title", "Media")
    author = cached.get("author", "Creator")

    if action == "compress":
        input_path = cached.get("file_path")
        is_tiktok = cached.get("is_tiktok", False)
        if not input_path or not os.path.exists(input_path):
            await query.edit_message_text("⚠️ Original download file is no longer available. Please paste the link again.")
            return
        t = asyncio.create_task(
            execute_compression_and_upload(
                update, context, query.message, cache_id, input_path, title, author, is_tiktok
            )
        )
        ACTIVE_TASKS[cache_id] = t
        return

    if action in ["vid1080", "vid720", "aud"]:
        quality_map = {
            "vid1080": "1080",
            "vid720": "720",
            "aud": "audio"
        }
        q = quality_map[action]
        t = asyncio.create_task(
            download_and_send(update, context, query.message, cache_id, quality=q)
        )
        ACTIVE_TASKS[cache_id] = t
        return

async def execute_compression_and_upload(update, context, status_msg, cache_id, input_path, title, author, is_tiktok):
    cancel_markup = get_cancel_keyboard(cache_id)
    await status_msg.edit_text(
        f"🗜️ **Compressing Video to fit under 50 MB...**\n`{title[:40]}`\n\n_Re-encoding video with FFmpeg for Telegram delivery..._",
        reply_markup=cancel_markup,
        parse_mode="Markdown"
    )

    compressed_path = os.path.join(DOWNLOAD_DIR, f"compressed_{cache_id}.mp4")

    loop = asyncio.get_running_loop()
    def run_compress():
        meta = probe_video_metadata(input_path)
        dur = meta.get("duration", 0)
        scale_filter = "scale=trunc(min(1280,iw)/2)*2:trunc(min(720,ih)/2)*2"
        if dur > 0:
            target_total_bitrate = int((44 * 1024 * 1024 * 8) / dur)
            video_bitrate = max(150000, target_total_bitrate - 128000)
            cmd = [
                FFMPEG_PATH, "-y", "-i", input_path,
                "-vf", scale_filter,
                "-c:v", "libx264", "-b:v", str(video_bitrate), "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                compressed_path
            ]
        else:
            cmd = [
                FFMPEG_PATH, "-y", "-i", input_path,
                "-vf", scale_filter,
                "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                compressed_path
            ]
        
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ACTIVE_SUBPROCESSES[cache_id] = proc
        stdout, stderr = proc.communicate()
        ACTIVE_SUBPROCESSES.pop(cache_id, None)
        return proc.returncode == 0 and os.path.exists(compressed_path)

    try:
        success = await loop.run_in_executor(None, run_compress)
    except asyncio.CancelledError:
        cleanup_files(cache_id)
        raise
    except Exception as e:
        logger.error(f"Compression error: {e}")
        success = False

    if not success or not os.path.exists(compressed_path):
        await status_msg.edit_text(
            f"⚠️ Compression failed or cancelled.\nYou can still download the high-res file directly to your phone:",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=False)
        )
        return

    comp_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
    logger.info(f"🗜️ [COMPRESS COMPLETE] Compressed {input_path} down to {comp_size_mb:.1f} MB")
    if comp_size_mb > 49.5:
        await status_msg.edit_text(
            f"⚠️ Compressed file is {comp_size_mb:.1f} MB (still exceeds Telegram's 50 MB limit).\nPlease use the direct phone download:",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=False)
        )
        return

    await deliver_file(update, context, status_msg, cache_id, compressed_path, is_audio=False, title=title, author=author, is_tiktok=is_tiktok)

async def download_and_send(update, context, status_msg, cache_id, quality="1080"):
    cached = MEDIA_CACHE.get(cache_id)
    if not cached:
        await status_msg.edit_text("⚠️ Download session expired. Please re-send the link.")
        return

    url = cached["url"]
    title = cached.get("title", "Media")
    author = cached.get("author", "Creator")
    is_audio = (quality == "audio")
    cancel_markup = get_cancel_keyboard(cache_id)
    main_loop = asyncio.get_running_loop()

    # --- 1. INSTANT TELEGRAM FILE_ID CACHE CHECK ---
    cache_key = f"{url}_{quality}"
    cached_file = FILE_CACHE.get(cache_key)
    if cached_file and cached_file.get("file_id"):
        logger.info(f"⚡ Instant delivery from Telegram cache for {url}")
        file_id = cached_file["file_id"]
        c_title = cached_file.get("title", title)
        c_author = cached_file.get("author", author)
        c_size = cached_file.get("size_mb", 0.0)
        c_type = cached_file.get("type", "video")

        import html
        t_clean = html.escape(c_title[:100])
        a_clean = html.escape(c_author[:60])
        caption = (
            f"🎬 <b>{t_clean}</b>\n"
            f"👤 <i>{a_clean}</i>\n"
            f"📦 <code>{c_size:.1f} MB</code>\n\n"
            f"⚡ Instant delivery via @Boltrip_bot"
        )
        chat_id = update.effective_chat.id
        delivery_markup = get_post_download_keyboard()

        try:
            if c_type == "audio":
                sent_msg = await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=file_id,
                    title=c_title[:64],
                    performer=c_author[:64],
                    caption=caption,
                    reply_markup=delivery_markup,
                    parse_mode="HTML"
                )
            else:
                kwargs = {
                    "chat_id": chat_id,
                    "video": file_id,
                    "caption": caption,
                    "reply_markup": delivery_markup,
                    "parse_mode": "HTML",
                    "supports_streaming": True
                }
                w = cached_file.get("width")
                h = cached_file.get("height")
                dur = cached_file.get("duration", 0)
                if w and h:
                    kwargs["width"] = w
                    kwargs["height"] = h
                if dur > 0:
                    kwargs["duration"] = dur
                await context.bot.send_video(**kwargs)

            try:
                await status_msg.delete()
            except Exception:
                pass
            ACTIVE_TASKS.pop(cache_id, None)
            return
        except Exception as e:
            logger.warning(f"Cached file_id delivery failed ({e}), proceeding to fresh download...")

    # --- 2. CONCURRENCY QUEUE SEMAPHORE ---
    in_queue = False
    if DOWNLOAD_SEMAPHORE.locked():
        in_queue = True
        if cache_id not in QUEUE_WAITING:
            QUEUE_WAITING.append(cache_id)
        pos = QUEUE_WAITING.index(cache_id) + 1
        await status_msg.edit_text(
            f"⏳ **In Queue (Position #{pos})**\n`{title[:45]}`\n\n"
            f"_High server traffic: {MAX_CONCURRENT_DOWNLOADS} downloads currently active._\n"
            f"Your download will start automatically once a slot opens.",
            reply_markup=cancel_markup,
            parse_mode="Markdown"
        )

    try:
        async with DOWNLOAD_SEMAPHORE:
            if in_queue and cache_id in QUEUE_WAITING:
                QUEUE_WAITING.remove(cache_id)

            await status_msg.edit_text(
                f"⏳ **Starting download...**\n`{title[:45]}`\n\nQuality: `{quality}`",
                reply_markup=cancel_markup,
                parse_mode="Markdown"
            )
            await _run_download_workflow(update, context, status_msg, cache_id, quality, url, title, author, is_audio, cancel_markup, main_loop)
    except Exception as e:
        logger.error(f"❌ [WORKFLOW ERROR] {e}", exc_info=True)
        if "Cancelled by user" not in str(e):
            try:
                await status_msg.edit_text(f"❌ **Error during download:**\n`{str(e)[:250]}`", parse_mode="Markdown")
            except Exception:
                pass
    finally:
        if cache_id in QUEUE_WAITING:
            QUEUE_WAITING.remove(cache_id)
        ACTIVE_TASKS.pop(cache_id, None)
    return

async def _run_download_workflow(update, context, status_msg, cache_id, quality, url, title, author, is_audio, cancel_markup, main_loop):

    last_edit = [0]
    last_text = [""]

    def progress_hook(d):
        if cache_id not in ACTIVE_TASKS:
            raise Exception("Cancelled by user")
        
        now = time.time()
        if now - last_edit[0] < 1.5:
            return
        
        if d.get("status") == "downloading":
            frag_index = d.get("fragment_index")
            frag_count = d.get("fragment_count")
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            
            if frag_index is not None and frag_count is not None and frag_count > 0:
                percent = (frag_index / frag_count) * 100
                size_str = f"Chunk {frag_index}/{frag_count}"
            else:
                percent = (downloaded / total * 100) if total > 0 else 0
                size_str = f"{downloaded / (1024*1024):.1f}MB / {total / (1024*1024):.1f}MB" if total > 0 else f"{downloaded / (1024*1024):.1f}MB"
            
            bar = format_progress_bar(percent)
            speed_str = f"{speed / (1024*1024):.1f} MB/s" if speed else "..."
            eta_str = f"{eta}s" if eta else "..."

            text = (
                f"📥 **Downloading Media...**\n"
                f"`{title[:40]}`\n\n"
                f"{bar} **{percent:.1f}%**\n"
                f"⚡ `{speed_str}` | ⏱️ `{eta_str}`\n"
                f"📦 `{size_str}`"
            )
            # Log milestones on server console at ~25%, 50%, 75%, 100%
            pct_int = int(percent)
            if pct_int in [25, 50, 75, 100] and pct_int != getattr(progress_hook, 'last_log_pct', -1):
                progress_hook.last_log_pct = pct_int
                logger.info(f"📥 [DOWNLOADING] '{title[:30]}' -> {percent:.1f}% ({speed_str}, ETA {eta_str})")

            if text != last_text[0]:
                last_text[0] = text
                last_edit[0] = now
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(text, reply_markup=cancel_markup, parse_mode="Markdown"),
                        main_loop
                    )
                except Exception as ex:
                    logger.debug(f"Progress edit error: {ex}")

    is_tiktok = is_tiktok_url(url)
    downloaded_path = None

    if is_tiktok and not is_audio:
        try:
            loop = asyncio.get_running_loop()
            downloaded_path = await loop.run_in_executor(
                None, lambda: download_tiktok_direct(url, cache_id, progress_hook)
            )
        except Exception as e:
            logger.warning(f"TikWM direct download failed, falling back to yt-dlp: {e}")
            downloaded_path = None

    if not downloaded_path:
        outtmpl = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_%(id)s.%(ext)s")
        if is_audio:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 10,
                "concurrent_fragment_downloads": 8,
                                "buffersize": 1024 * 128,
                "extractor_args": {"youtube": {"player_client": ["default", "-android_sdkless"]}, "twitter": {"api": ["syndication"]}},
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            if quality == "720":
                fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            else:
                fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            
            ydl_opts = {
                "format": fmt,
                "format_sort": ["res", "ext:mp4:m4a", "+proto:https", "hasaud"],
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 10,
                "concurrent_fragment_downloads": 8,
                                "buffersize": 1024 * 128,
                "merge_output_format": "mp4",
                "extractor_args": {"youtube": {"player_client": ["default", "-android_sdkless"]}, "twitter": {"api": ["syndication"]}},
            }
        if FFMPEG_PATH:
            ydl_opts["ffmpeg_location"] = FFMPEG_PATH
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ydl_opts["cookiefile"] = COOKIE_FILE_PATH
        if PROXY_URL:
            ydl_opts["proxy"] = PROXY_URL

        loop = asyncio.get_running_loop()
        try:
            def run_ydl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    fname = ydl.prepare_filename(info)
                    if is_audio:
                        fname = os.path.splitext(fname)[0] + ".mp3"
                    elif not fname.endswith(".mp4"):
                        base = os.path.splitext(fname)[0]
                        if os.path.exists(base + ".mp4"):
                            fname = base + ".mp4"
                    return fname, info

            fname, info = await loop.run_in_executor(None, run_ydl)
            if os.path.exists(fname):
                downloaded_path = fname
            else:
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_*"))
                matches = [m for m in matches if not m.endswith(".part")]
                if matches:
                    downloaded_path = matches[0]
        except asyncio.CancelledError:
            cleanup_files(cache_id)
            raise
        except Exception as e:
            if "Cancelled by user" in str(e):
                cleanup_files(cache_id)
                return
            logger.error(f"yt-dlp download failed: {e}")
            await status_msg.edit_text(f"❌ **Download failed:**\n`{str(e)[:250]}`", parse_mode="Markdown")
            return

    if not downloaded_path or not os.path.exists(downloaded_path):
        await status_msg.edit_text("❌ Download could not locate completed file.", parse_mode="Markdown")
        return

    cached_item = MEDIA_CACHE.get(cache_id, {})
    cached_item["file_path"] = downloaded_path
    cached_item["is_tiktok"] = is_tiktok
    cached_item["url"] = url
    cached_item["title"] = title
    cached_item["author"] = author
    MEDIA_CACHE[cache_id] = cached_item
    save_cache(MEDIA_CACHE)

    await deliver_file(
        update, context, status_msg, cache_id, downloaded_path,
        is_audio=is_audio, title=title, author=author, is_tiktok=is_tiktok
    )

async def deliver_file(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok):
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if file_size_mb >= 49.5:
        await status_msg.edit_text(
            f"⚠️ **File is {file_size_mb:.1f} MB (Telegram Bot limit is 50 MB)**\n\n"
            f"Choose how you want to receive it:\n"
            f"📱 **Download directly to phone browser** at full quality, OR\n"
            f"🗜️ **Auto-compress video** to ~45 MB and send directly inside this chat.",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=is_audio),
            parse_mode="Markdown"
        )
        return

    await upload_to_telegram(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok)

async def upload_to_telegram(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok):
    cancel_markup = get_cancel_keyboard(cache_id)
    main_loop = asyncio.get_running_loop()
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"📤 [UPLOAD START] Sending '{title[:35]}' ({file_size_mb:.1f} MB) as {'Audio' if is_audio else 'Video'} to chat {update.effective_chat.id}")
    last_edit = [0]
    last_pct = [-1]

    def upload_progress(read_bytes, total_bytes, speed_mb):
        if cache_id not in ACTIVE_TASKS:
            raise Exception("Cancelled by user")
        now = time.time()
        percent = (read_bytes / total_bytes * 100) if total_bytes > 0 else 0
        if now - last_edit[0] < 1.8 and int(percent) == last_pct[0]:
            return
        last_edit[0] = now
        last_pct[0] = int(percent)
        if int(percent) in [25, 50, 75, 100] and int(percent) != getattr(upload_progress, 'last_log_pct', -1):
            upload_progress.last_log_pct = int(percent)
            logger.info(f"📤 [UPLOADING] '{title[:30]}' -> {percent:.1f}% ({speed_mb:.1f} MB/s)")
        bar = format_progress_bar(percent)
        text = (
            f"📤 **Uploading to Telegram...**\n"
            f"`{title[:40]}`\n\n"
            f"{bar} **{percent:.1f}%**\n"
            f"⚡ `{speed_mb:.1f} MB/s` | 📦 `{file_size_mb:.1f} MB`"
        )
        try:
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, reply_markup=cancel_markup, parse_mode="Markdown"),
                main_loop
            )
        except Exception as ex:
            logger.debug(f"Upload progress error: {ex}")

    import html
    t_clean = html.escape(title[:100])
    a_clean = html.escape(author[:60])
    caption = (
        f"🎬 <b>{t_clean}</b>\n"
        f"👤 <i>{a_clean}</i>\n"
        f"📦 <code>{file_size_mb:.1f} MB</code>\n\n"
        f"⚡ Downloaded with @Boltrip_bot"
    )

    delivery_markup = get_post_download_keyboard()

    try:
        reader = ProgressFileReader(file_path, cache_id=cache_id, callback=upload_progress)
        chat_id = update.effective_chat.id

        if is_audio:
            sent_msg = await context.bot.send_audio(
                chat_id=chat_id,
                audio=reader,
                title=title[:64],
                performer=author[:64],
                caption=caption,
                reply_markup=delivery_markup,
                parse_mode="HTML",
                write_timeout=300.0,
                read_timeout=120.0
            )
        else:
            meta = probe_video_metadata(file_path)
            w = meta.get("width")
            h = meta.get("height")
            dur = int(meta.get("duration", 0))

            kwargs = {
                "chat_id": chat_id,
                "video": reader,
                "caption": caption,
                "reply_markup": delivery_markup,
                "parse_mode": "HTML",
                "supports_streaming": True,
                "write_timeout": 300.0,
                "read_timeout": 120.0
            }
            if w and h:
                kwargs["width"] = w
                kwargs["height"] = h
            if dur > 0:
                kwargs["duration"] = dur

            sent_msg = await context.bot.send_video(**kwargs)

        # Save to permanent Telegram file_id cache
        try:
            f_id = None
            if is_audio and 'sent_msg' in locals() and sent_msg.audio:
                f_id = sent_msg.audio.file_id
            elif 'sent_msg' in locals() and sent_msg.video:
                f_id = sent_msg.video.file_id

            if f_id:
                cached_info = MEDIA_CACHE.get(cache_id, {})
                cache_key = f"{cached_info.get('url', '')}_{'audio' if is_audio else ('720' if '720' in file_path else '1080')}"
                meta = probe_video_metadata(file_path) if not is_audio else {}
                FILE_CACHE[cache_key] = {
                    "file_id": f_id,
                    "type": "audio" if is_audio else "video",
                    "title": title,
                    "author": author,
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                    "duration": meta.get("duration", 0),
                    "size_mb": file_size_mb
                }
                save_file_cache(FILE_CACHE)
        except Exception as ce:
            logger.debug(f"Cache save error: {ce}")

        logger.info(f"🎉 [DELIVERED] Successfully sent '{title[:35]}' ({file_size_mb:.1f} MB) to chat {chat_id}")
        # Immediate disk cleanup to maintain 0 MB disk footprint
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"🧹 [CLEANUP] Deleted temporary file from disk: {os.path.basename(file_path)}")
        except Exception:
            pass

        try:
            await status_msg.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        cleanup_files(cache_id)
        raise
    except Exception as e:
        if "Cancelled by user" in str(e):
            cleanup_files(cache_id)
            return
        logger.error(f"Telegram upload error: {e}")
        await status_msg.edit_text(
            f"⚠️ Direct upload failed: `{str(e)[:150]}`\n\nYou can still download the file to your phone:",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=is_audio)
        )
        return
    finally:
        ACTIVE_TASKS.pop(cache_id, None)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/download/{file_id}', handle_direct_download)
    app.router.add_get('/health', lambda r: web.Response(text="OK"))
    app.router.add_get('/', lambda r: web.Response(text="Boltrip Downloader Bot is Running."))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', SERVER_PORT)
    await site.start()
    logger.info(f"🌐 HTTP Download Server running on http://0.0.0.0:{SERVER_PORT}")

async def post_init(application):
    await start_web_server()
    try:
        commands = [
            BotCommand("start", "Welcome & feature overview"),
            BotCommand("help", "Command guide & group usage"),
            BotCommand("support", "Help keep Boltrip 100% free"),
            BotCommand("donate", "Support server & bandwidth costs"),
            BotCommand("cancel", "Abort current download/upload"),
            BotCommand("dl", "Download media from link"),
            BotCommand("video", "Direct 1080p video download"),
            BotCommand("audio", "Direct MP3 audio extraction"),
        ]
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Could not register commands menu: {e}")
    logger.info(f"Bot initialized! Public/LAN Base URL: {BASE_URL}")

def main():
    logger.info("Starting Boltrip Bot...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .write_timeout(300.0)
        .read_timeout(120.0)
        .connect_timeout(60.0)
        .pool_timeout(60.0)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("donate", support_command))
    application.add_handler(CommandHandler("coffee", support_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("dl", dl_command))
    application.add_handler(CommandHandler("download", dl_command))
    application.add_handler(CommandHandler("video", video_command))
    application.add_handler(CommandHandler("audio", audio_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_link))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(InlineQueryHandler(handle_inline_query))
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
