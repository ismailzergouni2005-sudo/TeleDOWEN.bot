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
    """يبني نص جسم الرسالة حسب المرحلة الحالية (تحميل / معالجة)"""
    stage = state.get("stage", "downloading")
    if stage == "processing":
        return "جاري معالجة الفيديو..."
    percent = state.get("percent")
    if percent is not None:
        return build_progress_bar(percent)
    mb = (state.get("downloaded") or 0) / (1024 * 1024)
    return f"تم تحميل {mb:.1f} MB..."


async def progress_ticker(status_msg, meta_caption, state):
    """مهمة تعمل باستمرار (كل ثانية تقريباً) لتحديث الرسالة بشكل حي،
    تعمل من داخل الحلقة الرئيسية مباشرة (بدون قفزات بين الخيوط)"""
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
    """يشغّل دالة تحميل (blocking) في executor مع مؤشر تقدّم حي بالتوازي"""
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
    state["stage"] = "processing"  # لضمان ظهور "تم بنجاح" فور الانتهاء حتى لو كان سريعاً
    return dest_path


def _locate_downloaded_file(info, ydl, audio_only):
    """يحدد المسار الفعلي للملف الناتج بعد التحميل/الدمج، بدل الاعتماد فقط على
    prepare_filename الذي قد يختلف عن الامتداد الحقيقي بعد دمج ffmpeg"""
    # الطريقة الأدق: yt-dlp يسجل المسارات الفعلية في requested_downloads
    requested = info.get("requested_downloads") or []
    for item in requested:
        fp = item.get("filepath") or item.get("_filename")
        if fp and os.path.exists(fp):
            return fp

    # احتياطي: الاسم المتوقع كما هو
    expected = ydl.prepare_filename(info)
    if audio_only:
        base, _ = os.path.splitext(expected)
        expected = base + ".mp3"
    if os.path.exists(expected):
        return expected

    # احتياطي أخير: ابحث عن أي ملف بنفس الاسم الأساسي (دون الامتداد) في مجلد التحميل
    base_no_ext, _ = os.path.splitext(expected)
    directory = os.path.dirname(base_no_ext) or "."
    base_name = os.path.basename(base_no_ext)
    if os.path.isdir(directory):
        for fname in os.listdir(directory):
            if fname.startswith(base_name):
                return os.path.join(directory, fname)
    return None


def yt_dlp_download_with_progress(url, dest_template, state, audio_only=False):
    """تنزيل حقيقي عبر yt-dlp مع تحديث حالة التقدم في state (يشمل مرحلة المعالجة)"""

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
            # انتهى تنزيل البيانات الخام، قد يبدأ الآن دمج/تحويل عبر ffmpeg
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
            "preferredquality": "192",
        }]
    else:
        # نفضّل ملف mp4 جاهز (فيديو+صوت في ملف واحد) لتفادي الحاجة لدمج ffmpeg قدر الإمكان
        ydl_opts["format"] = "b[ext=mp4]/b/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        state["stage"] = "processing"
        filepath = _locate_downloaded_file(info, ydl, audio_only)
        return filepath, info


def extract_info_only(url):
    """جلب معلومات الفيديو فقط (بدون تنزيل) للحصول على الصورة المصغرة والوصف"""
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


async def upload_ticker(status_msg, meta_caption, filepath):
    """يعرض نسبة تقديرية متحركة أثناء الرفع (تيليجرام لا يوفر نسبة رفع حقيقية)،
    مبنية على حجم الملف وسرعة رفع افتراضية، محدودة بـ 95% حتى يكتمل الرفع فعلياً"""
    try:
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
    except OSError:
        size_mb = 5
    assumed_speed_mbps = 1.5  # سرعة رفع تقديرية متحفظة
    estimated_seconds = max(4, size_mb / assumed_speed_mbps)

    frame = 0
    start = time.time()
    try:
        while True:
            frame = (frame + 1) % len(SPINNER_FRAMES)
            elapsed = time.time() - start
            percent = min(95, (elapsed / estimated_seconds) * 100)
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري رفع الفيديو...\n{build_progress_bar(percent)}"
            await edit_progress_message(status_msg, text)
            await asyncio.sleep(1.2)
    except asyncio.CancelledError:
        pass


async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False):
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"

    # نُبقي رسالة الصورة ظاهرة مع نسبة رفع متحركة طوال مدة الرفع، لتفادي أي تجمّد أو فراغ
    ticker = asyncio.create_task(upload_ticker(status_msg, meta_caption, filepath))
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

    # نحذف رسالة الصورة فقط بعد ظهور الفيديو فعلياً -> لا يوجد فراغ بينهما
    try:
        await status_msg.delete()
    except Exception:
        pass


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
    chat_id = query.message.chat_id
    bot = context.bot
    status_msg = None

    try:
        # 1️⃣ نفس منطق تيك توك الأصلي عبر TikWM API + إضافة الوصف والتقدم
        if "tiktok.com" in url:
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
            direct_url = data.get("music") if want_audio else (data.get("hdplay") or data.get("play"))

            if not direct_url:
                await query.edit_message_text("❌ تعذر جلب الرابط المباشر.")
                return

            status_msg = await send_progress_placeholder(query, thumb, meta_caption)

            ext = "mp3" if want_audio else "mp4"
            filename = f"{DOWNLOAD_DIR}/tt_{status_msg.message_id}.{ext}"

            state = {"stage": "downloading", "percent": None, "downloaded": 0}
            await run_with_live_progress(
                download_file_with_progress, (direct_url, filename, state), status_msg, meta_caption, state
            )

            if not os.path.exists(filename):
                raise RuntimeError("تعذر حفظ ملف تيك توك محلياً.")

            await send_final_file(bot, chat_id, status_msg, filename, meta_caption, is_audio=want_audio)
            return

        # 2️⃣ نفس منطق yt-dlp الأصلي (إنستغرام، يوتيوب... إلخ) + الوصف والتقدم
        else:
            # تحديث فوري حتى لا تبقى الرسالة بلا رد أثناء انتظار معلومات الفيديو (قد تستغرق ثوانٍ لإنستغرام)
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
            filepath, _ = await run_with_live_progress(
                yt_dlp_download_with_progress, (url, dest_template, state, want_audio),
                status_msg, meta_caption, state
            )

            if not filepath or not os.path.exists(filepath):
                raise RuntimeError("تعذر العثور على الملف بعد التحميل، جرب رابطاً آخر.")

            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=want_audio)
            return

    except Exception as e:
        # نحذف رسالة الصورة دائماً عند حدوث خطأ حتى لا تبقى معلّقة، ثم نُرسل رسالة خطأ جديدة
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
    # مهلات أطول لأن رفع ملفات فيديو كبيرة قد يستغرق دقائق
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=180,
        write_timeout=180,
        pool_timeout=30,
    )
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل الآن بنجاح وتكامل تام...")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
