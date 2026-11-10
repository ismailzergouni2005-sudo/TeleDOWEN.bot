import os
import re
import sys
import json
import html
import time
import shutil
import logging
import asyncio
import threading
import subprocess
import urllib.request
from urllib.parse import urlparse
from http.cookiejar import MozillaCookieJar

# --- تفعيل مكتبة static-ffmpeg تلقائياً ---
import static_ffmpeg
static_ffmpeg.add_paths()
# ----------------------------------------

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ لم يتم تعيين متغير البيئة BOT_TOKEN.")

# ---------- تجهيز ملف الكوكيز (يُطبق على كل المنصات) ----------
COOKIES_FILE = "/tmp/site_cookies.txt"
_secret_paths = ["/etc/secrets/cookies.txt", "cookies.txt"]

def normalize_cookies(data: str) -> str:
    """إصلاح تنسيق الكوكيز: صيغة Netscape تتطلب 7 أعمدة مفصولة بـ Tab."""
    out = ["# Netscape HTTP Cookie File"]
    for line in data.replace("\r", "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# Netscape") or s.startswith("# HTTP Cookie"):
            continue
        if s.startswith("#") and not s.startswith("#HttpOnly_"):
            continue
        if "\t" in s and len(s.split("\t")) >= 7:
            out.append(s)
            continue
        parts = s.split(None, 6)
        if len(parts) == 7:
            out.append("\t".join(parts))
    return "\n".join(out) + "\n"

def prepare_cookies():
    raw = None
    source = None
    env_data = os.environ.get("SITE_COOKIES") or os.environ.get("YT_COOKIES")
    if env_data and env_data.strip():
        raw, source = env_data, "متغير البيئة"
    else:
        for p in _secret_paths:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        raw, source = f.read(), p
                    break
                except Exception as e:
                    logging.error(f"تعذر قراءة {p}: {e}")
    if not raw:
        logging.info("ℹ️ لا يوجد ملف كوكيز.")
        return

    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(normalize_cookies(raw))
        logging.info(f"✅ تم تجهيز الكوكيز من: {source}")
    except Exception as e:
        logging.error(f"فشل تجهيز ملف الكوكيز: {e}")
        return

    try:
        jar = MozillaCookieJar(COOKIES_FILE)
        jar.load(ignore_discard=True, ignore_expires=True)
        domains = {c.domain for c in jar}
        names = {(c.domain, c.name) for c in jar}
        has_ig = any("instagram.com" in d for d in domains)
        has_ig_session = any("instagram.com" in d and n == "sessionid" for d, n in names)
        has_fb = any("facebook.com" in d for d in domains)
        has_yt = any("youtube.com" in d or "google.com" in d for d in domains)
        logging.info(
            f"🍪 عدد الكوكيز: {len(list(jar))} | instagram: {has_ig} | "
            f"sessionid: {has_ig_session} | facebook: {has_fb} | youtube: {has_yt}"
        )
        if has_ig and not has_ig_session:
            logging.warning("⚠️ كوكيز إنستغرام لا تحتوي sessionid (الحساب غير مسجل دخول).")
        if not has_ig:
            logging.warning("⚠️ لا توجد كوكيز إنستغرام في الملف.")
        if not has_yt:
            logging.warning("⚠️ لا توجد كوكيز يوتيوب في الملف (قد يطلب يوتيوب التحقق من أنك لست روبوتاً).")
    except Exception as e:
        logging.error(f"⚠️ ملف الكوكيز تالف وسيتم تجاهله: {e}")
        try:
            os.remove(COOKIES_FILE)
        except Exception:
            pass

prepare_cookies()
logging.info(f"🔧 deno: {shutil.which('deno')} | yt-dlp: {yt_dlp.version.__version__}")
# ---------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL")

WELCOME_IMAGE_URL = "https://files.catbox.moe/heevbw.jpg"
WELCOME_STICKER_ID = "CAACAgIAAxkBAAEtNrJqciCsb_KyhKNta-pPJzCKUefSigACVAADQbVWDGq3-McIjQH6PQQ"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = None

SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# حد تيليغرام للبوتات 50MB، نترك هامش أمان
MAX_BYTES = 48 * 1024 * 1024

# ---------------- النصوص متعددة اللغات (HTML) ----------------

TEXTS = {
    "ar": {
        "welcome": (
            "✨ أهلاً وسهلاً بك يا ✦ {user_link} ✦\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote><b> ⚡ مرحباً بك في بوت التحميل السريع! ⚡ </b></blockquote>\n\n"
            "أنا هنا لمساعدتك في تحميل الفيديوهات والمقاطع الصوتية بأعلى جودة ممكنة.\n\n"
            "🌐 <b><u>المنصات المدعومة حالياً:</u></b>\n\n"
            "<code> ▌ Y O U T U B E </code>\n"
            "<code> ▌ T I K T O K </code>\n"
            "<code> ▌ I N S T A G R A M </code>\n"
            "<code> ▌ P I N T E R E S T </code>\n"
            "<code> ▌ F A C E B O O K </code>\n"
            "<code> ▌ X ( T W I T T E R ) </code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <i>كل ما عليك هو إرسال رابط الفيديو الآن!</i>"
        ),
        "invalid_url": "❌ يرجى إرسال رابط صحيح.",
        "ig_story_login": "🔒 ستوريات إنستغرام تتطلب تسجيل دخول ولا يمكن تحميلها حالياً.",
        "checking": "🔍 جاري فحص الرابط والجودات...",
        "choose_format": "👇 <b>اختر الصيغة والجودة المطلوبة:</b>",
        "timeout": "⏱ استغرق فحص الرابط وقتاً طويلاً. أعد المحاولة لاحقاً.",
        "analyze_error": "❌ تعذر تحليل الرابط:\n<code>{error}</code>",
        "err_login": "🔒 المنصة رفضت الطلب مؤقتاً (تتطلب تسجيل دخول). حاول مجدداً بعد قليل، وإن تكرر الأمر فسيقوم المشرف بتحديث الجلسة.",
        "err_yt_bot": "🤖 يوتيوب طلب التحقق من أنك لست روبوتاً. سيقوم المشرف بتحديث الكوكيز، حاول لاحقاً.",
        "err_private": "🔒 هذا المحتوى خاص ولا يمكن تحميله.",
        "err_no_video": "🖼 لا يوجد فيديو في هذا الرابط (قد يكون منشور صور فقط) أو أن الرابط غير مدعوم.",
        "err_unavailable": "🚫 هذا المحتوى غير متاح أو تم حذفه.",
        "account": "👤 الحساب",
        "unknown": "غير معروف",
        "description": "📝 الوصف",
        "duration": "⏱ المدة",
        "views": "👁 المشاهدات",
        "quality": "🎬 الجودة/الصيغة",
        "best_quality": "أفضل جودة متاحة",
        "audio_only": "🎵 الصوت فقط",
        "instant": "⚡ أفضل جودة مباشرة",
        "cancel": "❌ إلغاء",
        "cancel_download": "🛑 إلغاء التحميل",
        "reselect": "🔄 اختيار صيغة أخرى",
        "cancelled": "🛑 تم إلغاء عملية التحميل.",
        "expired": "❌ انتهت الجلسة، أرسل الرابط مجدداً.",
        "downloading": "⏳ جاري بدء التحميل...",
        "processing": "جاري التحميل والمعالجة...",
        "downloaded_mb": "تم تحميل {mb:.1f} MB...",
        "too_large": "❌ <b>حجم الملف كبير جداً ({size})</b>.\nحد التحميل المسموح للبوتات هو 50MB.",
        "success": "✅ تم التحميل بنجاح!",
        "size": "💾 الحجم",
        "download_error": "❌ <b>حدث خطأ أثناء التحميل:</b>\n<code>{error}</code>"
    },
    "en": {
        "welcome": (
            "✨ Welcome ✦ {user_link} ✦\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote><b> ⚡ Welcome to Fast Downloader Bot! ⚡ </b></blockquote>\n\n"
            "I'm here to help you download videos and audio clips in the highest possible quality.\n\n"
            "🌐 <b><u>Currently Supported Platforms:</u></b>\n\n"
            "<code> ▌ Y O U T U B E </code>\n"
            "<code> ▌ T I K T O K </code>\n"
            "<code> ▌ I N S T A G R A M </code>\n"
            "<code> ▌ P I N T E R E S T </code>\n"
            "<code> ▌ F A C E B O O K </code>\n"
            "<code> ▌ X ( T W I T T E R ) </code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <i>Just send any video link now!</i>"
        ),
        "invalid_url": "❌ Please send a valid link.",
        "ig_story_login": "🔒 Instagram stories require login and can't be downloaded right now.",
        "checking": "🔍 Checking link and qualities...",
        "choose_format": "👇 <b>Select the desired format and quality:</b>",
        "timeout": "⏱ Checking took too long. Please try again later.",
        "analyze_error": "❌ Failed to analyze link:\n<code>{error}</code>",
        "err_login": "🔒 The platform temporarily rejected the request (login required). Try again shortly; if it persists, the admin will refresh the session.",
        "err_yt_bot": "🤖 YouTube asked to verify you're not a bot. The admin will refresh cookies, try later.",
        "err_private": "🔒 This content is private and cannot be downloaded.",
        "err_no_video": "🖼 No video found in this link (it may be an image-only post) or the link is unsupported.",
        "err_unavailable": "🚫 This content is unavailable or has been removed.",
        "account": "👤 Account",
        "unknown": "Unknown",
        "description": "📝 Description",
        "duration": "⏱ Duration",
        "views": "👁 Views",
        "quality": "🎬 Quality/Format",
        "best_quality": "Best quality available",
        "audio_only": "🎵 Audio Only",
        "instant": "⚡ Best Quality Direct",
        "cancel": "❌ Cancel",
        "cancel_download": "🛑 Cancel Download",
        "reselect": "🔄 Choose Another Format",
        "cancelled": "🛑 Download process cancelled.",
        "expired": "❌ Session expired, please send the link again.",
        "downloading": "⏳ Starting download...",
        "processing": "Downloading & processing...",
        "downloaded_mb": "Downloaded {mb:.1f} MB...",
        "too_large": "❌ <b>File size too large ({size})</b>.\nTelegram bot limit is 50MB.",
        "success": "✅ Downloaded successfully!",
        "size": "💾 Size",
        "download_error": "❌ <b>An error occurred during download:</b>\n<code>{error}</code>"
    }
}

# ---------------- أدوات الروابط ----------------

YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com")
SHORT_HOSTS = {"pin.it", "vm.tiktok.com", "vt.tiktok.com", "fb.watch"}

def get_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host

def is_youtube(url: str) -> bool:
    host = get_host(url)
    return any(host == d or host.endswith("." + d) for d in YOUTUBE_DOMAINS)

def is_instagram(url: str) -> bool:
    host = get_host(url)
    return host == "instagram.com" or host.endswith(".instagram.com")

def needs_resolve(url: str) -> bool:
    host = get_host(url)
    path = urlparse(url).path or ""
    if host in SHORT_HOSTS:
        return True
    if host.endswith("facebook.com") and path.startswith("/share/"):
        return True
    if host.endswith("instagram.com") and path.startswith("/share/"):
        return True
    if host.endswith("tiktok.com") and path.startswith("/t/"):
        return True
    return False

def resolve_url(url: str) -> str:
    """فك الروابط المختصرة وروابط المشاركة للحصول على الرابط الحقيقي."""
    if not needs_resolve(url):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            final = r.geturl()
        if "/login" in final or "login.php" in final or "accounts/login" in final:
            return url
        return final
    except Exception as e:
        logging.warning(f"تعذر فك الرابط المختصر: {str(e)[:100]}")
        return url

def clean_url(url: str) -> str:
    if "instagram.com/stories/" in url:
        return url.split("?")[0]
    if "instagram.com" in url:
        match = re.search(
            r'https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)',
            url
        )
        if match:
            kind = "reel" if match.group(1).startswith("reel") else match.group(1)
            return f"https://www.instagram.com/{kind}/{match.group(2)}/"
    return url

def clean_err(msg: str) -> str:
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(msg))
    msg = re.sub(r'^\s*ERROR:\s*', '', msg.strip())
    return msg

def friendly_error(lang, e):
    t = TEXTS[lang]
    low = clean_err(str(e)).lower()
    if "no video" in low or "no video formats" in low or "unsupported url" in low:
        return t["err_no_video"]
    if "private" in low:
        return t["err_private"]
    if "not a bot" in low or "confirm you" in low:
        return t["err_yt_bot"]
    if any(k in low for k in ("login", "log in", "cookies", "rate-limit", "rate limit", "empty media response", "sign in", "checkpoint", "authentication")):
        return t["err_login"]
    if any(k in low for k in ("not available", "unavailable", "removed", "404", "does not exist", "deleted")):
        return t["err_unavailable"]
    return None

# ---------------- مسار بديل لإنستغرام (بدون تسجيل دخول) ----------------

def _parse_ig(url):
    m = re.search(r'instagram\.com/(?:[A-Za-z0-9_.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)', url)
    if not m:
        return None, None
    kind = "reel" if m.group(1).startswith("reel") else m.group(1)
    return kind, m.group(2)

def _http_get_text(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")

def _decode_json_string(s):
    s = s.replace("\\\\/", "/").replace("\\/", "/")
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except Exception:
        return s.replace("\\u0026", "&").replace("&amp;", "&")

def instagram_fallback(url):
    """محاولة جلب رابط الفيديو المباشر عبر صفحة التضمين أو خدمة وسيطة."""
    kind, code = _parse_ig(url)
    if not code:
        return None

    result = {"video_url": None, "uploader": None, "title": None, "code": code}

    # 1) صفحة التضمين (embed)
    try:
        body = _http_get_text(f"https://www.instagram.com/{kind}/{code}/embed/captioned/")
        body = html.unescape(body)
        for pattern in (r'\\"video_url\\":\\"(.*?)\\"', r'"video_url":"(.*?)"'):
            m = re.search(pattern, body)
            if m:
                result["video_url"] = _decode_json_string(m.group(1))
                break
        mu = re.search(r'\\?"username\\?":\\?"([A-Za-z0-9_.]+)', body)
        if mu:
            result["uploader"] = mu.group(1)
        mc = re.search(r'class="Caption"[^>]*>.*?<div class="CaptionContent">(.*?)</div>', body, re.S)
        if mc:
            result["title"] = re.sub(r'<[^>]+>', ' ', mc.group(1)).strip()[:200]
    except Exception as e:
        logging.warning(f"فشل مسار embed: {str(e)[:100]}")

    # 2) خدمة وسيطة
    if not result["video_url"]:
        try:
            req = urllib.request.Request(
                f"https://d.ddinstagram.com/{kind}/{code}",
                headers={"User-Agent": BROWSER_UA}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.headers.get("Content-Type", "").startswith("video"):
                    result["video_url"] = r.geturl()
        except Exception as e:
            logging.warning(f"فشل المسار الوسيط: {str(e)[:100]}")

    return result if result["video_url"] else None

def download_direct(video_url, dest_template, state, cancel_event, mode, meta=None):
    """تحميل ملف مباشر مع تحديث التقدم، وتحويله لـ mp3 عند الحاجة."""
    code = (meta or {}).get("code", "ig")
    name = dest_template.replace("%(id)s", code).replace("%(ext)s", "mp4")
    req = urllib.request.Request(video_url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r, open(name, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel_event.is_set():
                raise Exception("CANCELLED_BY_USER")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            state["downloaded"] = done
            if total:
                state["percent"] = done / total * 100

    entry = {"id": code}
    if mode == "mp3":
        out = name[:-4] + ".mp3"
        subprocess.run(
            [FFMPEG_PATH or "ffmpeg", "-y", "-i", name, "-vn", "-acodec", "libmp3lame", "-b:a", "192k", out],
            check=True, capture_output=True
        )
        try:
            os.remove(name)
        except Exception:
            pass
        return out, entry
    return name, entry

# ---------------- أدوات التنسيق والواجهة ----------------

def format_file_size(filepath):
    if os.path.exists(filepath):
        size_bytes = os.path.getsize(filepath)
        size_mb = size_bytes / (1024 * 1024)
        return size_mb, f"{size_mb:.1f} MB"
    return 0, None

def build_progress_bar(percent, length=12):
    percent = max(0, min(100, percent))
    filled = int(length * percent / 100)
    return "[" + "■" * filled + "□" * (length - filled) + f"] {percent:.0f}%"

def format_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def format_count(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def build_meta_caption(lang, uploader=None, uploader_url=None, duration=None, views=None, title=None, description=None, quality=None):
    t = TEXTS[lang]
    lines = []

    safe_uploader = html.escape(str(uploader)) if uploader else None
    if uploader_url and str(uploader_url).startswith("http"):
        href = html.escape(str(uploader_url), quote=True)
        lines.append(f"{t['account']}: <a href=\"{href}\">{safe_uploader or t['unknown']}</a>")
    elif safe_uploader:
        lines.append(f"{t['account']}: <b>{safe_uploader}</b>")
    else:
        lines.append(f"{t['account']}: {t['unknown']}")

    text_content = title or description
    if text_content:
        clean_text = text_content.strip().split('\n')[0]
        short_text = clean_text if len(clean_text) <= 80 else clean_text[:77] + "..."
        lines.append(f"{t['description']}: {html.escape(short_text)}")

    formatted_duration = format_duration(duration)
    if formatted_duration:
        lines.append(f"{t['duration']}: {formatted_duration}")

    formatted_views = format_count(views)
    if formatted_views and views != 0:
        lines.append(f"{t['views']}: {formatted_views}")

    if quality:
        lines.append(f"{t['quality']}: {html.escape(str(quality))}")

    return "\n".join(lines)

# ---------------- تجميع خيارات الجودة ----------------

def get_available_options(info):
    formats = info.get("formats") or []
    candidates = {}
    audio_size = 0

    # أكبر ملف صوتي فقط (لحساب الحجم التقريبي بعد الدمج)
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            s = f.get("filesize") or f.get("filesize_approx") or 0
            audio_size = max(audio_size, s)

    for f in formats:
        h = f.get("height")
        if not h and f.get("resolution"):
            res_match = re.search(r'\d+x(\d+)', str(f.get("resolution")))
            if res_match:
                h = int(res_match.group(1))

        vcodec = f.get("vcodec")
        ext = f.get("ext", "mp4")

        if not h or vcodec == "none":
            continue

        try:
            h = int(h)
        except (TypeError, ValueError):
            continue

        # الأفضلية: h264 ثم mp4 ثم الباقي
        rank = 2 if str(vcodec).startswith(("avc", "h264")) else (1 if ext == "mp4" else 0)
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size and f.get("acodec") in (None, "none"):
            size += audio_size

        prev = candidates.get(h)
        if prev is None or rank > prev["rank"]:
            candidates[h] = {"ext": ext, "rank": rank, "size": size}

    if not candidates:
        # لا تعرض جودات افتراضية غير موجودة فعلياً
        height_from_info = info.get("height")
        if height_from_info:
            try:
                candidates[int(height_from_info)] = {
                    "ext": str(info.get("ext") or "mp4"), "rank": 0, "size": 0
                }
            except (TypeError, ValueError):
                pass

    return candidates

def visible_heights(video_opts):
    """الجودات التي لا تتجاوز حد 50MB (أو أصغر جودة إن لم تتوفر أي واحدة)."""
    heights = sorted(video_opts.keys(), reverse=True)
    fit = [h for h in heights if not video_opts[h].get("size") or video_opts[h]["size"] <= MAX_BYTES]
    return (fit or heights[-1:])[:6]

def build_quality_keyboard(lang, video_opts):
    t = TEXTS[lang]
    rows = []
    heights = visible_heights(video_opts)

    buttons = []
    for h in heights:
        opt = video_opts[h]
        ext = str(opt["ext"]).upper()
        size_txt = f" • {opt['size'] / (1024 * 1024):.0f}MB" if opt.get("size") else ""
        buttons.append(InlineKeyboardButton(f"🎬 {h}p ({ext}){size_txt}", callback_data=f"q_{h}"))

    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])

    rows.append([InlineKeyboardButton(t["audio_only"], callback_data="a_mp3")])
    rows.append([InlineKeyboardButton(t["instant"], callback_data="v_instant")])
    rows.append([InlineKeyboardButton(t["cancel"], callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)

def build_cancel_keyboard(lang):
    t = TEXTS[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["cancel_download"], callback_data="cancel_active_task")]
    ])

def build_reselect_keyboard(lang):
    t = TEXTS[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["reselect"], callback_data="reselect_format")]
    ])

# ---------------- خيارات yt-dlp ----------------

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "heic"}

def find_js_runtimes():
    """يبحث عن deno/node/bun في PATH وفي مجلد بيئة بايثون (.venv/bin) ويمررها لـ yt-dlp."""
    found = {}
    venv_bin = os.path.dirname(sys.executable)
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if not path:
            cand = os.path.join(venv_bin, name)
            if os.path.exists(cand):
                path = cand
        if path:
            found[name] = {"path": path}
    return found

JS_RUNTIMES = find_js_runtimes()
logging.info(f"🔧 JS runtimes: {list(JS_RUNTIMES.keys()) or 'لا يوجد'} | {JS_RUNTIMES}")

def get_base_opts(url, use_cookies=True):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "socket_timeout": 20,
        "retries": 8,
        "fragment_retries": 8,
        "file_access_retries": 3,
        "extractor_retries": 5,
        "skip_unavailable_fragments": True,
        "geo_bypass": True,
        "noplaylist": True,
        "playlist_items": "1",
        "http_headers": {"User-Agent": BROWSER_UA},
    }
    if use_cookies and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
    if is_youtube(url):
        if JS_RUNTIMES:
            ydl_opts["js_runtimes"] = JS_RUNTIMES
        # تنزيل سكربت حل التحدي من GitHub إن لم تكن حزمة yt-dlp-ejs مثبتة
        ydl_opts["remote_components"] = ["ejs:github"]
    return ydl_opts

def cookie_attempts():
    """المحاولة الأولى بالكوكيز، والثانية بدونها."""
    return [True, False] if os.path.exists(COOKIES_FILE) else [False]

def pick_entry(info):
    """في المنشورات المتعددة (تغريدات/كاروسيل) نأخذ أول عنصر."""
    entries = info.get("entries")
    if entries:
        for e in entries:
            if e:
                return e
    return info

def build_video_format(height=None):
    h = f"[height<={height}]" if height else ""
    return (
        f"bv*{h}[vcodec~='^(h264|avc)']+ba[ext=m4a]"
        f"/bv*{h}[ext=mp4]+ba[ext=m4a]"
        f"/bv*{h}+ba"
        f"/b{h}[ext=mp4]"
        f"/b{h}"
        f"/best"
    )

class _CaptureLogger:
    """يلتقط رسائل yt-dlp (تحذيرات/أخطاء) لمعرفة السبب الحقيقي عند غياب الصيغ."""
    def __init__(self):
        self.msgs = []
    def debug(self, msg):
        pass
    def info(self, msg):
        pass
    def warning(self, msg):
        self.msgs.append(clean_err(msg))
    def error(self, msg):
        self.msgs.append(clean_err(msg))

def _no_formats_error(cap):
    reasons = [m for m in cap.msgs if m and not m.startswith("[")] or cap.msgs
    detail = " | ".join(reasons[-2:]).strip()
    return Exception(detail or "No video formats found")

def extract_info_only(url):
    last_err = None
    for use_cookies in cookie_attempts():
        cap = _CaptureLogger()
        ydl_opts = get_base_opts(url, use_cookies)
        ydl_opts["skip_download"] = True
        ydl_opts["ignore_no_formats_error"] = True
        ydl_opts["format"] = "all"
        ydl_opts["no_warnings"] = False
        ydl_opts["logger"] = cap
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                raise _no_formats_error(cap)

            entry = pick_entry(info)
            if entry is not info:
                for k in ("uploader", "uploader_url", "channel", "channel_url"):
                    if not entry.get(k):
                        entry[k] = info.get(k)

            has_formats = bool(entry.get("formats"))
            has_direct = bool(entry.get("url")) and str(entry.get("ext", "")).lower() not in IMAGE_EXTS
            if not has_formats and not has_direct:
                raise _no_formats_error(cap)

            return entry
        except Exception as e:
            last_err = e
            logging.warning(f"فشل الفحص (cookies={use_cookies}): {clean_err(str(e))[:200]}")

    # مسار بديل لإنستغرام
    if is_instagram(url):
        fb = instagram_fallback(url)
        if fb:
            logging.info("✅ نجح المسار البديل لإنستغرام (الفحص).")
            uname = fb.get("uploader")
            return {
                "id": fb["code"],
                "title": fb.get("title"),
                "uploader": uname,
                "uploader_url": f"https://www.instagram.com/{uname}/" if uname else None,
                "formats": [{"height": 720, "vcodec": "avc1", "ext": "mp4"}],
            }
    raise last_err

def resolve_downloaded_file(ydl, info, mode):
    entry = pick_entry(info)

    paths = []
    for src in (entry, info):
        for rd in (src.get("requested_downloads") or []):
            fp = rd.get("filepath")
            if fp:
                paths.append(fp)
    for fp in paths:
        if os.path.exists(fp):
            return fp, entry

    try:
        filename = ydl.prepare_filename(entry)
    except Exception:
        filename = None

    if filename:
        base, _ = os.path.splitext(filename)
        exts = [".mp3"] if mode == "mp3" else [".mp4", ".mkv", ".webm"]
        for cand in [filename] + [base + e for e in exts]:
            if os.path.exists(cand):
                return cand, entry
        directory = os.path.dirname(base) or "."
        prefix = os.path.basename(base)
        for fname in os.listdir(directory):
            if fname.startswith(prefix) and not fname.endswith((".part", ".ytdl")):
                return os.path.join(directory, fname), entry

    raise FileNotFoundError("لم يتم العثور على الملف بعد التحميل")

def yt_dlp_download_one_pass(url, dest_template, state, cancel_event, mode="video", height=None):
    def hook(d):
        if cancel_event.is_set():
            raise Exception("CANCELLED_BY_USER")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            state["downloaded"] = downloaded
            if total:
                state["percent"] = downloaded / total * 100

    last_err = None
    for use_cookies in cookie_attempts():
        if cancel_event.is_set():
            raise Exception("CANCELLED_BY_USER")

        ydl_opts = get_base_opts(url, use_cookies)
        ydl_opts["outtmpl"] = dest_template
        ydl_opts["progress_hooks"] = [hook]

        if mode == "mp3":
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            ydl_opts["format"] = build_video_format(height)
            ydl_opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise Exception("No video formats found")
                return resolve_downloaded_file(ydl, info, mode)
        except Exception as e:
            if "CANCELLED_BY_USER" in str(e):
                raise
            last_err = e
            logging.warning(f"فشل التحميل (cookies={use_cookies}): {clean_err(str(e))[:200]}")

    # مسار بديل لإنستغرام
    if is_instagram(url):
        fb = instagram_fallback(url)
        if fb:
            logging.info("✅ نجح المسار البديل لإنستغرام (التحميل).")
            return download_direct(fb["video_url"], dest_template, state, cancel_event, mode, fb)
    raise last_err

def cleanup_partial_files(token):
    """حذف أي ملفات مؤقتة/متبقية تخص رسالة معينة."""
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            if token in fname:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, fname))
                except Exception:
                    pass
    except Exception:
        pass

# ---------------- إدارة البث والتحديث ----------------

async def edit_progress_message(status_msg, text, reply_markup=None):
    try:
        await status_msg.edit_caption(caption=text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception:
        try:
            await status_msg.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        except Exception:
            pass

async def progress_ticker(status_msg, meta_caption, state, cancel_event, lang):
    t = TEXTS[lang]
    frame = 0
    last_text = None
    last_update = 0
    try:
        while not cancel_event.is_set():
            frame = (frame + 1) % len(SPINNER_FRAMES)
            percent = state.get("percent")
            if percent is not None:
                body = build_progress_bar(percent)
            else:
                mb = (state.get("downloaded") or 0) / (1024 * 1024)
                body = t["downloaded_mb"].format(mb=mb)

            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} {t['processing']}\n{body}"

            now = time.time()
            if text != last_text and (now - last_update) >= 1.5:
                last_text = text
                last_update = now
                await edit_progress_message(status_msg, text, reply_markup=build_cancel_keyboard(lang))

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass

async def run_with_progress(func, args, status_msg, meta_caption, state, cancel_event, lang, timeout=420):
    loop = asyncio.get_running_loop()
    ticker = asyncio.create_task(progress_ticker(status_msg, meta_caption, state, cancel_event, lang))
    start_time = time.time()
    try:
        download_task = loop.run_in_executor(None, func, *args)
        while not download_task.done():
            if cancel_event.is_set():
                download_task.cancel()
                raise asyncio.CancelledError("Cancelled by user.")
            if time.time() - start_time > timeout:
                cancel_event.set()
                raise asyncio.TimeoutError("Download timeout.")
            await asyncio.sleep(0.2)
        return await download_task
    finally:
        ticker.cancel()
        try:
            await ticker
        except Exception:
            pass

async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False, entry=None):
    t = TEXTS[lang]
    size_mb, file_size_str = format_file_size(filepath)

    if size_mb > 50:
        await edit_progress_message(
            status_msg,
            f"{meta_caption}\n\n{t['too_large'].format(size=file_size_str)}",
            reply_markup=build_reselect_keyboard(lang)
        )
        if os.path.exists(filepath):
            os.remove(filepath)
        return

    size_line = f"\n{t['size']}: {file_size_str}" if file_size_str else ""
    caption = f"{meta_caption}{size_line}\n\n{t['success']}"
    entry = entry or {}

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(lang),
                    duration=_int(entry.get("duration")),
                    read_timeout=120, write_timeout=120
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(lang),
                    supports_streaming=True,
                    width=_int(entry.get("width")), height=_int(entry.get("height")),
                    duration=_int(entry.get("duration")),
                    read_timeout=120, write_timeout=120
                )
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
        try:
            await status_msg.delete()
        except Exception:
            pass

# ---------------- اختيار اللغة والأوامر الترحيبية ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇩🇿 العربية", callback_data="lang_ar"),
            InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")
        ]
    ])
    await update.message.reply_text(
        "🌐 <b>الرجاء اختيار اللغة / Please select your language:</b>",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def send_welcome(query, context: ContextTypes.DEFAULT_TYPE, lang: str):
    t = TEXTS[lang]
    user = query.from_user
    name = html.escape(user.first_name or "User")
    user_id = user.id

    blue_user_link = f'<a href="tg://user?id={user_id}">« {name} »</a>'
    welcome_text = t["welcome"].format(user_link=blue_user_link)

    try:
        await query.message.reply_photo(
            photo=WELCOME_IMAGE_URL,
            caption=welcome_text,
            parse_mode='HTML'
        )
    except Exception as e:
        logging.error(f"Error sending photo: {e}")
        await query.message.reply_text(
            text=welcome_text,
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    try:
        await query.message.reply_sticker(sticker=WELCOME_STICKER_ID)
    except Exception as e:
        logging.error(f"Error sending sticker: {e}")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ar")
    t = TEXTS[lang]

    text = (update.message.text or "").strip()
    match = re.search(r'https?://[^\s]+', text)
    if not match:
        await update.message.reply_text(t["invalid_url"])
        return

    raw_url = match.group(0).rstrip(".,;:!?)»”\"'")

    checking_msg = await update.message.reply_text(t["checking"])
    try:
        loop = asyncio.get_running_loop()

        url = await loop.run_in_executor(None, resolve_url, raw_url)
        url = clean_url(url)

        # ستوريات إنستغرام تتطلب تسجيل دخول دائماً
        if is_instagram(url) and "/stories/" in url and not os.path.exists(COOKIES_FILE):
            await checking_msg.edit_text(t["ig_story_login"])
            return

        context.user_data['download_url'] = url

        info = await asyncio.wait_for(
            loop.run_in_executor(None, extract_info_only, url),
            timeout=90
        )
        context.user_data['ytdlp_info'] = info
        video_opts = get_available_options(info)
        context.user_data['video_opts'] = video_opts

        await checking_msg.edit_text(
            t["choose_format"],
            reply_markup=build_quality_keyboard(lang, video_opts),
            parse_mode='HTML'
        )
    except asyncio.TimeoutError:
        await checking_msg.edit_text(t["timeout"])
    except Exception as e:
        logging.warning(f"فشل تحليل الرابط: {clean_err(str(e))[:300]}")
        friendly = friendly_error(lang, e)
        if is_youtube(raw_url):
            msg = t["analyze_error"].format(error=html.escape(clean_err(str(e))[:400]))
        else:
            msg = friendly or t["analyze_error"].format(error=html.escape(clean_err(str(e))[:150]))
        try:
            await checking_msg.edit_text(msg, parse_mode='HTML')
        except Exception:
            pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ["lang_ar", "lang_en"]:
        lang = "ar" if query.data == "lang_ar" else "en"
        context.user_data["lang"] = lang
        await query.delete_message()
        await send_welcome(query, context, lang)
        return

    lang = context.user_data.get("lang", "ar")
    t = TEXTS[lang]

    if query.data == "action_cancel":
        await query.delete_message()
        return

    if query.data == "cancel_active_task":
        cancel_event = context.user_data.get("active_cancel_event")
        if cancel_event:
            cancel_event.set()
        try:
            await query.edit_message_text(t["cancelled"])
        except Exception:
            pass
        return

    if query.data == "reselect_format":
        video_opts = context.user_data.get('video_opts')
        if not video_opts or not context.user_data.get('download_url'):
            await query.message.reply_text(t["expired"])
            return
        await query.message.reply_text(
            t["choose_format"],
            reply_markup=build_quality_keyboard(lang, video_opts),
            parse_mode='HTML'
        )
        return

    url = context.user_data.get('download_url')
    if not url:
        await query.edit_message_text(t["expired"])
        return

    info = context.user_data.get('ytdlp_info') or {}

    quality_str = None
    if query.data.startswith("q_"):
        quality_str = f"{query.data.split('_')[1]}p"
    elif query.data == "a_mp3":
        quality_str = t["audio_only"]
    elif query.data == "v_instant":
        quality_str = t["best_quality"]

    meta_caption = build_meta_caption(
        lang=lang,
        uploader=info.get("uploader") or info.get("channel"),
        uploader_url=info.get("uploader_url") or info.get("channel_url"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        title=info.get("title"),
        description=info.get("description"),
        quality=quality_str
    )

    # يُقرأ هذا الحدث من خيط التنزيل ومن حلقة asyncio؛ لذلك نستخدم
    # threading.Event بدلاً من asyncio.Event لتكون مشاركة الحالة آمنة بين الخيوط.
    cancel_event = threading.Event()
    context.user_data["active_cancel_event"] = cancel_event

    status_msg = await query.edit_message_text(
        f"{meta_caption}\n\n{t['downloading']}",
        parse_mode='HTML',
        reply_markup=build_cancel_keyboard(lang)
    )

    chat_id = query.message.chat_id
    bot = context.bot
    token = f"_{status_msg.message_id}."
    dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
    state = {"percent": None, "downloaded": 0}

    try:
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            filepath, entry = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", height),
                status_msg, meta_caption, state, cancel_event, lang
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False, entry=entry)

        elif query.data.startswith("a_"):
            mode = query.data.split("_")[1]
            filepath, entry = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, mode, None),
                status_msg, meta_caption, state, cancel_event, lang
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=True, entry=entry)

        elif query.data == "v_instant":
            # أعلى جودة لا تتجاوز حد 50MB
            heights = visible_heights(context.user_data.get('video_opts') or {})
            best_h = heights[0] if heights else None
            filepath, entry = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", best_h),
                status_msg, meta_caption, state, cancel_event, lang
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False, entry=entry)

    except (asyncio.CancelledError, RuntimeError):
        pass
    except Exception as e:
        if "CANCELLED_BY_USER" in str(e):
            pass
        else:
            logging.warning(f"فشل التحميل: {clean_err(str(e))[:300]}")
            friendly = friendly_error(lang, e)
            msg = friendly or t["download_error"].format(error=html.escape(clean_err(str(e))[:150]))
            try:
                await status_msg.edit_text(msg, parse_mode='HTML', reply_markup=build_reselect_keyboard(lang))
            except Exception:
                pass
    finally:
        context.user_data.pop("active_cancel_event", None)
        cleanup_partial_files(token)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("حدث خطأ:", exc_info=context.error)

def main():
    request = HTTPXRequest(connect_timeout=15, read_timeout=100, write_timeout=100)
    application = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)

    print("🚀 البوت يعمل...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
