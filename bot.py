import os
import re
import time
import shutil
import logging
import asyncio
import threading
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter
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

# ⚠️ التوكن يُقرأ حصراً من متغير بيئة
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "❌ لم يتم تعيين متغير البيئة BOT_TOKEN. "
        "أضفه من إعدادات Environment Variables على Render قبل التشغيل."
    )

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 🎞️ فحص توفر ffmpeg على السيرفر
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = None

FFMPEG_AVAILABLE = FFMPEG_PATH is not None
if not FFMPEG_AVAILABLE:
    logging.warning(
        "⚠️ ffmpeg غير متاح على هذا السيرفر — سيتم تعطيل خيارات الجودة التي "
        "تحتاج دمج فيديو+صوت وكذلك أزرار تحميل الصوت. "
        "لتفعيلها: pip install imageio-ffmpeg"
    )
else:
    logging.info(f"✅ ffmpeg متاح: {FFMPEG_PATH}")

# رموز دائرة الانتظار المتحركة
SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]

# قوائم الجودات المدعومة
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


def get_remote_file_size(url):
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


# ---------------- استخراج الجودات المتاحة فعلياً ----------------

def get_direct_quality_map(info):
    formats = info.get("formats") or []
    video_map = {}
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        url = f.get("url")
        if h and url and vcodec not in (None, "none") and acodec not in (None, "none"):
            current = video_map.get(h)
            if not current or (f.get("tbr") or 0) > (current.get("tbr") or 0):
                video_map[h] = {"url": url, "tbr": f.get("tbr") or 0}
    return video_map


def get_direct_audio_map(info):
    formats = info.get("formats") or []
    audio_map = {}
    for f in formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        url = f.get("url")
        abr = f.get("abr")
        if url and vcodec in (None, "none") and acodec not in (None, "none") and abr:
            key = int(round(abr))
            current = audio_map.get(key)
            if not current or (f.get("tbr") or 0) > (current.get("tbr") or 0):
                audio_map[key] = {"url": url, "tbr": f.get("tbr") or 0}
    return audio_map


def get_merge_only_heights(info, exclude_heights):
    formats = info.get("formats") or []
    heights = set()
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        if h and vcodec and vcodec != "none" and h not in exclude_heights:
            heights.add(int(h))
    return sorted(heights, reverse=True)


# ---------------- لوحة اختيار الجودة ----------------

def build_quality_keyboard_generic(heights=None, bitrates=None, merge_heights=None):
    rows = []
    if heights:
        capped = heights[:6]
        buttons = [InlineKeyboardButton(f"🎬 {h}p", callback_data=f"q_{h}") for h in capped]
        for i in range(0, len(buttons), 3):
            rows.append(buttons[i:i + 3])

    if merge_heights and FFMPEG_AVAILABLE:
        capped_merge = merge_heights[:6]
        buttons = [InlineKeyboardButton(f"🎞️ {h}p (دمج)", callback_data=f"qm_{h}") for h in capped_merge]
        for i in range(0, len(buttons), 3):
            rows.append(buttons[i:i + 3])

    if bitrates and FFMPEG_AVAILABLE:
        rows.append([
            InlineKeyboardButton(f"🎵 {b}kbps", callback_data=f"a_{b}") for b in bitrates[:2]
        ])

    rows.append([InlineKeyboardButton("⚡ أفضل جودة (إرسال فوري)", callback_data="v_instant")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------- الحصول على رابط الفيديو المباشر ----------------

def get_direct_video_url(url):
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


# ---------------- تنزيل الملف مع تتبع نسبة التقدم الدوري ----------------

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


# ✅ تم تحديث الدالة لتقييد زمن التحديث تجنباً للحظر
async def progress_ticker(status_msg, meta_caption, state):
    frame = 0
    last_text = None
    last_update_time = 0
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            body = _progress_body(state)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} {body}"
            
            now = time.time()
            if text != last_text and (now - last_update_time) >= 2.0:
                last_text = text
                last_update_time = now
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
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate) if bitrate else "192",
        }]
    elif height:
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
    context.user_data.pop('ytdlp_info', None)

    checking_msg = await update.message.reply_text("🔍 جاري تحليل الرابط...")
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, extract_info_only, url)
    except Exception as e:
        await checking_msg.edit_text(f"❌ تعذر جلب معلومات الرابط:\n`{str(e)[:150]}`", parse_mode='Markdown')
        return

    context.user_data['ytdlp_info'] = info
    video_map = get_direct_quality_map(info)
    audio_map = get_direct_audio_map(info)
    context.user_data['direct_video_map'] = video_map
    context.user_data['direct_audio_map'] = audio_map

    heights = sorted(video_map.keys(), reverse=True)
    bitrates = sorted(audio_map.keys(), reverse=True)
    merge_heights = get_merge_only_heights(info, exclude_heights=set(heights)) if FFMPEG_AVAILABLE else []
    context.user_data['merge_video_heights'] = merge_heights

    lines = []
    if heights:
        qualities_text = "، ".join(f"{h}p" for h in heights[:6])
        lines.append(f"🎬 جودات فورية (إرسال مباشر بدون انتظار): {qualities_text}")
    if merge_heights:
        merge_text = "، ".join(f"{h}p" for h in merge_heights[:6])
        lines.append(f"🎞️ جودات تحتاج دمج فيديو+صوت (أبطأ قليلاً): {merge_text}")
    if not heights and not merge_heights:
        lines.append("🎬 لا توجد جودات محددة، استخدم زر ⚡ الإرسال الفوري.")

    await checking_msg.edit_text(
        "👇 **اختر الجودة:**\n" + "\n".join(lines),
        reply_markup=build_quality_keyboard_generic(heights, bitrates, merge_heights),
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


async def send_via_direct_url(bot, chat_id, status_msg, direct_url, meta_caption, thumb_url=None, is_audio=False):
    ticker = asyncio.create_task(_sending_ticker(status_msg, meta_caption))
    try:
        size_task = asyncio.get_running_loop().run_in_executor(None, get_remote_file_size, direct_url)
        async def _do_send():
            if is_audio:
                return await bot.send_audio(
                    chat_id=chat_id, audio=direct_url, caption=f"{meta_caption}\n\n✅ تم الإرسال بنجاح!",
                    read_timeout=180, write_timeout=180, connect_timeout=30,
                )
            return await bot.send_video(
                chat_id=chat_id, video=direct_url, caption=f"{meta_caption}\n\n✅ تم الإرسال بنجاح!",
                supports_streaming=True,
                read_timeout=180, write_timeout=180, connect_timeout=30,
            )
        sent_message = await send_with_retry(_do_send)
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


# ✅ تم تحديث الدالة لتتحدث كل 2.5 ثانية
async def _sending_ticker(status_msg, meta_caption):
    frame = 0
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري الإرسال..."
            await edit_progress_message(status_msg, text)
            await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        pass


# ✅ تم تحديث الدالة لتتحدث كل 2.5 ثانية
async def upload_ticker(status_msg, meta_caption):
    frame = 0
    start = time.time()
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            elapsed = int(time.time() - start)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري رفع الفيديو... ({elapsed} ث)"
            await edit_progress_message(status_msg, text)
            await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        pass


async def send_with_retry(send_fn, max_attempts=4, base_delay=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await send_fn()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            last_error = e
        except (TimedOut, NetworkError) as e:
            last_error = e
            if attempt < max_attempts:
                await asyncio.sleep(base_delay * attempt)
            continue
    raise last_error


async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False):
    size_label = format_size(os.path.getsize(filepath)) if os.path.exists(filepath) else None
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"
    if size_label:
        caption += f"\n📦 الحجم: {size_label}"

    ticker = asyncio.create_task(upload_ticker(status_msg, meta_caption))
    try:
        async def _do_send():
            with open(filepath, "rb") as f:
                if is_audio:
                    return await bot.send_audio(
                        chat_id=chat_id, audio=f, caption=caption,
                        read_timeout=180, write_timeout=180, connect_timeout=30,
                    )
                return await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption, supports_streaming=True,
                    read_timeout=180, write_timeout=180, connect_timeout=30,
                )
        await send_with_retry(_do_send)
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
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            entry = (context.user_data.get('direct_video_map') or {}).get(height)
            if not entry:
                await query.edit_message_text("❌ انتهت صلاحية هذه الجودة، أرسل الرابط مجدداً.")
                return
            info = context.user_data.get('ytdlp_info') or {}
            meta_caption = build_meta_caption(
                uploader=info.get("uploader") or info.get("channel"),
                duration=info.get("duration"),
                views=info.get("view_count"),
                title=info.get("title"),
            )
            thumb = info.get("thumbnail")
            status_msg = await send_progress_placeholder(query, thumb, meta_caption)
            await send_via_direct_url(bot, chat_id, status_msg, entry["url"], meta_caption, thumb)
            return

        if query.data.startswith("qm_"):
            if not FFMPEG_AVAILABLE:
                await query.edit_message_text("❌ هذه الجودة تحتاج ffmpeg غير مثبت على السيرفر حالياً.")
                return
            height = int(query.data.split("_")[1])
            await handle_ytdlp_video_quality(query, context, url, height, chat_id, bot)
            return

        if query.data.startswith("a_"):
            if not FFMPEG_AVAILABLE:
                await query.edit_message_text("❌ تحميل الصوت يحتاج ffmpeg غير مثبت على السيرفر حالياً.")
                return
            bitrate = int(query.data.split("_")[1])
            await handle_ytdlp_audio_bitrate(query, context, url, bitrate, chat_id, bot)
            return

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
