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
TOKEN = "8846997512:AAFfc2HSrJHWmXHfiEMO_M5I4F-OPc3zrrk"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# رموز دائرة الانتظار المتحركة
SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]


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


def download_tiktok_fast(tiktok_url):
    """محفوظة للتوافق: ترجع رابط الفيديو المباشر فقط (نفس السلوك القديم)"""
    data = get_tiktok_data(tiktok_url)
    if data:
        return data.get("hdplay") or data.get("play")
    return None


# ---------------- تنزيل الملف مع تتبع نسبة التقدم الحقيقية ----------------

async def edit_progress_message(status_msg, text):
    try:
        await status_msg.edit_caption(caption=text)
    except Exception:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass


def download_file_with_progress(direct_url, dest_path, loop, status_msg, meta_caption):
    """تنزيل رابط مباشر (تيك توك) عبر requests مع تحديث حي لنسبة التقدم"""
    headers = {"User-Agent": "Mozilla/5.0"}
    with requests.get(direct_url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        last_update = 0
        frame_idx = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_update >= 1.2:
                    last_update = now
                    percent = (downloaded / total * 100) if total else 0
                    frame_idx = (frame_idx + 1) % len(SPINNER_FRAMES)
                    text = (
                        f"{meta_caption}\n\n"
                        f"{SPINNER_FRAMES[frame_idx]} جاري التحميل...\n"
                        f"{build_progress_bar(percent)}"
                    )
                    asyncio.run_coroutine_threadsafe(edit_progress_message(status_msg, text), loop)
    return dest_path


def yt_dlp_download_with_progress(url, dest_template, loop, status_msg, meta_caption, audio_only=False):
    """تنزيل حقيقي عبر yt-dlp (وليس رابط مباشر فقط) للحصول على نسبة تقدم صحيحة"""
    state = {"last_update": 0, "frame": 0}

    def hook(d):
        if d.get("status") == "downloading":
            now = time.time()
            if now - state["last_update"] < 1.2:
                return
            state["last_update"] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            state["frame"] = (state["frame"] + 1) % len(SPINNER_FRAMES)
            text = (
                f"{meta_caption}\n\n"
                f"{SPINNER_FRAMES[state['frame']]} جاري التحميل...\n"
                f"{build_progress_bar(percent)}"
            )
            asyncio.run_coroutine_threadsafe(edit_progress_message(status_msg, text), loop)
        elif d.get("status") == "finished":
            text = f"{meta_caption}\n\n⏳ جاري المعالجة والرفع..."
            asyncio.run_coroutine_threadsafe(edit_progress_message(status_msg, text), loop)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": dest_template,
        "socket_timeout": 20,
        "progress_hooks": [hook],
    }

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["format"] = "b/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        if audio_only:
            base, _ = os.path.splitext(filepath)
            filepath = base + ".mp3"
        return filepath, info


def extract_info_only(url):
    """جلب معلومات الفيديو فقط (بدون تنزيل) للحصول على الصورة المصغرة والوصف"""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}
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

    keyboard = [
        [InlineKeyboardButton("⚡ تحميل الفيديو", callback_data="v_instant")],
        [InlineKeyboardButton("🎵 تحميل الصوت فقط", callback_data="v_audio")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")]
    ]
    await update.message.reply_text("👇 **اختر التحميل:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def send_progress_placeholder(query, thumb_url, meta_caption):
    """إرسال صورة مصغرة من الفيديو مع نص يحتوي الوصف ونسبة تقدم 0%"""
    text = f"{meta_caption}\n\n{SPINNER_FRAMES[0]} جاري التحميل...\n{build_progress_bar(0)}"
    if thumb_url:
        try:
            msg = await query.message.reply_photo(photo=thumb_url, caption=text)
            await query.delete_message()
            return msg
        except Exception:
            pass
    return await query.edit_message_text(text)


async def send_final_file(status_msg, filepath, meta_caption, is_audio=False):
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await status_msg.reply_audio(audio=f, caption=caption)
            else:
                await status_msg.reply_video(video=f, caption=caption, supports_streaming=True)
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
        if os.path.exists(filepath):
            os.remove(filepath)


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

    want_audio = query.data == "v_audio"
    loop = asyncio.get_running_loop()

    try:
        # 1️⃣ نفس منطق تيك توك الأصلي عبر TikWM API + إضافة الوصف والتقدم
        if "tiktok.com" in url:
            data = await loop.run_in_executor(None, get_tiktok_data, url)
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
            direct_url = data.get("music") if want_audio else (data.get("hdplay") or data.get("play"))

            if not direct_url:
                await query.edit_message_text("❌ تعذر جلب الرابط المباشر.")
                return

            status_msg = await send_progress_placeholder(query, thumb, meta_caption)

            ext = "mp3" if want_audio else "mp4"
            filename = f"{DOWNLOAD_DIR}/tt_{status_msg.message_id}.{ext}"
            await loop.run_in_executor(
                None, download_file_with_progress, direct_url, filename, loop, status_msg, meta_caption
            )
            await send_final_file(status_msg, filename, meta_caption, is_audio=want_audio)
            return

        # 2️⃣ نفس منطق yt-dlp الأصلي (إنستغرام، يوتيوب... إلخ) + الوصف والتقدم
        else:
            info = await loop.run_in_executor(None, extract_info_only, url)
            meta_caption = build_meta_caption(
                uploader=info.get("uploader") or info.get("channel"),
                duration=info.get("duration"),
                views=info.get("view_count"),
                title=info.get("title"),
            )
            thumb = info.get("thumbnail")

            status_msg = await send_progress_placeholder(query, thumb, meta_caption)

            dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
            filepath, _ = await loop.run_in_executor(
                None, yt_dlp_download_with_progress, url, dest_template, loop, status_msg, meta_caption, want_audio
            )

            if not filepath or not os.path.exists(filepath):
                await status_msg.edit_caption(caption="❌ تعذر جلب المقطع، جرب رابطاً آخر.")
                return

            await send_final_file(status_msg, filepath, meta_caption, is_audio=want_audio)
            return

    except Exception as e:
        await query.edit_message_text(f"❌ **حدث خطأ:**\n`{str(e)[:150]}`", parse_mode='Markdown')


def main():
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل الآن بنجاح وتكامل تام...")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
