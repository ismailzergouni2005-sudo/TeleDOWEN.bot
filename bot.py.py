import os
import re
import time
import logging
import asyncio
import threading
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

# 🌐 سيرفر وهمي لفتح Port على Render
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

logging.basicConfig(level=logging.INFO)

# ⚠️ يفضّل وضع التوكن في متغير بيئة بدل كتابته مباشرة في الكود
TOKEN = os.environ.get("BOT_TOKEN", "8846997512:AAFfc2HSrJHWmXHfiEMO_M5I4F-OPc3zrrk")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# رموز دائرة الانتظار المتحركة
SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]

# قوائم الجودات المدعومة (تطابق الأزرار المطلوبة)
VIDEO_QUALITIES = [1080, 720, 480, 360, 240, 144]
AUDIO_BITRATES = [128, 64]

# 🍪 مسار ملف كوكيز يوتيوب (اختياري) لتجاوز رسالة "Sign in to confirm you're not a bot"
# التي تظهر أحياناً على سيرفرات الاستضافة (Render/Railway/إلخ). لإصلاحها:
# 1) ثبّت إضافة مثل "Get cookies.txt LOCALLY" على متصفحك وسجّل الدخول ليوتيوب.
# 2) صدّر كوكيز يوتيوب كملف cookies.txt وارفعه بجانب هذا الملف على السيرفر
#    (أو كـ Secret File على Render مثلاً في /etc/secrets/cookies.txt).
# 3) عيّن متغير بيئة باسم YOUTUBE_COOKIES_FILE يشير لمسار الملف الأصلي.
#
# ⚠️ ملاحظة مهمة: بعض منصات الاستضافة (مثل Render "Secret Files") تضع الملف
# في مسار للقراءة فقط (read-only). لكن yt-dlp يحاول إعادة كتابة/تحديث الكوكيز
# في نفس الملف بعد كل طلب، مما يسبب خطأ:
#   [Errno 30] Read-only file system: '/etc/secrets/cookies.txt'
# لحل هذا، ننسخ الملف عند بدء التشغيل إلى مسار قابل للكتابة داخل DOWNLOAD_DIR
# ونستخدم هذه النسخة بدل الأصل المحمي.
_RAW_YOUTUBE_COOKIES_FILE = os.environ.get("YOUTUBE_COOKIES_FILE")
YOUTUBE_COOKIES_FILE = None


def _prepare_writable_cookies_file():
    """ينسخ ملف الكوكيز (الذي قد يكون للقراءة فقط) إلى مسار قابل للكتابة
    داخل مجلد التنزيلات، حتى يستطيع yt-dlp تحديثه دون فشل."""
    global YOUTUBE_COOKIES_FILE
    if _RAW_YOUTUBE_COOKIES_FILE and os.path.exists(_RAW_YOUTUBE_COOKIES_FILE):
        writable_path = os.path.join(DOWNLOAD_DIR, "cookies.txt")
        try:
            import shutil
            shutil.copyfile(_RAW_YOUTUBE_COOKIES_FILE, writable_path)
            YOUTUBE_COOKIES_FILE = writable_path
            logging.info(f"تم نسخ ملف الكوكيز إلى مسار قابل للكتابة: {writable_path}")
        except Exception as e:
            logging.warning(f"تعذر نسخ ملف الكوكيز إلى مسار قابل للكتابة: {e}")
            YOUTUBE_COOKIES_FILE = None


_prepare_writable_cookies_file()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def base_ydl_opts():
    """خيارات yt-dlp مشتركة لكل الطلبات، تشمل محاولة تجاوز حماية يوتيوب ضد
    البوتات عبر انتحال شخصية تطبيق يوتيوب على أندرويد (لا يحتاج كوكيز غالباً)،
    مع دعم اختياري لملف كوكيز حقيقي إن توفر (أدق حل لكن يتطلب إعداداً يدوياً)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": {"User-Agent": _UA},
        "extractor_args": {
            # انتحال تطبيق أندرويد يتجاوز فحص "Sign in to confirm you're not a bot"
            # في كثير من الحالات لأن يوتيوب يطبّق الفحص بشكل أساسي على متصفح الويب
            "youtube": {"player_client": ["android", "web"]}
        },
    }
    if YOUTUBE_COOKIES_FILE and os.path.exists(YOUTUBE_COOKIES_FILE):
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    return opts


def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url


# ---------------- أدوات تنسيق الوصف وشريط التقدم ----------------

def build_progress_bar(percent, length=12):
    percent = max(0, min(100, percent))
    filled = int(length * percent / 100)
    return "[" + "■" * filled + "□" * (length - filled) + f"] {percent:.0f}%"


def format_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "غير معروف"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_size(num_bytes):
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError):
        return None
    if num_bytes <= 0:
        return None
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def get_remote_file_size(url):
    """يحاول معرفة حجم الملف عبر طلب HEAD قبل الإرسال المباشر (بدون تنزيله)،
    يُستخدم فقط لعرض الحجم في وصف الفيديو بعد نجاح الإرسال."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
        size = r.headers.get("content-length")
        return format_size(size) if size else None
    except Exception:
        return None


def format_count(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "غير معروف"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def build_meta_caption(uploader=None, duration=None, views=None, title=None):
    lines = []
    if title:
        title_short = title if len(title) <= 60 else title[:57] + "..."
        lines.append(f"📝 {title_short}")
    lines.append(f"👤 الحساب: {uploader or 'غير معروف'}")
    lines.append(f"⏱ المدة: {format_duration(duration) if duration else 'غير معروف'}")
    lines.append(f"👁 المشاهدات: {format_count(views) if views is not None else 'غير معروف'}")
    return "\n".join(lines)


# ---------------- لوحة اختيار الجودة (فيديو + صوت) ----------------

def build_quality_keyboard_generic(heights=None):
    """لوحة جودات يوتيوب/إنستغرام. إن توفرت قائمة heights حقيقية (من extract_info_only)
    نعرض زراً واحداً فقط لكل دقة موجودة فعلاً (فيديو بدقة واحدة يظهر بزر واحد فقط)،
    بدل عرض 6 أزرار وهمية دائماً."""
    rows = []
    if heights:
        capped = heights[:6]  # سقف احتياطي لتفادي لوحة طويلة جداً
        buttons = [InlineKeyboardButton(f"🎬 {h}p", callback_data=f"q_{h}") for h in capped]
        for i in range(0, len(buttons), 3):
            rows.append(buttons[i:i + 3])
    else:
        # لم نتمكن من قراءة الصيغ (نادر) — نعرض القائمة القياسية كخيار احتياطي
        for i in range(0, len(VIDEO_QUALITIES), 3):
            chunk = VIDEO_QUALITIES[i:i + 3]
            rows.append([InlineKeyboardButton(f"🎬 {q}p", callback_data=f"q_{q}") for q in chunk])

    rows.append([
        InlineKeyboardButton(f"🎵 {b}kbps", callback_data=f"a_{b}") for b in AUDIO_BITRATES
    ])
    rows.append([InlineKeyboardButton("⚡ أفضل جودة (إرسال فوري)", callback_data="v_instant")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------- الحصول على رابط الفيديو المباشر (بدون تنزيل محلي) ----------------

def get_direct_video_url(url):
    """يجلب رابط الفيديو المباشر + معلوماته، بدون تنزيل الملف على السيرفر إطلاقاً."""
    ydl_opts = {**base_ydl_opts(), "format": "b[ext=mp4]/b/best"}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        direct = info.get("url")
        if not direct and info.get("formats"):
            for f in reversed(info["formats"]):
                if f.get("url") and f.get("vcodec") != "none":
                    direct = f["url"]
                    break
        return direct, info


# ---------------- تنزيل الملف مع تتبع نسبة التقدم الحقيقية (مؤشر حي مستمر) ----------------

async def edit_progress_message(status_msg, text):
    try:
        await status_msg.edit_caption(caption=text)
    except Exception:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass


def _progress_body(state):
    stage = state.get("stage", "downloading")
    if stage == "processing":
        return "جاري معالجة الفيديو..."
    percent = state.get("percent")
    if percent is not None:
        return build_progress_bar(percent)
    mb = (state.get("downloaded") or 0) / (1024 * 1024)
    return f"تم تحميل {mb:.1f} MB..."


async def progress_ticker(status_msg, meta_caption, state):
    frame = 0
    last_text = None
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            body = _progress_body(state)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} {body}"
            if text != last_text:
                last_text = text
                await edit_progress_message(status_msg, text)
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass


async def run_with_live_progress(func, args, status_msg, meta_caption, state):
    loop = asyncio.get_running_loop()
    ticker = asyncio.create_task(progress_ticker(status_msg, meta_caption, state))
    try:
        result = await loop.run_in_executor(None, func, *args)
        return result
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass


def _locate_downloaded_file(info, ydl, audio_only):
    requested = info.get("requested_downloads") or []
    for item in requested:
        fp = item.get("filepath") or item.get("_filename")
        if fp and os.path.exists(fp):
            return fp

    expected = ydl.prepare_filename(info)
    if audio_only:
        base, _ = os.path.splitext(expected)
        expected = base + ".mp3"
    if os.path.exists(expected):
        return expected

    base_no_ext, _ = os.path.splitext(expected)
    directory = os.path.dirname(base_no_ext) or "."
    base_name = os.path.basename(base_no_ext)
    if os.path.isdir(directory):
        for fname in os.listdir(directory):
            if fname.startswith(base_name):
                return os.path.join(directory, fname)
    return None


def yt_dlp_download_with_progress(url, dest_template, state, audio_only=False, height=None, bitrate=None):
    """تنزيل حقيقي عبر yt-dlp مع تحديث حالة التقدم في state.
    height: أقصى ارتفاع فيديو مطلوب (1080/720/480/360/240/144) عند تحديد جودة معيّنة.
    bitrate: بتريت الصوت المطلوب (128/64) عند تحميل صوت فقط."""

    def hook(d):
        if d.get("status") == "downloading":
            state["stage"] = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            state["downloaded"] = downloaded
            if total:
                state["percent"] = downloaded / total * 100
            else:
                frag_idx = d.get("fragment_index")
                frag_count = d.get("fragment_count")
                if frag_idx is not None and frag_count:
                    state["percent"] = frag_idx / frag_count * 100
                else:
                    state["percent"] = None
        elif d.get("status") == "finished":
            state["stage"] = "processing"

    ydl_opts = {
        **base_ydl_opts(),
        "outtmpl": dest_template,
        "retries": 5,
        "fragment_retries": 5,
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
    }

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate) if bitrate else "192",
        }]
    elif height:
        # نطلب أفضل نسخة لا يتجاوز ارتفاعها القيمة المطلوبة، مع بدائل متسلسلة:
        # 1) ملف جاهز (فيديو+صوت) بالحد الأقصى المطلوب
        # 2) دمج فيديو+صوت بالحد الأقصى المطلوب
        # 3) أقرب جودة أعلى متاحة (بعض المصادر مثل الريلز توفر دقة واحدة فقط)
        # 4) أضعف جودة متاحة كملاذ أخير حتى لا يفشل التحميل نهائياً
        ydl_opts["format"] = (
            f"best[height<={height}][ext=mp4]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/"
            f"worst[ext=mp4]/worst/best"
        )
    else:
        ydl_opts["format"] = "b[ext=mp4]/b/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        state["stage"] = "processing"
        filepath = _locate_downloaded_file(info, ydl, audio_only)
        return filepath, info


def extract_info_only(url):
    ydl_opts = {**base_ydl_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def get_available_video_heights(info):
    """يستخرج القيم الحقيقية والمختلفة لدقة الفيديو المتوفرة فعلاً من قائمة الصيغ،
    حتى لا نعرض للمستخدم جودات وهمية غير موجودة أصلاً (مثل الريلز التي غالباً
    لا تحتوي إلا على دقة واحدة)."""
    formats = info.get("formats") or []
    heights = set()
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        if h and vcodec and vcodec != "none":
            heights.add(int(h))
    return sorted(heights, reverse=True)


# ---------------- الرسائل والأزرار ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✨ أهلاً بك! أرسل لي أي رابط وسأقوم بمعالجته فوراً 🚀")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        await update.message.reply_text("❌ يرجى إرسال رابط صحيح.")
        return

    url = clean_url(url_match.group(0))
    context.user_data['download_url'] = url
    context.user_data.pop('ytdlp_info', None)

    checking_msg = await update.message.reply_text("🔍 جاري تحليل الرابط...")
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, extract_info_only, url)
    except Exception as e:
        err_text = str(e)
        if "Sign in to confirm" in err_text or "not a bot" in err_text:
            await checking_msg.edit_text(
                "❌ يوتيوب رفض الطلب مؤقتاً (فحص مكافحة البوتات).\n"
                "هذا شائع على سيرفرات الاستضافة، ويُحل غالباً بإضافة ملف كوكيز "
                "(YOUTUBE_COOKIES_FILE) — راجع الملاحظة أعلى الكود لمعرفة كيفية إعداده."
            )
        else:
            await checking_msg.edit_text(f"❌ تعذر جلب معلومات الرابط:\n`{err_text[:150]}`", parse_mode='Markdown')
        return

    context.user_data['ytdlp_info'] = info
    heights = get_available_video_heights(info)
    await checking_msg.edit_text(
        "👇 **اختر الجودة المتاحة فعلياً:**", reply_markup=build_quality_keyboard_generic(heights),
        parse_mode='Markdown'
    )


async def send_progress_placeholder(query, thumb_url, meta_caption):
    text = f"{meta_caption}\n\n{SPINNER_FRAMES[0]} جاري التحميل...\n{build_progress_bar(0)}"
    if thumb_url:
        try:
            msg = await query.message.reply_photo(photo=thumb_url, caption=text)
            await query.delete_message()
            return msg
        except Exception:
            pass
    return await query.edit_message_text(text)


async def send_via_direct_url(bot, chat_id, status_msg, direct_url, meta_caption, thumb_url=None):
    ticker = asyncio.create_task(_sending_ticker(status_msg, meta_caption))
    try:
        # نجلب حجم الملف بالتوازي مع الإرسال حتى لا نؤخر الرفع من أجله
        size_task = asyncio.get_running_loop().run_in_executor(None, get_remote_file_size, direct_url)
        sent_message = await bot.send_video(
            chat_id=chat_id, video=direct_url, caption=f"{meta_caption}\n\n✅ تم الإرسال بنجاح!",
            supports_streaming=True,
            read_timeout=180, write_timeout=180, connect_timeout=30,
        )
        size_label = await size_task
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

    if size_label:
        caption = f"{meta_caption}\n\n✅ تم الإرسال بنجاح!\n📦 الحجم: {size_label}"
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=sent_message.message_id, caption=caption)
        except Exception:
            pass

    try:
        await status_msg.delete()
    except Exception:
        pass


async def _sending_ticker(status_msg, meta_caption):
    frame = 0
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري الإرسال..."
            await edit_progress_message(status_msg, text)
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass


async def upload_ticker(status_msg, meta_caption):
    frame = 0
    start = time.time()
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            elapsed = int(time.time() - start)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري رفع الفيديو... ({elapsed} ث)"
            await edit_progress_message(status_msg, text)
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass


async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False):
    size_label = format_size(os.path.getsize(filepath)) if os.path.exists(filepath) else None
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"
    if size_label:
        caption += f"\n📦 الحجم: {size_label}"

    ticker = asyncio.create_task(upload_ticker(status_msg, meta_caption))
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f, caption=caption,
                    read_timeout=180, write_timeout=180, connect_timeout=30,
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption, supports_streaming=True,
                    read_timeout=180, write_timeout=180, connect_timeout=30,
                )
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        if os.path.exists(filepath):
            os.remove(filepath)

    try:
        await status_msg.delete()
    except Exception:
        pass


async def handle_ytdlp_video_quality(query, context, url, height, chat_id, bot):
    info = context.user_data.get('ytdlp_info')
    if not info:
        await query.edit_message_text("🔍 جاري جلب معلومات الفيديو...")
        info = await asyncio.get_running_loop().run_in_executor(None, extract_info_only, url)
    meta_caption = build_meta_caption(
        uploader=info.get("uploader") or info.get("channel"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        title=info.get("title"),
    )
    thumb = info.get("thumbnail")

    status_msg = await send_progress_placeholder(query, thumb, meta_caption)
    dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
    state = {"stage": "downloading", "percent": None, "downloaded": 0}
    filepath, result_info = await run_with_live_progress(
        yt_dlp_download_with_progress, (url, dest_template, state, False, height, None),
        status_msg, meta_caption, state
    )

    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("تعذر العثور على الملف بهذه الجودة، جرب جودة أخرى.")

    actual_height = (result_info or {}).get("height")
    if actual_height and actual_height != height:
        meta_caption += f"\n⚠️ الجودة المطلوبة ({height}p) غير متاحة، تم الإرسال بأقرب جودة متاحة: {actual_height}p"

    await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False)


async def handle_ytdlp_audio_bitrate(query, context, url, bitrate, chat_id, bot):
    info = context.user_data.get('ytdlp_info')
    if not info:
        info = await asyncio.get_running_loop().run_in_executor(None, extract_info_only, url)
    meta_caption = build_meta_caption(
        uploader=info.get("uploader") or info.get("channel"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        title=info.get("title"),
    )
    thumb = info.get("thumbnail")

    status_msg = await send_progress_placeholder(query, thumb, meta_caption)
    dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
    state = {"stage": "downloading", "percent": None, "downloaded": 0}
    filepath, _ = await run_with_live_progress(
        yt_dlp_download_with_progress, (url, dest_template, state, True, None, bitrate),
        status_msg, meta_caption, state
    )

    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("تعذر العثور على الملف بعد التحميل، جرب رابطاً آخر.")

    await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=True)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "action_cancel":
        await query.edit_message_text("❌ تم إلغاء الطلب.")
        return

    url = context.user_data.get('download_url')
    if not url:
        await query.edit_message_text("❌ انتهت الجلسة، أرسل الرابط مجدداً.")
        return

    chat_id = query.message.chat_id
    bot = context.bot
    status_msg = None

    try:
        # 🎬 جودة فيديو محددة (نفس المنطق لكل المصادر: يوتيوب/إنستغرام/تيك توك...)
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            await handle_ytdlp_video_quality(query, context, url, height, chat_id, bot)
            return

        # 🎵 بتريت صوت محدد (128/64)
        if query.data.startswith("a_"):
            bitrate = int(query.data.split("_")[1])
            await handle_ytdlp_audio_bitrate(query, context, url, bitrate, chat_id, bot)
            return

        # ⚡ المسار السريع: أفضل جودة عبر رابط مباشر بدون تنزيل/رفع من عندنا
        if query.data == "v_instant":
            await query.edit_message_text("🔍 جاري جلب معلومات الفيديو...")
            direct_url, info = await asyncio.get_running_loop().run_in_executor(
                None, get_direct_video_url, url
            )
            meta_caption = build_meta_caption(
                uploader=info.get("uploader") or info.get("channel"),
                duration=info.get("duration"),
                views=info.get("view_count"),
                title=info.get("title"),
            )
            thumb = info.get("thumbnail")
            if not direct_url:
                await query.edit_message_text("❌ تعذر جلب رابط الفيديو المباشر، جرب رابطاً آخر.")
                return
            status_msg = await send_progress_placeholder(query, thumb, meta_caption)
            await send_via_direct_url(bot, chat_id, status_msg, direct_url, meta_caption, thumb)
            return

    except Exception as e:
        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception:
                pass
        try:
            await bot.send_message(chat_id=chat_id, text=f"❌ **حدث خطأ:**\n`{str(e)[:150]}`", parse_mode='Markdown')
        except Exception:
            pass


def main():
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=180,
        write_timeout=180,
        pool_timeout=30,
        connection_pool_size=8,
    )
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل الآن بنجاح وتكامل تام...")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
