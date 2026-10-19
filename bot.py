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
import html
try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None
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
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    CHROME_IMPERSONATE = ImpersonateTarget.from_str('chrome')
except Exception:
    CHROME_IMPERSONATE = None
try:
    import yt_dlp.extractor.linkedin as yt_linkedin
    yt_linkedin.LinkedInIE._VALID_URL = [
        r'https?://(?:www\.)?linkedin\.com/posts/[^/?#]+-(?P<id>\d+)(?:-\w+)?/?(?:[?#]|$)',
        r'https?://(?:www\.)?linkedin\.com/feed/update/urn:li:(?:activity|ugcPost|share):(?P<id>\d+)',
        r'https?://(?:www\.)?linkedin\.com/video/live/urn:li:(?:activity|ugcPost|share):(?P<id>\d+)',
    ]
except Exception:
    pass

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

def get_yt_cookie_file():
    if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
        return COOKIE_FILE_PATH
    return None

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
BASE_URL = os.environ.get('BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or f'http://{LOCAL_IP}:{SERVER_PORT}'
if BASE_URL.startswith("http://") and ".onrender.com" in BASE_URL:
    BASE_URL = BASE_URL.replace("http://", "https://", 1)

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r'(https?://[^\s]+)')

ACTIVE_TASKS = {}
ACTIVE_SUBPROCESSES = {}
WAITING_FOR_CLIP = {}
GLOBAL_BOT = None

def parse_time_to_seconds(time_str: str) -> float | None:
    time_str = time_str.strip().lower()
    m_sec = re.fullmatch(r'(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?', time_str)
    if m_sec:
        return float(m_sec.group(1))
    if re.fullmatch(r'\d+(?:\.\d+)?', time_str):
        return float(time_str)
    parts = time_str.split(':')
    if len(parts) == 2:
        try:
            return float(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return None
    elif len(parts) == 3:
        try:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    return None

def parse_clip_range(text: str) -> tuple[float, float] | None:
    cleaned = text.replace('–', '-').replace('—', '-')
    patterns = [r'\s*-\s*', r'\s+to\s+', r'\s+until\s+', r'\s*,\s*', r'\s+']
    for pat in patterns:
        splits = re.split(pat, cleaned.strip(), maxsplit=1)
        if len(splits) == 2:
            s_val = parse_time_to_seconds(splits[0])
            e_val = parse_time_to_seconds(splits[1])
            if s_val is not None and e_val is not None and e_val > s_val:
                return (s_val, e_val)
    return None

def format_seconds_to_time(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

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
    if not match:
        return None
    url = match.group(0)
    # Unwrap Facebook login/?next= redirects to get original post URL
    if "facebook.com/login/" in url and "next=" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("next", [None])[0]
            if target:
                url = urllib.parse.unquote(target)
        except Exception:
            pass
    # Unwrap LinkedIn authwall redirects to get original post URL
    if "linkedin.com/authwall" in url and "sessionRedirect=" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("sessionRedirect", [None])[0]
            if target:
                url = urllib.parse.unquote(target)
        except Exception:
            pass
    return url

def resolve_url(url: str) -> str:
    """Follow HTTP redirects to get the real destination URL."""
    # Let our specialized extractors handle Facebook natively — do not resolve to login pages
    if "facebook.com" in url or "fb.com" in url or "fb.watch" in url:
        return url
    # Full pin, post, and thread links are already direct — skip redirect resolution
    if any(k in url for k in ["pinterest.com/pin/", "linkedin.com/posts/", "linkedin.com/feed/", "linkedin.com/video/", "threads.net/", "threads.com/"]):
        return url
    try:
        # For short-links like pin.it, lnkd.in, use curl_cffi with Chrome TLS impersonation
        if curl_requests and any(s in url for s in ["pin.it", "lnkd.in"]):
            r = curl_requests.get(url, impersonate="chrome", allow_redirects=True, timeout=8)
            if r.url and r.url != url:
                target_url = r.url
                if "linkedin.com/authwall" in target_url and "sessionRedirect=" in target_url:
                    parsed = urllib.parse.urlparse(target_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    unwrapped = qs.get("sessionRedirect", [None])[0]
                    if unwrapped:
                        return urllib.parse.unquote(unwrapped)
                return target_url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(req, timeout=8) as resp:
            final = resp.geturl()
            if final and final != url:
                if "linkedin.com/authwall" in final and "sessionRedirect=" in final:
                    parsed = urllib.parse.urlparse(final)
                    qs = urllib.parse.parse_qs(parsed.query)
                    unwrapped = qs.get("sessionRedirect", [None])[0]
                    if unwrapped:
                        return urllib.parse.unquote(unwrapped)
                url = final
    except Exception:
        pass
    return url

def is_tiktok_url(url: str) -> bool:
    u = url.lower()
    return 'tiktok.com' in u or 'douyin.com' in u

def is_facebook_url(url: str) -> bool:
    u = url.lower()
    return 'facebook.com' in u or 'fb.watch' in u or 'fb.com' in u

def is_pinterest_url(url: str) -> bool:
    u = url.lower()
    return 'pinterest.com' in u or 'pin.it' in u

def is_linkedin_url(url: str) -> bool:
    u = url.lower()
    return 'linkedin.com' in u or 'lnkd.in' in u

def is_threads_url(url: str) -> bool:
    u = url.lower()
    return 'threads.net' in u or 'threads.com' in u

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

def get_platform_info(url: str) -> str:
    u = url.lower()
    if 'tiktok.com' in u or 'douyin.com' in u:
        return 'tiktok'
    elif 'twitter.com' in u or 'x.com' in u:
        return 'twitter'
    elif 'instagram.com' in u:
        return 'instagram'
    elif 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    elif 'facebook.com' in u or 'fb.watch' in u:
        return 'facebook'
    elif 'reddit.com' in u or 'redd.it' in u:
        return 'reddit'
    elif 'pinterest.com' in u or 'pin.it' in u:
        return 'pinterest'
    elif 'linkedin.com' in u or 'lnkd.in' in u:
        return 'linkedin'
    elif 'threads.net' in u or 'threads.com' in u:
        return 'threads'
    return 'general'

def get_quality_keyboard(cache_id: str, platform: str) -> InlineKeyboardMarkup:
    cached = MEDIA_CACHE.get(cache_id, {})
    has_video = cached.get("has_video", True)

    if platform == 'tiktok':
        buttons = [
            [InlineKeyboardButton("🎬 Download HD (No Watermark)", callback_data=f"vidbest:{cache_id}")],
            [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"aud:{cache_id}")],
            [get_cancel_button(cache_id, "❌ Cancel")]
        ]
    elif platform == 'twitter':
        buttons = [
            [InlineKeyboardButton("🎬 Download Video (Original)", callback_data=f"vidbest:{cache_id}")],
            [
                InlineKeyboardButton("✂️ Clip / Trim Video", callback_data=f"clip:{cache_id}"),
                InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"aud:{cache_id}"),
            ],
            [get_cancel_button(cache_id, "❌ Cancel")]
        ]
    elif platform == 'threads':
        if not has_video:
            buttons = [
                [InlineKeyboardButton("🖼️ Download Threads Photo (HD)", callback_data=f"vidbest:{cache_id}")],
                [get_cancel_button(cache_id, "❌ Cancel")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("🎬 Download Threads Post (HD)", callback_data=f"vidbest:{cache_id}")],
                [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"aud:{cache_id}")],
                [get_cancel_button(cache_id, "❌ Cancel")]
            ]
    elif platform == 'pinterest':
        if not has_video:
            buttons = [
                [InlineKeyboardButton("🖼️ Download Pin Image (HD)", callback_data=f"vidbest:{cache_id}")],
                [get_cancel_button(cache_id, "❌ Cancel")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("🎬 Download Pin (HD)", callback_data=f"vidbest:{cache_id}")],
                [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"aud:{cache_id}")],
                [get_cancel_button(cache_id, "❌ Cancel")]
            ]
    elif platform in ['instagram', 'linkedin']:
        label = "Reel / Post" if platform == 'instagram' else "Post"
        buttons = [
            [InlineKeyboardButton(f"🎬 Download {label} (HD)", callback_data=f"vidbest:{cache_id}")],
            [InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"aud:{cache_id}")],
            [get_cancel_button(cache_id, "❌ Cancel")]
        ]
    elif platform == 'youtube':
        buttons = [
            [
                InlineKeyboardButton("🌟 4K / 2K Ultra", callback_data=f"vid4k:{cache_id}"),
                InlineKeyboardButton("🎬 1080p Full HD", callback_data=f"vid1080:{cache_id}"),
            ],
            [
                InlineKeyboardButton("📱 720p Fast", callback_data=f"vid720:{cache_id}"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"aud:{cache_id}"),
            ],
            [InlineKeyboardButton("✂️ Clip / Trim Video", callback_data=f"clip:{cache_id}")],
            [get_cancel_button(cache_id, "❌ Cancel")]
        ]
    else:
        buttons = [
            [
                InlineKeyboardButton("🎬 Best Quality", callback_data=f"vidbest:{cache_id}"),
                InlineKeyboardButton("📱 720p Fast", callback_data=f"vid720:{cache_id}"),
            ],
            [
                InlineKeyboardButton("✂️ Clip / Trim Video", callback_data=f"clip:{cache_id}"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"aud:{cache_id}"),
            ],
            [get_cancel_button(cache_id, "❌ Cancel")]
        ]
    return InlineKeyboardMarkup(buttons)

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
    "👨‍💻 <b>Developer Contact:</b> @silas_inegbe\n\n"
    "<i>Thank you so much for keeping free tools alive! ❤️</i>"
)

def get_support_keyboard():
    ton_url = f"ton://transfer/{TON_WALLET}?text=BoltripSupport"
    buttons = [
        [InlineKeyboardButton("🪙 Pay via Telegram / TON Wallet", url=ton_url)],
        [InlineKeyboardButton("👨‍💻 Contact Developer (@silas_inegbe)", url="https://t.me/silas_inegbe")],
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

def fetch_facebook_direct(url):
    if not curl_requests:
        return None
    try:
        r = curl_requests.get(url, impersonate="chrome", allow_redirects=True, timeout=12)
        if r.status_code != 200:
            return None
        html_text = r.text

        title = None
        og_t = re.search(r'property="og:title"\s+content="([^"]+)"', html_text) or \
               re.search(r'name="description"\s+content="([^"]+)"', html_text)
        if og_t:
            title = html.unescape(og_t.group(1))
            if " | " in title:
                title = title.split(" | ", 1)[-1]
            title = re.sub(r'[\r\n]+', ' ', title).strip()[:80]
        if not title:
            title = "Facebook Video"

        author = "Facebook Creator"
        author_m = re.search(r'ownerName:"([^"]+)"', html_text) or \
                   re.search(r'"owner":\{"name":"([^"]+)"\}', html_text)
        if author_m:
            author = author_m.group(1)

        hd_matches = re.findall(r'"browser_native_hd_url":"(https:[^"]+)"', html_text) or \
                     re.findall(r'"playable_url_quality_hd":"(https:[^"]+)"', html_text) or \
                     re.findall(r'hd_src:"(https:[^"]+)"', html_text)

        sd_matches = re.findall(r'"browser_native_sd_url":"(https:[^"]+)"', html_text) or \
                     re.findall(r'"playable_url":"(https:[^"]+)"', html_text) or \
                     re.findall(r'sd_src:"(https:[^"]+)"', html_text)

        hd_url = None
        if hd_matches:
            hd_raw = hd_matches[0].encode('utf-8').decode('unicode_escape')
            hd_url = hd_raw.replace(r'\/', '/').replace(r'\u0026', '&')

        sd_url = None
        if sd_matches:
            sd_raw = sd_matches[0].encode('utf-8').decode('unicode_escape')
            sd_url = sd_raw.replace(r'\/', '/').replace(r'\u0026', '&')

        if not hd_url and not sd_url:
            return None

        return {
            "title": title,
            "author": author,
            "hd_url": hd_url,
            "sd_url": sd_url,
        }
    except Exception as e:
        logger.warning(f"Facebook direct meta extraction failed: {e}")
        return None

def download_facebook_direct(url, cache_id, progress_hook=None, quality="best"):
    if not curl_requests:
        return None
    meta = fetch_facebook_direct(url)
    if not meta:
        return None
    stream_url = meta["hd_url"] if (quality != "720" and meta["hd_url"]) else (meta["sd_url"] or meta["hd_url"])
    if not stream_url:
        return None
    try:
        out_path = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_fb.mp4")
        r = curl_requests.get(stream_url, impersonate="chrome", stream=True, timeout=30)
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        start_time = time.time()
        chunk_size = 128 * 1024
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
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
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        logger.warning(f"Facebook direct download error: {e}")
    return None

def get_pinterest_cookies():
    if not os.path.exists(COOKIE_FILE_PATH):
        return {}
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE_PATH)
        jar.load(ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in jar if "pinterest" in c.domain}
    except Exception:
        return {}

def fetch_pinterest_direct(url: str):
    if not curl_requests:
        return None
    try:
        cookies = get_pinterest_cookies()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = curl_requests.get(url, cookies=cookies, headers=headers, impersonate="chrome", allow_redirects=True, timeout=12)
        html = r.text
        real_url = r.url

        title = "Pinterest Media"
        author = "Pinterest Creator"

        titles = re.findall(r'<title>(.*?)</title>', html)
        if titles:
            clean_t = titles[0].replace(" | Pinterest", "").replace(" - Pinterest", "").strip()
            if clean_t:
                title = clean_t

        video_url = None
        image_url = None

        m = re.search(r'<script id="__PWS_INITIAL_PROPS__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                props = json.loads(m.group(1))

                def find_pin(o):
                    if isinstance(o, dict):
                        if ("videos" in o and o["videos"]) or ("images" in o and o["images"]):
                            return o
                        for v in o.values():
                            res = find_pin(v)
                            if res:
                                return res
                    elif isinstance(o, list):
                        for it in o:
                            res = find_pin(it)
                            if res:
                                return res
                    return None

                pin_dict = find_pin(props)
                if pin_dict:
                    if pin_dict.get("title"):
                        title = pin_dict["title"]
                    elif pin_dict.get("grid_title"):
                        title = pin_dict["grid_title"]

                    pinner = pin_dict.get("pinner", {})
                    if pinner.get("username"):
                        author = f"@{pinner['username']}"
                    elif pin_dict.get("closeup_attribution", {}).get("full_name"):
                        author = pin_dict["closeup_attribution"]["full_name"]

                    videos = pin_dict.get("videos") or {}
                    video_list = videos.get("video_list") or {}

                    for key in ["V_720P", "V_EXP720P", "V_1080P", "V_EXP1080P"]:
                        if key in video_list and video_list[key].get("url") and ".mp4" in video_list[key]["url"]:
                            video_url = video_list[key]["url"]
                            break

                    if not video_url:
                        for vinfo in video_list.values():
                            if isinstance(vinfo, dict) and vinfo.get("url") and ".mp4" in vinfo["url"]:
                                video_url = vinfo["url"]
                                break

                    images = pin_dict.get("images") or {}
                    if isinstance(images, dict):
                        for img_key in ["orig", "736x", "564x"]:
                            if img_key in images and images[img_key].get("url"):
                                image_url = images[img_key]["url"]
                                break
            except Exception as e:
                logger.debug(f"Error parsing Pinterest props: {e}")

        if not video_url:
            mp4_matches = re.findall(r'https?://v1\.pinimg\.com/videos/[^\s"\'<>]+\.mp4[^\s"\'<>]*', html)
            if mp4_matches:
                for m_url in mp4_matches:
                    if "720w" in m_url or "expmp4" in m_url.lower():
                        video_url = m_url
                        break
                if not video_url:
                    video_url = mp4_matches[0]

        if not video_url and not image_url:
            img_matches = re.findall(r'https?://i\.pinimg\.com/originals/[^\s"\'<>]+\.(?:jpg|png|webp)', html)
            if img_matches:
                image_url = img_matches[0]

        if video_url:
            return {
                "title": title[:80],
                "author": author[:60],
                "is_tiktok": False,
                "is_pinterest": True,
                "has_video": True,
                "media_url": video_url,
                "ext": "mp4",
                "resolved_url": real_url,
            }
        elif image_url:
            return {
                "title": title[:80],
                "author": author[:60],
                "is_tiktok": False,
                "is_pinterest": True,
                "has_video": False,
                "media_url": image_url,
                "ext": "jpg",
                "resolved_url": real_url,
            }
    except Exception as e:
        logger.warning(f"Pinterest direct meta extraction failed: {e}")
    return None

def download_pinterest_direct(url, cache_id, progress_hook=None, direct_media_url=None, ext="mp4"):
    media_url = direct_media_url
    if not media_url:
        meta = fetch_pinterest_direct(url)
        if not meta or not meta.get("media_url"):
            return None
        media_url = meta["media_url"]
        ext = meta.get("ext", "mp4")

    out_path = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_pin.{ext}")
    try:
        req = urllib.request.Request(media_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(out_path, "wb") as f:
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
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        logger.warning(f"Pinterest direct download error: {e}")
    return None

def extract_threads_shortcode(url: str):
    m = re.search(r'/post/([a-zA-Z0-9_-]+)', url)
    if not m:
        m = re.search(r'/t/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None

def fetch_threads_direct(url: str):
    try:
        code = extract_threads_shortcode(url)
        fetch_url = f"https://www.threads.com/t/{code}" if code else url.replace("threads.net", "threads.com")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if curl_requests:
            r = curl_requests.get(fetch_url, headers=headers, impersonate="chrome", timeout=12)
            html = r.text if r.status_code == 200 else ""
        else:
            req = urllib.request.Request(fetch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

        if not html:
            return None

        scripts = re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html)
        target_post = None
        for s in scripts:
            if "thread_items" in s or (code and code in s):
                try:
                    data = json.loads(s)
                    def find_post(obj):
                        if isinstance(obj, dict):
                            if code and obj.get("code") == code:
                                return obj
                            if "thread_items" in obj and obj["thread_items"]:
                                for ti in obj["thread_items"]:
                                    p = ti.get("post")
                                    if p and isinstance(p, dict):
                                        if not code or p.get("code") == code:
                                            return p
                            for v in obj.values():
                                res = find_post(v)
                                if res:
                                    return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_post(item)
                                if res:
                                    return res
                        return None

                    found = find_post(data)
                    if found:
                        target_post = found
                        break
                except Exception:
                    pass

        if not target_post:
            return None

        user = target_post.get("user") or {}
        author = user.get("username") or user.get("full_name") or "Threads User"
        caption_dict = target_post.get("caption") or {}
        caption_text = caption_dict.get("text", "") if isinstance(caption_dict, dict) else ""
        title = caption_text.strip() if caption_text.strip() else f"Threads post by @{author}"

        video_versions = target_post.get("video_versions") or []
        image_candidates = (target_post.get("image_versions2") or {}).get("candidates") or []
        carousel_media = target_post.get("carousel_media") or []

        # If direct media not found, check quote post or repost
        share_info = (target_post.get("text_post_app_info") or {}).get("share_info") or {}
        quoted_post = share_info.get("quoted_post") or share_info.get("quoted_attachment_post") or share_info.get("reposted_post")
        if quoted_post and isinstance(quoted_post, dict):
            if not video_versions and quoted_post.get("video_versions"):
                video_versions = quoted_post.get("video_versions")
            if not carousel_media and quoted_post.get("carousel_media"):
                carousel_media = quoted_post.get("carousel_media")
            if not image_candidates and (quoted_post.get("image_versions2") or {}).get("candidates"):
                image_candidates = quoted_post["image_versions2"]["candidates"]

        has_video = False
        media_url = None
        ext = "mp4"

        if video_versions:
            has_video = True
            media_url = video_versions[0]["url"]
            ext = "mp4"
        elif carousel_media:
            first_item = carousel_media[0]
            c_vv = first_item.get("video_versions") or []
            if c_vv:
                has_video = True
                media_url = c_vv[0]["url"]
                ext = "mp4"
            else:
                c_iv = (first_item.get("image_versions2") or {}).get("candidates") or []
                if c_iv:
                    has_video = False
                    media_url = c_iv[0]["url"]
                    ext = "jpg"
        elif image_candidates:
            has_video = False
            media_url = image_candidates[0]["url"]
            ext = "jpg"

        # Check for external embedded link preview (e.g. Instagram Reel, YouTube, etc.)
        if not media_url:
            link_preview = (target_post.get("text_post_app_info") or {}).get("link_preview_attachment") or {}
            preview_url = link_preview.get("url")
            if preview_url:
                return {
                    "title": (link_preview.get("title") or title)[:100],
                    "author": author[:60],
                    "is_tiktok": False,
                    "is_threads": False,
                    "has_video": True,
                    "resolved_url": preview_url,
                    "is_external_link": True,
                }
            preview_img = link_preview.get("image_url")
            if preview_img:
                return {
                    "title": (link_preview.get("title") or title)[:100],
                    "author": author[:60],
                    "is_tiktok": False,
                    "is_threads": True,
                    "has_video": False,
                    "media_url": preview_img,
                    "ext": "jpg",
                    "resolved_url": url,
                }

        if not media_url:
            return None

        return {
            "title": title[:100],
            "author": author[:60],
            "is_tiktok": False,
            "is_threads": True,
            "has_video": has_video,
            "media_url": media_url,
            "ext": ext,
            "resolved_url": url,
        }
    except Exception as e:
        logger.warning(f"Threads direct meta extraction error: {e}")
        return None

def download_threads_direct(url, cache_id, progress_hook=None, direct_media_url=None, ext="mp4"):
    media_url = direct_media_url
    if not media_url:
        meta = fetch_threads_direct(url)
        if not meta or not meta.get("media_url"):
            return None
        media_url = meta["media_url"]
        ext = meta.get("ext", "mp4")

    out_path = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_threads.{ext}")
    try:
        if curl_requests:
            resp = curl_requests.get(media_url, impersonate="chrome", stream=True, timeout=30)
            if resp.status_code != 200:
                return None
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            start_time = time.time()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
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
        else:
            req = urllib.request.Request(media_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r, open(out_path, "wb") as f:
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
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        logger.warning(f"Threads direct download error: {e}")
    return None



def render_expired_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>Link Expired — Boltrip</title>
  <style>
    :root {
      --bg: #f2f2f7;
      --card-bg: #ffffff;
      --card-border: rgba(0, 0, 0, 0.08);
      --label: #000000;
      --secondary-label: #8e8e93;
      --accent: #007aff;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #000000;
        --card-bg: #1c1c1e;
        --card-border: rgba(255, 255, 255, 0.1);
        --label: #ffffff;
        --secondary-label: #8e8e93;
        --accent: #0a84ff;
      }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--label);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      text-align: center;
    }
    .card {
      background: var(--card-bg);
      border-radius: 20px;
      border: 1px solid var(--card-border);
      padding: 32px 24px;
      max-width: 420px;
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 16px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.05);
    }
    .icon { font-size: 48px; }
    h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
    p { font-size: 15px; color: var(--secondary-label); line-height: 1.4; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      background: var(--accent);
      color: #fff;
      font-size: 16px;
      font-weight: 600;
      text-decoration: none;
      padding: 14px 24px;
      border-radius: 12px;
      width: 100%;
      min-height: 48px;
      margin-top: 8px;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⏳</div>
    <h1>Download Link Expired</h1>
    <p>This temporary download link is no longer available on the server. Please generate a fresh download from @Boltrip_bot.</p>
    <a href="https://t.me/Boltrip_bot" class="btn">🤖 Open @Boltrip_bot</a>
  </div>
</body>
</html>
"""

def render_download_page(file_id, filename, file_size_mb, is_video=True, is_audio=False, title="", author="", width=None, height=None, duration=0):
    raw_url = f"/download/{file_id}?raw=1"
    stream_url = f"/download/{file_id}?stream=1"
    display_title = title if title else filename
    import html as html_mod
    clean_title = html_mod.escape(display_title)
    clean_author = html_mod.escape(author) if author else ""
    safe_download_name = html_mod.escape(filename)

    res_badge = ""
    if height:
        if height >= 2160:
            res_badge = "4K Ultra HD"
        elif height >= 1440:
            res_badge = "2K Quad HD"
        elif height >= 1080:
            res_badge = "1080p Full HD"
        elif height >= 720:
            res_badge = "720p HD"
        else:
            res_badge = f"{height}p"
    elif is_video:
        res_badge = "HD Video"
    elif is_audio:
        res_badge = "MP3 Audio"

    dur_str = ""
    if duration > 0:
        mins = int(duration) // 60
        secs = int(duration) % 60
        dur_str = f"{mins}:{secs:02d}"

    media_player_html = ""
    if is_video:
        media_player_html = f'<video controls playsinline webkit-playsinline preload="metadata" onplay="notifyDownloaded()"><source src="{stream_url}" type="video/mp4">Your browser does not support HTML5 video.</video>'
    elif is_audio:
        media_player_html = f'<audio controls preload="metadata" onplay="notifyDownloaded()"><source src="{stream_url}" type="audio/mpeg">Your browser does not support audio.</audio>'

    author_html = f'<div class="author">By {clean_author}</div>' if clean_author else ''
    badge_html = f'<span class="badge">{res_badge}</span>' if res_badge else ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>{clean_title} — Boltrip</title>
  <style>
    :root {{
      --bg: #f2f2f7;
      --card-bg: #ffffff;
      --card-border: rgba(0, 0, 0, 0.08);
      --card-shadow: 0 4px 20px rgba(0, 0, 0, 0.04);
      --label: #000000;
      --secondary-label: #8e8e93;
      --separator: rgba(60, 60, 67, 0.12);
      --accent: #007aff;
      --accent-hover: #0062cc;
      --accent-tint: rgba(0, 122, 255, 0.1);
      --tip-bg: rgba(0, 122, 255, 0.06);
      --tip-border: rgba(0, 122, 255, 0.2);
      --tip-text: #0056b3;
      --badge-bg: rgba(0, 0, 0, 0.06);
      --badge-text: #3c3c43;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #000000;
        --card-bg: #1c1c1e;
        --card-border: rgba(255, 255, 255, 0.1);
        --card-shadow: 0 4px 24px rgba(0, 0, 0, 0.5);
        --label: #ffffff;
        --secondary-label: #8e8e93;
        --separator: rgba(84, 84, 88, 0.36);
        --accent: #0a84ff;
        --accent-hover: #409cff;
        --accent-tint: rgba(10, 132, 255, 0.18);
        --tip-bg: rgba(10, 132, 255, 0.12);
        --tip-border: rgba(10, 132, 255, 0.28);
        --tip-text: #82b9f9;
        --badge-bg: rgba(255, 255, 255, 0.12);
        --badge-text: #ebebf5;
      }}
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--label);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: max(20px, env(safe-area-inset-top)) 16px max(32px, env(safe-area-inset-bottom)) 16px;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{
      width: 100%;
      max-width: 520px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 4px 4px 8px 4px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .brand-bolt {{
      font-size: 20px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.02em;
      background: var(--badge-bg);
      color: var(--badge-text);
      text-transform: uppercase;
    }}
    .media-card {{
      background: var(--card-bg);
      border-radius: 20px;
      border: 1px solid var(--card-border);
      box-shadow: var(--card-shadow);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .media-player-wrapper {{
      width: 100%;
      background: #000000;
      position: relative;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 220px;
    }}
    video {{
      width: 100%;
      max-height: 65vh;
      display: block;
      background: #000000;
    }}
    audio {{
      width: 100%;
      padding: 24px 16px;
    }}
    .meta-content {{
      padding: 18px 20px 20px 20px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .title {{
      font-size: 19px;
      font-weight: 700;
      line-height: 1.25;
      letter-spacing: -0.025em;
      word-break: break-word;
    }}
    .author {{
      font-size: 14px;
      color: var(--secondary-label);
      font-weight: 500;
    }}
    .info-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      padding-top: 6px;
      border-top: 1px solid var(--separator);
      margin-top: 4px;
    }}
    .info-item {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .info-label {{
      font-size: 11px;
      text-transform: uppercase;
      font-weight: 600;
      color: var(--secondary-label);
      letter-spacing: 0.03em;
    }}
    .info-value {{
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }}
    .actions-card {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      min-height: 50px;
      border-radius: 14px;
      font-size: 17px;
      font-weight: 600;
      text-decoration: none;
      border: none;
      cursor: pointer;
      transition: transform 0.1s ease, opacity 0.15s ease;
      letter-spacing: -0.015em;
    }}
    .btn:active {{
      transform: scale(0.98);
      opacity: 0.85;
    }}
    .btn-primary {{
      background: var(--accent);
      color: #ffffff;
      box-shadow: 0 4px 14px rgba(0, 122, 255, 0.35);
    }}
    .btn-secondary {{
      background: var(--card-bg);
      color: var(--accent);
      border: 1px solid var(--card-border);
      font-size: 15px;
      min-height: 46px;
    }}
    .tip-card {{
      background: var(--tip-bg);
      border: 1px solid var(--tip-border);
      border-radius: 14px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 13px;
      line-height: 1.45;
      color: var(--tip-text);
    }}
    .tip-title {{
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 14px;
    }}
    .toast {{
      position: fixed;
      bottom: max(24px, env(safe-area-inset-bottom));
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: rgba(30, 30, 30, 0.95);
      color: #ffffff;
      padding: 10px 20px;
      border-radius: 999px;
      font-size: 14px;
      font-weight: 600;
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      transition: transform 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275);
      z-index: 999;
      pointer-events: none;
    }}
    .toast.show {{
      transform: translateX(-50%) translateY(0);
    }}
    .footer-note {{
      text-align: center;
      font-size: 12px;
      color: var(--secondary-label);
      margin-top: 8px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <span class="brand-bolt">⚡</span> Boltrip
      </div>
      {badge_html}
    </div>

    <div class="media-card">
      <div class="media-player-wrapper">
        {media_player_html}
      </div>
      <div class="meta-content">
        <div class="title">{clean_title}</div>
        {author_html}
        <div class="info-grid">
          <div class="info-item">
            <span class="info-label">File Size</span>
            <span class="info-value">{file_size_mb:.1f} MB</span>
          </div>
          <div class="info-item">
            <span class="info-label">Quality</span>
            <span class="info-value">{res_badge if res_badge else ("Video" if is_video else "Audio")}</span>
          </div>
          <div class="info-item">
            <span class="info-label">Length</span>
            <span class="info-value">{dur_str if dur_str else "Original"}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="actions-card">
      <a href="{raw_url}" download="{safe_download_name}" class="btn btn-primary" id="downloadBtn" onclick="notifyDownloaded()">
        📥 Save / Download File
      </a>
      <button class="btn btn-secondary" onclick="copyDirectLink()">
        📋 Copy Direct Download Link
      </button>
    </div>

    <div class="tip-card" id="telegramTip">
      <div class="tip-title">📱 Mobile Instructions</div>
      <div id="tipBody">
        • <b>Instant Play:</b> Tap the video above to stream in Full HD right inside this window.<br>
        • <b>iPhone / Safari:</b> Tap the fullscreen button, tap <b>Share (⬆️)</b> ➔ <b>Save Video</b> to save to Camera Roll.<br>
        • <b>Android / Chrome:</b> Tap <b>Save / Download File</b> above to download straight to your storage.
      </div>
    </div>

    <div class="footer-note">
      Downloaded with @Boltrip_bot • Zero ads • High speed<br>
      Developed by <a href="https://t.me/silas_inegbe" style="color: inherit; text-decoration: underline;">@silas_inegbe</a>
    </div>
  </div>

  <div class="toast" id="toast">Link copied to clipboard!</div>

  <script>
    const isTelegram = navigator.userAgent.includes('Telegram') || navigator.userAgent.includes('TG');
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;

    function notifyDownloaded() {{
      fetch('/download/{file_id}?ping=1').catch(() => {{}});
    }}

    // Auto-ping when the download page opens
    notifyDownloaded();

    if (isTelegram) {{
      const tipBody = document.getElementById('tipBody');
      if (isIOS) {{
        tipBody.innerHTML = '<b>Inside Telegram on iPhone:</b><br>' +
          '1. To save to Camera Roll: Tap play, tap <b>Share (⬆️)</b> ➔ <b>Save Video</b>.<br>' +
          '2. Or tap <b>•••</b> at top-right ➔ <b>Open in Safari</b> to download to Files.';
      }} else {{
        tipBody.innerHTML = '<b>Inside Telegram on Android:</b><br>' +
          'Tap <b>⋮</b> at top-right and select <b>Open in Chrome / Browser</b> to start download directly.';
      }}
    }}

    function copyDirectLink() {{
      notifyDownloaded();
      const fullUrl = window.location.origin + '{raw_url}';
      navigator.clipboard.writeText(fullUrl).then(() => {{
        showToast('Direct download link copied!');
      }}).catch(() => {{
        const input = document.createElement('input');
        input.value = fullUrl;
        document.body.appendChild(input);
        input.select();
        document.execCommand('copy');
        document.body.removeChild(input);
        showToast('Direct download link copied!');
      }});
    }}

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2400);
    }}
  </script>
</body>
</html>
"""

async def notify_telegram_downloaded(cache_id: str):
    """Pings the Telegram bot that a phone download occurred, updating the warning message."""
    try:
        global GLOBAL_BOT
        if not GLOBAL_BOT:
            return
        cached_item = MEDIA_CACHE.get(cache_id, {})
        if not cached_item or cached_item.get("download_notified"):
            return

        chat_id = cached_item.get("chat_id")
        status_msg_id = cached_item.get("status_msg_id")
        if not chat_id or not status_msg_id:
            return

        cached_item["download_notified"] = True
        save_cache(MEDIA_CACHE)

        title = cached_item.get("title") or "Media File"
        size_mb = cached_item.get("size_mb", 0.0)
        if not size_mb:
            file_path = cached_item.get("file_path")
            if file_path and os.path.exists(file_path):
                size_mb = os.path.getsize(file_path) / (1024 * 1024)

        import html
        t_clean = html.escape(title[:70])
        size_str = f"\n📦 <code>{size_mb:.1f} MB</code>" if size_mb > 0 else ""
        text = (
            "✅ <b>Downloaded via Phone Browser</b>\n\n"
            f"🎬 <b>{t_clean}</b>"
            f"{size_str}\n\n"
            "⚡ <i>Thanks for using @Boltrip_bot!</i>"
        )

        await GLOBAL_BOT.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=text,
            reply_markup=get_post_download_keyboard(),
            parse_mode="HTML"
        )
        logger.info(f"📲 [PHONE DOWNLOAD NOTIFIED] Successfully updated message for {cache_id} in chat {chat_id}")
    except Exception as e:
        logger.warning(f"Could not notify Telegram of phone download: {e}")

def prune_expired_downloads():
    try:
        now = time.time()
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            if os.path.isfile(f) and not f.endswith(".part"):
                if now - os.path.getmtime(f) > 7200:  # 2 hours
                    try:
                        os.remove(f)
                    except Exception:
                        pass
    except Exception:
        pass

async def handle_direct_download(request):
    prune_expired_downloads()
    file_id = request.match_info.get("file_id")

    # Handle asynchronous ping notification from web page/browser
    if request.query.get("ping") == "1":
        asyncio.create_task(notify_telegram_downloaded(file_id))
        return web.Response(text="OK")

    matching = glob.glob(os.path.join(DOWNLOAD_DIR, f"*{file_id}*"))
    matching = [m for m in matching if not m.endswith(".part") and os.path.isfile(m)]
    if not matching:
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return web.Response(text=render_expired_page(), content_type="text/html", status=404)
        return web.Response(text="Download expired or not found. Please request from @Boltrip_bot again.", status=404)

    file_path = matching[0]
    filename = os.path.basename(file_path)
    size_bytes = os.path.getsize(file_path)
    size_mb = size_bytes / (1024 * 1024)

    ext = os.path.splitext(filename)[1].lower()
    is_video = ext in ['.mp4', '.mkv', '.webm', '.mov', '.avi']
    is_audio = ext in ['.mp3', '.m4a', '.ogg', '.opus', '.wav', '.flac']
    is_image = ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']

    if is_video:
        content_type = "video/mp4" if ext == ".mp4" else f"video/{ext.lstrip('.')}"
    elif is_audio:
        content_type = "audio/mpeg" if ext == ".mp3" else ("audio/mp4" if ext == ".m4a" else f"audio/{ext.lstrip('.')}")
    elif is_image:
        content_type = "image/jpeg" if ext in ['.jpg', '.jpeg'] else f"image/{ext.lstrip('.')}"
    else:
        content_type = "application/octet-stream"

    safe_ascii = re.sub(r'[^\w\.-]', '_', filename)
    if not safe_ascii:
        safe_ascii = f"boltrip_media{ext}"
    encoded_filename = urllib.parse.quote(filename)

    raw_param = request.query.get("raw") == "1"
    user_agent = request.headers.get("User-Agent", "")
    is_cli = any(tool in user_agent.lower() for tool in ["curl", "wget", "aria2"])

    if raw_param or is_cli:
        logger.info(f"🌐 [PHONE DOWNLOAD RAW] Client downloaded '{filename}' ({size_mb:.1f} MB)")
        asyncio.create_task(notify_telegram_downloaded(file_id))
        headers = {
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded_filename}',
            "Accept-Ranges": "bytes",
            "Content-Length": str(size_bytes),
            "Cache-Control": "public, max-age=3600",
        }
        return web.FileResponse(file_path, headers=headers)

    stream_param = request.query.get("stream") == "1"
    if stream_param:
        headers = {
            "Content-Type": content_type,
            "Content-Disposition": f'inline; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded_filename}',
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }
        return web.FileResponse(file_path, headers=headers)

    cached_info = MEDIA_CACHE.get(file_id, {})
    meta = probe_video_metadata(file_path) if is_video else {}
    title = cached_info.get("title") or filename
    author = cached_info.get("author") or ""
    width = meta.get("width")
    height = meta.get("height")
    duration = meta.get("duration", 0)

    html_content = render_download_page(
        file_id=file_id,
        filename=filename,
        file_size_mb=size_mb,
        is_video=is_video,
        is_audio=is_audio,
        title=title,
        author=author,
        width=width,
        height=height,
        duration=duration
    )
    return web.Response(text=html_content, content_type="text/html")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "there"
    welcome_text = (
        f"**Welcome to Boltrip, {user_name}.**\n\n"
        "A fast, clean media downloader with zero ads and no subscription walls.\n\n"
        "**Supported Platforms:**\n"
        "• YouTube (Videos & Shorts)\n"
        "• TikTok (No watermark)\n"
        "• Instagram (Reels & Posts)\n"
        "• Threads (Videos & Photos)\n"
        "• X / Twitter\n"
        "• Pinterest\n"
        "• Facebook & Reddit\n\n"
        "**Developer Contact:**\n"
        "• Contact [@silas_inegbe](https://t.me/silas_inegbe) for inquiries, feedback, or support.\n\n"
        "**How to Use:**\n"
        "1. Paste any media link into this chat.\n"
        "2. Choose your format: 1080p Full HD, 720p, or MP3 Audio.\n"
        "3. Or tap **[✂️ Clip / Trim Video]** to download only a specific section!\n"
        "4. Track live download and upload progress.\n\n"
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
        "• `/clip <url> [start] - [end]` ➔ Clip/trim video portion\n"
        "• `/contact` ➔ Contact developer (@silas_inegbe)\n"
        "• `/support` or `/donate` ➔ Help us keep servers free & ad-free\n"
        "• `/cancel` ➔ Abort ongoing download or upload\n\n"
        "**Features:**\n"
        "• ✂️ Video section trimming (YouTube, Twitter, Facebook, etc.)\n"
        "• ❌ 1-Tap Cancel on every step\n"
        "• 📊 Real-time progress bars & download speed\n"
        "• 📱 Direct phone browser download for large files (>50 MB)\n"
        "• 🗜️ In-chat smart compressor\n\n"
        "👨‍💻 **Developer:** [@silas_inegbe](https://t.me/silas_inegbe)"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id and user_id in WAITING_FOR_CLIP:
        WAITING_FOR_CLIP.pop(user_id, None)
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

async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    text = (update.message.text or "").strip()
    args = text.split()[1:]
    if not args:
        disclaimer_msg = (
            "✂️ **Video Section Clipper / Trimmer**\n\n"
            "⚠️ **Supported Platforms Disclaimer:**\n"
            "Selective section streaming works on:\n"
            "• **YouTube** (e.g. 1–3+ hour podcasts, live streams, tutorials)\n"
            "• **Twitch & Kick**\n"
            "• **X / Twitter**\n"
            "• **Facebook & Reddit**\n\n"
            "❌ **Not supported for section streaming:**\n"
            "• **TikTok, Instagram Reels, Threads, Pinterest**\n"
            "*(Micro-videos [<60s] do not support selective streaming)*\n\n"
            "📖 **How to Use:**\n"
            "1. Simply paste your video link in chat and tap **[✂️ Clip / Trim Video]**\n"
            "2. OR run command:\n"
            "   `/clip <url> 01:30 - 03:00`"
        )
        await update.message.reply_text(disclaimer_msg, parse_mode="Markdown")
        return

    first_arg = args[0]
    url = extract_url(first_arg) or extract_url(text)
    if not url:
        await update.message.reply_text(
            "⚠️ No valid video URL found in your command.\n\n"
            "**Example:** `/clip https://youtu.be/xxx 01:20 - 02:45`",
            parse_mode="Markdown"
        )
        return

    platform = get_platform_info(url)
    if platform in ['tiktok', 'instagram', 'threads', 'pinterest']:
        await update.message.reply_text(
            f"⚠️ **Clipping is not supported on {platform.title()}**\n\n"
            f"TikTok, Instagram Reels, Threads, and Pinterest videos are already short mobile clips (<60s) and do not support section streaming.\n\n"
            f"Clipping works on: **YouTube, Twitch, X/Twitter, Facebook, and Reddit**.",
            parse_mode="Markdown"
        )
        return

    rest_text = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    rest_text = rest_text.replace(url, "").strip()

    clip_range = parse_clip_range(rest_text) if rest_text else None
    if clip_range:
        start_sec, end_sec = clip_range
        clip_dur = end_sec - start_sec
        if clip_dur > 900:
            await update.message.reply_text(
                f"⚠️ **Clip duration is too long ({int(clip_dur // 60)} minutes).**\n"
                f"Maximum allowed clip length is **15 minutes**.\n"
                f"Please specify a shorter range (e.g. `01:00 - 05:00`).",
                parse_mode="Markdown"
            )
            return

        cache_id = str(abs(hash(url)) % 10000000)
        start_str = format_seconds_to_time(start_sec)
        end_str = format_seconds_to_time(end_sec)
        dur_str = format_seconds_to_time(clip_dur)

        status_msg = await update.message.reply_text(
            f"✂️ **Clipping Video: {start_str} - {end_str}** (`{dur_str}`)\n"
            f"🔗 `{url[:45]}`\n\n"
            f"⏳ Initializing fast segment download...",
            reply_markup=get_cancel_keyboard(cache_id),
            parse_mode="Markdown"
        )

        MEDIA_CACHE[cache_id] = {
            "url": url,
            "title": f"Clip ({start_str} - {end_str})",
            "author": "Video Clip",
            "platform": platform,
            "has_video": True
        }
        save_cache(MEDIA_CACHE)

        t = asyncio.create_task(
            download_and_send(
                update, context, status_msg, cache_id, quality="1080", clip_range=(start_sec, end_sec)
            )
        )
        ACTIVE_TASKS[cache_id] = t
        return
    else:
        await process_url(update, context, url)

async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_text = (
        "👨‍💻 **Developer Contact & Support**\n\n"
        "Have a question, feedback, bug report, or feature request?\n\n"
        "💬 **Telegram:** [@silas_inegbe](https://t.me/silas_inegbe)\n\n"
        "Feel free to send a direct message anytime!"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message Developer (@silas_inegbe)", url="https://t.me/silas_inegbe")],
        [InlineKeyboardButton("💖 Support Boltrip", callback_data="show_support")]
    ])
    await update.message.reply_text(contact_text, reply_markup=keyboard, parse_mode="Markdown")

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

    platform = get_platform_info(url)
    reply_markup = get_quality_keyboard(cache_id, platform)
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
    # Follow redirects early so Facebook share/v/ links resolve to real post URLs
    url = resolve_url(url)
    logger.info(f"📥 [REQUEST] {user_str} sent link: {url}")
    cache_id = str(abs(hash(url)) % 10000000)
    cancel_markup = InlineKeyboardMarkup([[get_cancel_button(cache_id, "❌ Cancel")]])
    status_msg = await update.message.reply_text("⚡ **Analyzing link...**", reply_markup=cancel_markup, parse_mode="Markdown")

    loop = asyncio.get_running_loop()

    def fetch_meta(target_url=None):
        u = target_url or url
        # Fast oEmbed path for YouTube (100% reliable, zero player-response errors)
        if "youtube.com" in u or "youtu.be" in u:
            try:
                oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(u)}&format=json"
                req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read().decode())
                    if data.get("title"):
                        return {
                            "title": data.get("title", "YouTube Video")[:80],
                            "author": data.get("author_name", "YouTube Creator"),
                            "is_tiktok": False,
                            "resolved_url": u,
                        }
            except Exception:
                pass

        if is_tiktok_url(u):
            try:
                api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(u)}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get("code") == 0:
                        d = data["data"]
                        return {
                            "title": d.get("title", "TikTok Video")[:80],
                            "author": d.get("author", {}).get("nickname", "TikTok Creator"),
                            "is_tiktok": True,
                            "resolved_url": u,
                        }
            except Exception:
                pass

        if is_facebook_url(u):
            fb_meta = fetch_facebook_direct(u)
            if fb_meta:
                return {
                    "title": fb_meta.get("title", "Facebook Video")[:80],
                    "author": fb_meta.get("author", "Facebook Creator"),
                    "is_tiktok": False,
                    "is_facebook": True,
                    "resolved_url": u,
                }

        if is_pinterest_url(u):
            pin_meta = fetch_pinterest_direct(u)
            if pin_meta:
                return pin_meta

        if is_threads_url(u):
            threads_meta = fetch_threads_direct(u)
            if threads_meta:
                if threads_meta.get("is_external_link") and threads_meta.get("resolved_url"):
                    ext_url = threads_meta["resolved_url"]
                    logger.info(f"Threads post links to external media: {ext_url}")
                    ext_meta = fetch_meta(ext_url)
                    if ext_meta:
                        ext_meta["resolved_url"] = ext_url
                        return ext_meta
                return threads_meta
            else:
                raise Exception("Threads: No downloadable video, photo, or media attachment found on this post.")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "js_runtimes": {"deno": {}, "node": {}},
            "extractor_args": {
                "twitter": {"api": ["syndication"]},
                "youtube": {"player_client": ["ios", "android", "web"]},
            },
        }
        if CHROME_IMPERSONATE:
            opts["impersonate"] = CHROME_IMPERSONATE
        is_yt = ("youtube.com" in u or "youtu.be" in u)
        if is_yt:
            yt_c = get_yt_cookie_file()
            if yt_c and os.path.exists(yt_c):
                opts["cookiefile"] = yt_c
        elif os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            opts["cookiefile"] = COOKIE_FILE_PATH
        if PROXY_URL:
            opts["proxy"] = PROXY_URL
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u, download=False)
            return {
                "title": (info.get("title") or "Media")[:80],
                "author": info.get("uploader", info.get("channel", "Creator")),
                "is_tiktok": False,
                "resolved_url": u,
            }

    try:
        meta = await loop.run_in_executor(None, fetch_meta)
    except Exception as e:
        err_text = str(e)
        if "unable to extract video" in err_text.lower() and "linkedin" in err_text.lower():
            friendly_err = (
                "⚠️ **No video found on this LinkedIn post.**\n\n"
                "• This post does not contain a playable video (it might be a text, image, or document post).\n"
                "• Or the post might be restricted to 1st-degree connections, private, or deleted."
            )
            await status_msg.edit_text(friendly_err, parse_mode="Markdown")
        elif "threads" in err_text.lower() and ("no downloadable" in err_text.lower() or "no media" in err_text.lower() or "unsupported url" in err_text.lower()):
            friendly_err = (
                "⚠️ **No video or photo found on this Threads post.**\n\n"
                "• This post might only contain text without any attached video or image.\n"
                "• Or the post might be restricted, private, or deleted."
            )
            await status_msg.edit_text(friendly_err, parse_mode="Markdown")
        else:
            await status_msg.edit_text(f"❌ **Failed to load link:**\n`{err_text[:200]}`", parse_mode="Markdown")
        return

    target_url = meta.get("resolved_url") or url
    title = meta.get("title", "Media")
    author = meta.get("author", "Creator")
    logger.info(f"🔍 [METADATA] Extracted: '{title[:45]}' | Creator: '{author}' (target_url: {target_url})")
    platform = get_platform_info(target_url)
    MEDIA_CACHE[cache_id] = {
        "url": target_url,
        "title": title,
        "author": author,
        "platform": platform,
        "is_pinterest": meta.get("is_pinterest", False),
        "is_threads": meta.get("is_threads", False),
        "media_url": meta.get("media_url"),
        "ext": meta.get("ext", "mp4"),
        "has_video": meta.get("has_video", True),
    }
    save_cache(MEDIA_CACHE)

    if force_audio:
        t = asyncio.create_task(download_and_send(update, context, status_msg, cache_id, quality="audio"))
        ACTIVE_TASKS[cache_id] = t
        return
    elif force_video:
        t = asyncio.create_task(download_and_send(update, context, status_msg, cache_id, quality="1080"))
        ACTIVE_TASKS[cache_id] = t
        return

    reply_markup = get_quality_keyboard(cache_id, platform)

    await status_msg.edit_text(
        f"🎬 **{title}**\n👤 _{author}_\n\nSelect desired download quality:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def handle_incoming_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    user_id = user.id if user else None

    # Check if this user is waiting to send a clip timestamp range
    if user_id and user_id in WAITING_FOR_CLIP:
        # Check if user sent a new URL instead
        new_url = extract_url(text)
        if new_url and not any(text.startswith(cmd) for cmd in ["/clip", "/trim"]):
            # Abandon clipping and process new URL
            WAITING_FOR_CLIP.pop(user_id, None)
        else:
            clip_range = parse_clip_range(text)
            if clip_range:
                start_sec, end_sec = clip_range
                clip_dur = end_sec - start_sec
                if clip_dur > 900:  # 15 minutes limit
                    await update.message.reply_text(
                        f"⚠️ **Clip duration is too long ({int(clip_dur // 60)} minutes).**\n"
                        f"Maximum allowed clip length is **15 minutes**.\n"
                        f"Please reply with a shorter range (e.g. `01:00 - 05:00`).",
                        parse_mode="Markdown"
                    )
                    return

                clip_state = WAITING_FOR_CLIP.pop(user_id, None)
                cache_id = clip_state["cache_id"]
                cached = MEDIA_CACHE.get(cache_id)
                if not cached:
                    await update.message.reply_text("⚠️ Download session expired. Please paste the link again.")
                    return

                start_str = format_seconds_to_time(start_sec)
                end_str = format_seconds_to_time(end_sec)
                dur_str = format_seconds_to_time(clip_dur)
                v_title = cached.get("title", "Media")

                status_msg = await update.message.reply_text(
                    f"✂️ **Clipping Video: {start_str} - {end_str}** (`{dur_str}`)\n"
                    f"`{v_title[:45]}`\n\n"
                    f"⏳ Initializing fast segment download...",
                    reply_markup=get_cancel_keyboard(cache_id),
                    parse_mode="Markdown"
                )

                t = asyncio.create_task(
                    download_and_send(
                        update, context, status_msg, cache_id, quality="1080", clip_range=(start_sec, end_sec)
                    )
                )
                ACTIVE_TASKS[cache_id] = t
                return
            else:
                await update.message.reply_text(
                    "⚠️ **Unrecognized timestamp format.**\n\n"
                    "Please reply with start and end times, e.g.:\n"
                    "• `01:30 - 03:00`\n"
                    "• `10:00 - 12:30`\n"
                    "• `90s - 180s`\n\n"
                    "Or tap /cancel to abort clipping.",
                    parse_mode="Markdown"
                )
                return

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

    if action == "cancel_clip":
        user_id = query.from_user.id
        WAITING_FOR_CLIP.pop(user_id, None)
        await query.edit_message_text("❌ Clipping cancelled.")
        return

    if action == "back_formats":
        user_id = query.from_user.id
        WAITING_FOR_CLIP.pop(user_id, None)
        platform = cached.get("platform") or get_platform_info(cached.get("url", ""))
        await query.edit_message_text(
            f"🎬 **{title}**\n👤 _{author}_\n\nSelect desired download quality:",
            reply_markup=get_quality_keyboard(cache_id, platform),
            parse_mode="Markdown"
        )
        return

    if action == "clip":
        platform = cached.get("platform") or get_platform_info(cached.get("url", ""))
        unsupported = ["tiktok", "instagram", "threads", "pinterest"]
        if platform in unsupported:
            p_name = platform.title()
            await query.answer(f"Clipping is not supported for {p_name}!", show_alert=True)
            await query.message.reply_text(
                f"⚠️ **Clipping Not Supported for {p_name}**\n\n"
                f"Selective video section clipping works on long-form platforms (**YouTube, Twitch, X/Twitter, Facebook, Reddit**).\n\n"
                f"{p_name} videos are already short micro-clips (< 60s) and do not support section streaming.\n"
                f"Please choose a full download format from the options above.",
                parse_mode="Markdown"
            )
            return

        user_id = query.from_user.id
        WAITING_FOR_CLIP[user_id] = {
            "cache_id": cache_id,
            "status_msg_id": query.message.message_id,
            "chat_id": query.message.chat_id,
            "created_at": time.time(),
        }
        clip_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Formats", callback_data=f"back_formats:{cache_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_clip:{cache_id}")]
        ])
        await query.edit_message_text(
            f"✂️ **Clip / Trim Video Section**\n"
            f"`{title[:50]}`\n\n"
            f"⚠️ **Supported Platforms Disclaimer:**\n"
            f"• **Supported:** YouTube, Twitch, X / Twitter, Facebook, Reddit\n"
            f"• **Not Supported:** TikTok, Instagram, Threads, Pinterest _(micro-videos do not support selective section streaming)_\n\n"
            f"⏱️ **How to Clip:**\n"
            f"Reply with the **start** and **end** timestamp of the part you want to download:\n\n"
            f"**Examples:**\n"
            f"• `01:30 - 03:00` (from 1m 30s to 3m 00s)\n"
            f"• `15:00 - 18:30`\n"
            f"• `01:05:00 - 01:08:30` (for hour-long videos)\n"
            f"• `90s - 180s`\n\n"
            f"_(Maximum clip duration: 15 minutes)_",
            reply_markup=clip_markup,
            parse_mode="Markdown"
        )
        return

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

    if action in ["vidbest", "vid4k", "vid1080", "vid720", "aud"]:
        quality_map = {
            "vidbest": "best",
            "vid4k": "4k",
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
        cached_item = MEDIA_CACHE.get(cache_id, {})
        cached_item["chat_id"] = update.effective_chat.id
        cached_item["status_msg_id"] = status_msg.message_id
        MEDIA_CACHE[cache_id] = cached_item
        save_cache(MEDIA_CACHE)
        await status_msg.edit_text(
            f"⚠️ Compression failed or cancelled.\nYou can still download the high-res file directly to your phone:",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=False)
        )
        return

    comp_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
    logger.info(f"🗜️ [COMPRESS COMPLETE] Compressed {input_path} down to {comp_size_mb:.1f} MB")
    if comp_size_mb > 49.5:
        cached_item = MEDIA_CACHE.get(cache_id, {})
        cached_item["chat_id"] = update.effective_chat.id
        cached_item["status_msg_id"] = status_msg.message_id
        cached_item["size_mb"] = comp_size_mb
        MEDIA_CACHE[cache_id] = cached_item
        save_cache(MEDIA_CACHE)
        await status_msg.edit_text(
            f"⚠️ Compressed file is {comp_size_mb:.1f} MB (still exceeds Telegram's 50 MB limit).\nPlease use the direct phone download:",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=False)
        )
        return

    await deliver_file(update, context, status_msg, cache_id, compressed_path, is_audio=False, title=title, author=author, is_tiktok=is_tiktok)

async def download_and_send(update, context, status_msg, cache_id, quality="1080", clip_range=None):
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

    # Save chat_id and status_msg_id early
    cached["chat_id"] = update.effective_chat.id
    cached["status_msg_id"] = status_msg.message_id
    MEDIA_CACHE[cache_id] = cached
    save_cache(MEDIA_CACHE)

    # --- 1. INSTANT TELEGRAM FILE_ID CACHE CHECK ---
    if clip_range:
        s_sec, e_sec = clip_range
        cache_key = f"{url}_clip_{int(s_sec)}_{int(e_sec)}"
    else:
        cache_key = f"{url}_{quality}"

    cached_file = FILE_CACHE.get(cache_key)
    if cached_file and cached_file.get("file_id"):
        logger.info(f"⚡ Instant delivery from Telegram cache for {url} ({cache_key})")
        file_id = cached_file["file_id"]
        c_title = cached_file.get("title", title)
        c_author = cached_file.get("author", author)
        c_size = cached_file.get("size_mb", 0.0)
        c_type = cached_file.get("type", "video")

        import html
        t_clean = html.escape(c_title[:100])
        a_clean = html.escape(c_author[:60])
        clip_tag = ""
        if clip_range:
            s_sec, e_sec = clip_range
            s_str = format_seconds_to_time(s_sec)
            e_str = format_seconds_to_time(e_sec)
            dur_str = format_seconds_to_time(e_sec - s_sec)
            clip_tag = f"✂️ <b>Clip:</b> <code>{s_str} - {e_str}</code> (⏱️ {dur_str})\n"

        caption = (
            f"🎬 <b>{t_clean}</b>\n"
            f"👤 <i>{a_clean}</i>\n"
            f"{clip_tag}"
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
            elif c_type == "photo":
                sent_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
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

            mode_display = f"Clip {format_seconds_to_time(clip_range[0])} - {format_seconds_to_time(clip_range[1])}" if clip_range else quality
            await status_msg.edit_text(
                f"⏳ **Starting download...**\n`{title[:45]}`\n\nMode: `{mode_display}`",
                reply_markup=cancel_markup,
                parse_mode="Markdown"
            )
            await _run_download_workflow(update, context, status_msg, cache_id, quality, url, title, author, is_audio, cancel_markup, main_loop, clip_range=clip_range)
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

async def _run_download_workflow(update, context, status_msg, cache_id, quality, url, title, author, is_audio, cancel_markup, main_loop, clip_range=None):

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

            if clip_range:
                s_sec, e_sec = clip_range
                text = (
                    f"✂️ **Clipping & Downloading...**\n"
                    f"`{title[:40]}`\n"
                    f"⏱️ `{format_seconds_to_time(s_sec)} - {format_seconds_to_time(e_sec)}`\n\n"
                    f"{bar} **{percent:.1f}%**\n"
                    f"⚡ `{speed_str}` | ⏱️ `{eta_str}`\n"
                    f"📦 `{size_str}`"
                )
            else:
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
    is_fb = is_facebook_url(url)
    is_pin = is_pinterest_url(url)
    is_threads = is_threads_url(url)
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

    if is_fb and not downloaded_path and not clip_range:
        try:
            loop = asyncio.get_running_loop()
            downloaded_path = await loop.run_in_executor(
                None, lambda: download_facebook_direct(url, cache_id, progress_hook, quality=quality)
            )
            if downloaded_path and is_audio:
                mp3_path = os.path.splitext(downloaded_path)[0] + ".mp3"
                if FFMPEG_PATH:
                    cmd = [FFMPEG_PATH, "-y", "-i", downloaded_path, "-vn", "-c:a", "libmp3lame", "-q:a", "2", mp3_path]
                    await loop.run_in_executor(None, lambda: subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True))
                    try:
                        os.remove(downloaded_path)
                    except Exception:
                        pass
                    downloaded_path = mp3_path
        except Exception as e:
            logger.warning(f"Facebook direct download failed, falling back to yt-dlp: {e}")
            downloaded_path = None

    if is_pin and not downloaded_path:
        try:
            loop = asyncio.get_running_loop()
            cached_media_url = MEDIA_CACHE.get(cache_id, {}).get("media_url")
            cached_ext = MEDIA_CACHE.get(cache_id, {}).get("ext", "mp4")
            downloaded_path = await loop.run_in_executor(
                None, lambda: download_pinterest_direct(url, cache_id, progress_hook, direct_media_url=cached_media_url, ext=cached_ext)
            )
            if downloaded_path and is_audio and downloaded_path.endswith(".mp4"):
                mp3_path = os.path.splitext(downloaded_path)[0] + ".mp3"
                if FFMPEG_PATH:
                    cmd = [FFMPEG_PATH, "-y", "-i", downloaded_path, "-vn", "-c:a", "libmp3lame", "-q:a", "2", mp3_path]
                    await loop.run_in_executor(None, lambda: subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True))
                    try:
                        os.remove(downloaded_path)
                    except Exception:
                        pass
                    downloaded_path = mp3_path
        except Exception as e:
            logger.warning(f"Pinterest direct download failed, falling back to yt-dlp: {e}")
            downloaded_path = None

    if is_threads and not downloaded_path:
        try:
            loop = asyncio.get_running_loop()
            cached_media_url = MEDIA_CACHE.get(cache_id, {}).get("media_url")
            cached_ext = MEDIA_CACHE.get(cache_id, {}).get("ext", "mp4")
            downloaded_path = await loop.run_in_executor(
                None, lambda: download_threads_direct(url, cache_id, progress_hook, direct_media_url=cached_media_url, ext=cached_ext)
            )
            if downloaded_path and is_audio and downloaded_path.endswith(".mp4"):
                mp3_path = os.path.splitext(downloaded_path)[0] + ".mp3"
                if FFMPEG_PATH:
                    cmd = [FFMPEG_PATH, "-y", "-i", downloaded_path, "-vn", "-c:a", "libmp3lame", "-q:a", "2", mp3_path]
                    await loop.run_in_executor(None, lambda: subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True))
                    try:
                        os.remove(downloaded_path)
                    except Exception:
                        pass
                    downloaded_path = mp3_path
        except Exception as e:
            logger.warning(f"Threads direct download failed, falling back to yt-dlp: {e}")
            downloaded_path = None

    if not downloaded_path:
        outtmpl = os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_%(id)s.%(ext)s")
        is_yt = ("youtube.com" in url or "youtu.be" in url)
        yt_cookies = get_yt_cookie_file() if is_yt else None

        if is_audio:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 10,
                "concurrent_fragment_downloads": 1 if is_yt else 8,
                "buffersize": 1024 * 128,
                "js_runtimes": {"node": {}, "deno": {}},
                "extractor_args": {
                    "twitter": {"api": ["syndication"]},
                    "youtube": {"player_client": ["web_embedded", "web", "default"]} if is_yt else {}
                },
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            if quality == "720":
                fmt = "bestvideo[height<=720]+bestaudio/bestvideo[height<=720]+bestaudio[ext=m4a]/best[height<=720]/best"
            elif quality == "4k":
                fmt = "bestvideo[height<=2160]+bestaudio/bestvideo[height<=2160]+bestaudio[ext=m4a]/best[height<=2160]/best"
            elif quality == "1080":
                fmt = "bestvideo[height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio[ext=m4a]/best[height<=1080]/best"
            else:
                fmt = "bestvideo+bestaudio/bestvideo+bestaudio[ext=m4a]/best"
            
            ydl_opts = {
                "format": fmt,
                "format_sort": ["res", "vcodec:h264", "ext:mp4:m4a"],
                "outtmpl": outtmpl,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 10,
                "concurrent_fragment_downloads": 1 if is_yt else 8,
                "buffersize": 1024 * 128,
                "merge_output_format": "mp4",
                "postprocessor_args": {
                    "Merger": ["-movflags", "+faststart"],
                    "FFmpegVideoRemuxer": ["-movflags", "+faststart"],
                },
                "js_runtimes": {"node": {}, "deno": {}},
                "extractor_args": {
                    "twitter": {"api": ["syndication"]},
                    "youtube": {"player_client": ["web_embedded", "web", "default"]} if is_yt else {}
                },
            }
        if FFMPEG_PATH:
            ydl_opts["ffmpeg_location"] = FFMPEG_PATH
        ydl_opts["source_address"] = "0.0.0.0"
        if CHROME_IMPERSONATE:
            ydl_opts["impersonate"] = CHROME_IMPERSONATE

        if is_yt:
            if yt_cookies and os.path.exists(yt_cookies):
                ydl_opts["cookiefile"] = yt_cookies
        elif os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ydl_opts["cookiefile"] = COOKIE_FILE_PATH

        if PROXY_URL:
            ydl_opts["proxy"] = PROXY_URL

        if clip_range:
            start_sec, end_sec = clip_range
            ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)])
            ydl_opts["force_keyframes_at_cuts"] = True

        loop = asyncio.get_running_loop()
        try:
            def run_ydl():
                try:
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
                except Exception as first_err:
                    err_str = str(first_err).lower()
                    logger.warning(f"Primary yt-dlp download failed on {url}: {first_err}")
                    
                    if is_yt:
                        # Fallback 1: Web Embedded & Web Safari without cookies (bypasses cookie session challenge while keeping Full HD)
                        logger.warning(f"Retrying YouTube {url} with Web Embedded / Safari client...")
                        fb1_opts = dict(ydl_opts)
                        fb1_opts.pop("cookiefile", None)
                        fb1_opts["extractor_args"] = {
                            "youtube": {"player_client": ["web_embedded", "web_safari"]}
                        }
                        if not is_audio:
                            fb1_opts["format"] = fmt
                        fb1_opts["concurrent_fragment_downloads"] = 1
                        try:
                            with yt_dlp.YoutubeDL(fb1_opts) as ydl_fb1:
                                info = ydl_fb1.extract_info(url, download=True)
                                fname = ydl_fb1.prepare_filename(info)
                                if is_audio:
                                    fname = os.path.splitext(fname)[0] + ".mp3"
                                elif not fname.endswith(".mp4"):
                                    base = os.path.splitext(fname)[0]
                                    if os.path.exists(base + ".mp4"):
                                        fname = base + ".mp4"
                                return fname, info
                        except Exception as fb1_err:
                            logger.warning(f"Fallback 1 (Web Embedded/Safari) failed: {fb1_err}")

                        # Fallback 2: mweb / iOS client preserving requested quality
                        logger.warning(f"Retrying YouTube {url} with mweb / iOS client...")
                        fb2_opts = dict(ydl_opts)
                        fb2_opts.pop("cookiefile", None)
                        fb2_opts["extractor_args"] = {
                            "youtube": {"player_client": ["mweb", "ios"]}
                        }
                        if not is_audio:
                            fb2_opts["format"] = fmt
                        fb2_opts["concurrent_fragment_downloads"] = 1
                        try:
                            with yt_dlp.YoutubeDL(fb2_opts) as ydl_fb2:
                                info = ydl_fb2.extract_info(url, download=True)
                                fname = ydl_fb2.prepare_filename(info)
                                if is_audio:
                                    fname = os.path.splitext(fname)[0] + ".mp3"
                                elif not fname.endswith(".mp4"):
                                    base = os.path.splitext(fname)[0]
                                    if os.path.exists(base + ".mp4"):
                                        fname = base + ".mp4"
                                return fname, info
                        except Exception as fb2_err:
                            logger.warning(f"Fallback 2 (mweb/iOS) failed: {fb2_err}")

                        # Fallback 3: Android & TV Downgraded client (absolute last-resort fallback)
                        logger.warning(f"Retrying YouTube {url} with Android / TV Downgraded client...")
                        fb3_opts = dict(ydl_opts)
                        fb3_opts.pop("cookiefile", None)
                        fb3_opts["extractor_args"] = {
                            "youtube": {"player_client": ["android", "tv_downgraded"]}
                        }
                        if not is_audio:
                            fb3_opts["format"] = "best[ext=mp4]/bestvideo+bestaudio/best"
                        fb3_opts["concurrent_fragment_downloads"] = 1
                        try:
                            with yt_dlp.YoutubeDL(fb3_opts) as ydl_fb3:
                                info = ydl_fb3.extract_info(url, download=True)
                                fname = ydl_fb3.prepare_filename(info)
                                if is_audio:
                                    fname = os.path.splitext(fname)[0] + ".mp3"
                                elif not fname.endswith(".mp4"):
                                    base = os.path.splitext(fname)[0]
                                    if os.path.exists(base + ".mp4"):
                                        fname = base + ".mp4"
                                return fname, info
                        except Exception as fb3_err:
                            logger.warning(f"Fallback 3 (Android/TV) failed: {fb3_err}")

                    raise first_err

            fname, info = await loop.run_in_executor(None, run_ydl)
            if os.path.exists(fname):
                downloaded_path = fname
            else:
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"dl_{cache_id}_*"))
                matches = [m for m in matches if not m.endswith(".part")]
                if matches:
                    downloaded_path = matches[0]

            # Verify and trim if duration exceeds requested range
            if clip_range and downloaded_path and os.path.exists(downloaded_path) and FFMPEG_PATH:
                start_sec, end_sec = clip_range
                target_dur = end_sec - start_sec
                meta = probe_video_metadata(downloaded_path)
                actual_dur = meta.get("duration", 0)
                if actual_dur > target_dur + 4.0:
                    logger.info(f"Trimming with FFmpeg: actual duration {actual_dur}s > target {target_dur}s")
                    ext = os.path.splitext(downloaded_path)[1] or (".mp3" if is_audio else ".mp4")
                    trimmed_path = os.path.join(DOWNLOAD_DIR, f"trimmed_{cache_id}_{int(start_sec)}_{int(end_sec)}{ext}")
                    def do_trim():
                        trim_cmd = [
                            FFMPEG_PATH, "-y",
                            "-ss", str(start_sec),
                            "-i", downloaded_path,
                            "-t", str(target_dur),
                            "-c", "copy",
                            trimmed_path
                        ]
                        return subprocess.run(trim_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    trim_res = await loop.run_in_executor(None, do_trim)
                    if trim_res.returncode == 0 and os.path.exists(trimmed_path) and os.path.getsize(trimmed_path) > 0:
                        try:
                            os.remove(downloaded_path)
                        except Exception:
                            pass
                        downloaded_path = trimmed_path
        except asyncio.CancelledError:
            cleanup_files(cache_id)
            raise
        except Exception as e:
            if "Cancelled by user" in str(e):
                cleanup_files(cache_id)
                return
            logger.error(f"yt-dlp download failed: {e}")
            err_msg = str(e)
            if "confirm you" in err_msg.lower() and "bot" in err_msg.lower():
                user_friendly_err = (
                    "🔒 **YouTube Bot Verification Required**\n\n"
                    "YouTube blocked anonymous requests from this server IP.\n\n"
                    "💡 **How to fix this permanently (1-minute setup):**\n"
                    "1. Open an **Incognito / Private Window** in your browser.\n"
                    "2. Go to `https://www.youtube.com` and log in.\n"
                    "3. In the exact same tab, visit: `https://www.youtube.com/robots.txt`\n"
                    "4. Click **'Get cookies.txt LOCALLY'** to export your cookies.\n"
                    "5. **Close the Incognito window immediately** (do not browse further).\n"
                    "6. Paste the contents into `COOKIES_CONTENT` on Render.\n\n"
                    "*(Closing the window freezes the session so Google never rotates it!)*"
                )
                await status_msg.edit_text(user_friendly_err, parse_mode="Markdown")
            elif "403" in err_msg.lower() or "forbidden" in err_msg.lower():
                user_friendly_err = (
                    "🚫 **YouTube 403 Forbidden**\n\n"
                    "YouTube's video stream rejected the connection from the cloud server IP.\n\n"
                    "💡 **Tip:** If you have active YouTube cookies, update `COOKIES_CONTENT` on Render, or try another video format."
                )
                await status_msg.edit_text(user_friendly_err, parse_mode="Markdown")
            else:
                await status_msg.edit_text(f"❌ **Download failed:**\n`{err_msg[:250]}`", parse_mode="Markdown")
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
        is_audio=is_audio, title=title, author=author, is_tiktok=is_tiktok, quality=quality, clip_range=clip_range
    )

async def deliver_file(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok, quality="1080", clip_range=None):
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if file_size_mb >= 49.5:
        cached_item = MEDIA_CACHE.get(cache_id, {})
        cached_item["chat_id"] = update.effective_chat.id
        cached_item["status_msg_id"] = status_msg.message_id
        cached_item["size_mb"] = file_size_mb
        MEDIA_CACHE[cache_id] = cached_item
        save_cache(MEDIA_CACHE)
        await status_msg.edit_text(
            f"⚠️ **File is {file_size_mb:.1f} MB (Telegram Bot limit is 50 MB)**\n\n"
            f"Choose how you want to receive it:\n"
            f"📱 **Download directly to phone browser** at full quality, OR\n"
            f"🗜️ **Auto-compress video** to ~45 MB and send directly inside this chat.",
            reply_markup=get_large_file_keyboard(cache_id, is_audio=is_audio),
            parse_mode="Markdown"
        )
        return

    await upload_to_telegram(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok, quality=quality, clip_range=clip_range)

async def upload_to_telegram(update, context, status_msg, cache_id, file_path, is_audio, title, author, is_tiktok, quality="1080", clip_range=None):
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
    clip_line = ""
    if clip_range:
        s_sec, e_sec = clip_range
        s_str = format_seconds_to_time(s_sec)
        e_str = format_seconds_to_time(e_sec)
        dur_str = format_seconds_to_time(e_sec - s_sec)
        clip_line = f"✂️ <b>Clip:</b> <code>{s_str} - {e_str}</code> (⏱️ {dur_str})\n"

    caption = (
        f"🎬 <b>{t_clean}</b>\n"
        f"👤 <i>{a_clean}</i>\n"
        f"{clip_line}"
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
        elif file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            sent_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=reader,
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
            c_type = "video"
            if is_audio and 'sent_msg' in locals() and sent_msg.audio:
                f_id = sent_msg.audio.file_id
                c_type = "audio"
            elif 'sent_msg' in locals() and sent_msg.photo:
                f_id = sent_msg.photo[-1].file_id
                c_type = "photo"
            elif 'sent_msg' in locals() and sent_msg.video:
                f_id = sent_msg.video.file_id
                c_type = "video"

            if f_id:
                cached_info = MEDIA_CACHE.get(cache_id, {})
                c_url = cached_info.get('url', '')
                if clip_range:
                    s_sec, e_sec = clip_range
                    cache_key = f"{c_url}_clip_{int(s_sec)}_{int(e_sec)}"
                else:
                    cache_key = f"{c_url}_{quality}"
                meta = probe_video_metadata(file_path) if (not is_audio and c_type == "video") else {}
                FILE_CACHE[cache_key] = {
                    "file_id": f_id,
                    "type": c_type,
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
        cached_item = MEDIA_CACHE.get(cache_id, {})
        cached_item["chat_id"] = update.effective_chat.id
        cached_item["status_msg_id"] = status_msg.message_id
        cached_item["size_mb"] = file_size_mb
        MEDIA_CACHE[cache_id] = cached_item
        save_cache(MEDIA_CACHE)
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
    global GLOBAL_BOT
    GLOBAL_BOT = application.bot
    await start_web_server()
    try:
        commands = [
            BotCommand("start", "Welcome & feature overview"),
            BotCommand("help", "Command guide & group usage"),
            BotCommand("clip", "Clip/trim video by timestamps"),
            BotCommand("contact", "Contact developer (@silas_inegbe)"),
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

    global GLOBAL_BOT
    GLOBAL_BOT = application.bot

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clip", clip_command))
    application.add_handler(CommandHandler("trim", clip_command))
    application.add_handler(CommandHandler("contact", contact_command))
    application.add_handler(CommandHandler("developer", contact_command))
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
