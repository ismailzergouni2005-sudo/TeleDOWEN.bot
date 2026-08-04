import os
import re
import time
import logging
import asyncio
import threading
import subprocess
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
TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# رموز دائرة الانتظار المتحركة
SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]

# قوائم الجودات المدعومة (تطابق الأزرار المطلوبة)
VIDEO_QUALITIES = [1080, 720, 480, 360, 240, 144]
AUDIO_BITRATES = [128, 64]


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

def build_quality_keyboard_generic():
    """لوحة الجودات القياسية (يوتيوب/إنستغرام عبر yt-dlp) — شبكة 3x2 للفيديو
    + صف لبتريت الصوت، بنفس شكل اللقطة المرفقة. الجودة الفعلية المُسلَّمة قد
    تختلف قليلاً إن لم تتوفر بالضبط، وستظهر في وصف الفيديو بعد التحميل."""
    rows = []
    for i in range(0, len(VIDEO_QUALITIES), 3):
        chunk = VIDEO_QUALITIES[i:i + 3]
        rows.append([
            InlineKeyboardButton(f"🎬 {q}p", callback_data=f"q_{q}") for q in chunk
        ])
    rows.append([
        InlineKeyboardButton(f"🎵 {b}kbps", callback_data=f"a_{b}") for b in AUDIO_BITRATES
    ])
    rows.append([InlineKeyboardButton("⚡ أفضل جودة (إرسال فوري)", callback_data="v_instant")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)


def build_quality_keyboard_tiktok(data):
    """تيك توك عبر TikWM يوفر فعلياً نسختين مختلفتين فقط من الفيديو (HD و SD)،
    وليس 6 مستويات دقة كما في يوتيوب. لذا نعرض الخيارات الحقيقية فقط مع حجمها
    الفعلي بدل الإيحاء بوجود جودات غير موجودة فعلاً."""
    hd_url = data.get("hdplay")
    sd_url = data.get("play")
    hd_size = format_size(data.get("hd_size") or data.get("hdsize"))
    sd_size = format_size(data.get("size") or data.get("wm_size"))

    rows = []
    video_row = []
    if hd_url:
        label = "🎬 جودة عالية (HD)" + (f" · {hd_size}" if hd_size else "")
        video_row.append(InlineKeyboardButton(label, callback_data="th"))
    # لا نعرض زر SD منفصلاً إن كان بنفس رابط HD (يعني تيك توك لا يوفر نسخة أخرى فعلياً)
    if sd_url and sd_url != hd_url:
        label = "🎬 جودة عادية (SD)" + (f" · {sd_size}" if sd_size else "")
        video_row.append(InlineKeyboardButton(label, callback_data="ts"))
    if video_row:
        rows.append(video_row)
    elif sd_url:
        # لا يوجد سوى رابط واحد متاح فعلياً
        rows.append([InlineKeyboardButton("🎬 تحميل الفيديو", callback_data="th")])

    rows.append([
        InlineKeyboardButton(f"🎵 {b}kbps", callback_data=f"a_{b}") for b in AUDIO_BITRATES
    ])
    rows.append([InlineKeyboardButton("⚡ إرسال فوري", callback_data="v_instant")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------- تيك توك (نفس منطق TikWM الأصلي) ----------------

def get_tiktok_data(tiktok_url):
    """جلب بيانات فيديو تيك توك (روابط + معلومات) عبر TikWM API"""
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    params = {"url": tiktok_url, "hd": 1}
    response = requests.get(api_url, params=params, headers=headers, timeout=10)
    data = response.json()
    if data.get("code") == 0:
        return data["data"]
    return None


# ---------------- الحصول على رابط الفيديو المباشر (بدون تنزيل محلي) ----------------

def get_direct_video_url(url):
    """يجلب رابط الفيديو المباشر + معلوماته، بدون تنزيل الملف على السيرفر إطلاقاً."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "b[ext=mp4]/b/best",
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
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


def download_file_with_progress(direct_url, dest_path, state):
    """تنزيل رابط مباشر (تيك توك) عبر requests مع تحديث حالة التقدم في state"""
    headers = {"User-Agent": "Mozilla/5.0"}
    with requests.get(direct_url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                state["downloaded"] = downloaded
                state["percent"] = (downloaded / total * 100) if total > 0 else None
    state["stage"] = "processing"
    return dest_path


def convert_audio_bitrate(input_path, output_path, bitrate_kbps):
    """يحوّل ملف صوتي إلى بتريت محدد عبر ffmpeg (يُستخدم لتيك توك حيث
    لا يوفر TikWM خيارات بتريت مباشرة)"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-b:a", f"{bitrate_kbps}k",
            "-vn", output_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output_path


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
        "quiet": True,
        "no_warnings": True,
        "outtmpl": dest_template,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
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
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


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
    context.user_data.pop('tiktok_data', None)

    if "tiktok.com" in url:
        checking_msg = await update.message.reply_text("🔍 جاري تحليل الرابط...")
        data = await asyncio.get_running_loop().run_in_executor(None, get_tiktok_data, url)
        if not data:
            await checking_msg.edit_text("❌ تعذر جلب مقطع تيك توك، تأكد من صحة الرابط.")
            return
        context.user_data['tiktok_data'] = data
        await checking_msg.edit_text(
            "👇 **اختر الجودة المتاحة فعلياً:**", reply_markup=build_quality_keyboard_tiktok(data),
            parse_mode='Markdown'
        )
        return

    await update.message.reply_text(
        "👇 **اختر الجودة:**", reply_markup=build_quality_keyboard_generic(), parse_mode='Markdown'
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
    caption = f"{meta_caption}\n\n✅ تم الإرسال بنجاح!"

    ticker = asyncio.create_task(_sending_ticker(status_msg, meta_caption))
    try:
        await bot.send_video(
            chat_id=chat_id, video=direct_url, caption=caption, supports_streaming=True,
            read_timeout=180, write_timeout=180, connect_timeout=30,
        )
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
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
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"

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


async def handle_tiktok_video_pick(query, context, data, which, chat_id, bot):
    """which: 'hd' أو 'sd' — يستخدم الرابط الحقيقي المطابق تماماً لما اختاره المستخدم"""
    author = data.get("author", {}) or {}
    meta_caption = build_meta_caption(
        uploader=author.get("nickname") or author.get("unique_id"),
        duration=data.get("duration"),
        views=data.get("play_count"),
        title=data.get("title"),
    )
    thumb = data.get("origin_cover") or data.get("cover")
    if which == "hd":
        direct_url = data.get("hdplay") or data.get("play")
    else:
        direct_url = data.get("play") or data.get("hdplay")

    if not direct_url:
        await query.edit_message_text("❌ تعذر جلب الرابط المباشر لهذه الجودة.")
        return

    status_msg = await send_progress_placeholder(query, thumb, meta_caption)
    await send_via_direct_url(bot, chat_id, status_msg, direct_url, meta_caption, thumb)


async def handle_tiktok_audio_bitrate(query, context, data, bitrate, chat_id, bot):
    author = data.get("author", {}) or {}
    meta_caption = build_meta_caption(
        uploader=author.get("nickname") or author.get("unique_id"),
        duration=data.get("duration"),
        views=data.get("play_count"),
        title=data.get("title"),
    )
    thumb = data.get("origin_cover") or data.get("cover")
    direct_url = data.get("music")
    if not direct_url:
        await query.edit_message_text("❌ تعذر جلب رابط الصوت.")
        return

    status_msg = await send_progress_placeholder(query, thumb, meta_caption)
    raw_path = f"{DOWNLOAD_DIR}/tt_raw_{status_msg.message_id}.mp3"
    final_path = f"{DOWNLOAD_DIR}/tt_{bitrate}k_{status_msg.message_id}.mp3"

    state = {"stage": "downloading", "percent": None, "downloaded": 0}
    await run_with_live_progress(
        download_file_with_progress, (direct_url, raw_path, state), status_msg, meta_caption, state
    )

    if not os.path.exists(raw_path):
        raise RuntimeError("تعذر حفظ ملف الصوت محلياً.")

    state["stage"] = "processing"
    await asyncio.get_running_loop().run_in_executor(
        None, convert_audio_bitrate, raw_path, final_path, bitrate
    )
    if os.path.exists(raw_path):
        os.remove(raw_path)

    await send_final_file(bot, chat_id, status_msg, final_path, meta_caption, is_audio=True)


async def handle_ytdlp_video_quality(query, context, url, height, chat_id, bot):
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
    is_tiktok = "tiktok.com" in url

    try:
        # 🎬 تيك توك: جودة حقيقية واحدة (HD) أو (SD) — مطابقة تماماً لما يعرضه الزر
        if query.data in ("th", "ts"):
            data = context.user_data.get('tiktok_data')
            if not data:
                data = await asyncio.get_running_loop().run_in_executor(None, get_tiktok_data, url)
            if not data:
                await query.edit_message_text("❌ تعذر جلب مقطع تيك توك، تأكد من صحة الرابط.")
                return
            which = "hd" if query.data == "th" else "sd"
            await handle_tiktok_video_pick(query, context, data, which, chat_id, bot)
            return

        # 🎬 جودة فيديو محددة ليوتيوب/إنستغرام (1080/720/480/360/240/144)
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            await handle_ytdlp_video_quality(query, context, url, height, chat_id, bot)
            return

        # 🎵 بتريت صوت محدد (128/64)
        if query.data.startswith("a_"):
            bitrate = int(query.data.split("_")[1])
            if is_tiktok:
                data = context.user_data.get('tiktok_data')
                if not data:
                    data = await asyncio.get_running_loop().run_in_executor(None, get_tiktok_data, url)
                if not data:
                    await query.edit_message_text("❌ تعذر جلب مقطع تيك توك، تأكد من صحة الرابط.")
                    return
                await handle_tiktok_audio_bitrate(query, context, data, bitrate, chat_id, bot)
            else:
                await handle_ytdlp_audio_bitrate(query, context, url, bitrate, chat_id, bot)
            return

        # ⚡ المسار السريع الأصلي: أفضل جودة عبر رابط مباشر بدون تنزيل/رفع من عندنا
        if query.data == "v_instant":
            if is_tiktok:
                data = context.user_data.get('tiktok_data')
                if not data:
                    data = await asyncio.get_running_loop().run_in_executor(None, get_tiktok_data, url)
                if not data:
                    await query.edit_message_text("❌ تعذر جلب مقطع تيك توك، تأكد من صحة الرابط.")
                    return
                author = data.get("author", {}) or {}
                meta_caption = build_meta_caption(
                    uploader=author.get("nickname") or author.get("unique_id"),
                    duration=data.get("duration"),
                    views=data.get("play_count"),
                    title=data.get("title"),
                )
                thumb = data.get("origin_cover") or data.get("cover")
                direct_url = data.get("hdplay") or data.get("play")
                if not direct_url:
                    await query.edit_message_text("❌ تعذر جلب الرابط المباشر.")
                    return
                status_msg = await send_progress_placeholder(query, thumb, meta_caption)
                await send_via_direct_url(bot, chat_id, status_msg, direct_url, meta_caption, thumb)
                return
            else:
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
