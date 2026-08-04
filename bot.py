import os
import re
import time
import shutil
import logging
import asyncio
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ لم يتم تعيين متغير البيئة BOT_TOKEN.")

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

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

# ---------------- أدوات التنسيق والواجهة ----------------

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
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{m:02d}"

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

def build_meta_caption(uploader=None, duration=None, views=None, title=None, quality=None):
    lines = []
    if title:
        title_short = title if len(title) <= 60 else title[:57] + "..."
        lines.append(f"📝 {title_short}")
    lines.append(f"👤 الحساب: {uploader or 'غير معروف'}")
    lines.append(f"⏱ المدة: {format_duration(duration) if duration else 'غير معروف'}")
    lines.append(f"👁 المشاهدات: {format_count(views) if views is not None else 'غير معروف'}")
    if quality:
        lines.append(f"🎬 الجودة/الصيغة: {quality}")
    return "\n".join(lines)

# ---------------- تجميع خيارات الجودة والصيغة ----------------

def get_available_options(info):
    formats = info.get("formats") or []
    video_opts = {}
    
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        ext = f.get("ext", "mp4")
        if h and vcodec not in (None, "none"):
            if h not in video_opts:
                video_opts[h] = ext

    return video_opts

def build_quality_keyboard(video_opts):
    rows = []
    heights = sorted(video_opts.keys(), reverse=True)[:6]
    
    buttons = []
    for h in heights:
        ext = video_opts[h].upper()
        buttons.append(InlineKeyboardButton(f"🎬 {h}p ({ext})", callback_data=f"q_{h}"))
    
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])

    rows.append([
        InlineKeyboardButton("🎵 MP3 (صوت فقط)", callback_data="a_mp3"),
        InlineKeyboardButton("🎵 M4A (صوت أصلي)", callback_data="a_m4a")
    ])
    rows.append([InlineKeyboardButton("⚡ أفضل جودة مباشرة", callback_data="v_instant")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")])
    return InlineKeyboardMarkup(rows)

def build_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 إلغاء التحميل", callback_data="cancel_active_task")]
    ])

def build_reselect_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 اختيار صيغة أخرى", callback_data="reselect_format")]
    ])

# ---------------- منطق التحميل والتعديل لـ Pinterest ----------------

def extract_info_only(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def yt_dlp_download_one_pass(url, dest_template, state, cancel_event, mode="video", height=None):
    def hook(d):
        if cancel_event.is_set():
            raise RuntimeError("CANCELLED")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            state["downloaded"] = downloaded
            if total:
                state["percent"] = downloaded / total * 100

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": dest_template,
        "socket_timeout": 60,
        "retries": 5,
        "progress_hooks": [hook],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    if mode == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif mode == "m4a":
        ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    elif height:
        ydl_opts["format"] = f"best[height<={height}]/b[height<={height}]/bestvideo+bestaudio/best"
        # إعادة ضغط الفيديو وتغيير مقاسه تلقائياً لتقليل الحجم
        if FFMPEG_PATH:
            ydl_opts["postprocessors"] = [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]
            ydl_opts["postprocessor_args"] = {
                'videoconvertor': ['-vf', f'scale=-2:{height}', '-crf', '28']
            }
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/b[ext=mp4]/b/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        if mode == "mp3":
            base, _ = os.path.splitext(filename)
            filename = base + ".mp3"
            
        if os.path.exists(filename):
            return filename, info

        base_no_ext, _ = os.path.splitext(filename)
        directory = os.path.dirname(base_no_ext) or "."
        base_name = os.path.basename(base_no_ext)
        for fname in os.listdir(directory):
            if fname.startswith(base_name):
                return os.path.join(directory, fname), info
                
        return filename, info

# ---------------- إدارة البث المباشر والتقدم ----------------

async def edit_progress_message(status_msg, text, reply_markup=None):
    try:
        await status_msg.edit_caption(caption=text, reply_markup=reply_markup)
    except Exception:
        try:
            await status_msg.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass

async def progress_ticker(status_msg, meta_caption, state, cancel_event):
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
                body = f"تم تحميل {mb:.1f} MB..."
            
            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري التحميل المباشر السريع...\n{body}"
            
            now = time.time()
            if text != last_text and (now - last_update) >= 2.0:
                last_text = text
                last_update = now
                await edit_progress_message(status_msg, text, reply_markup=build_cancel_keyboard())
                
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass

async def run_with_progress(func, args, status_msg, meta_caption, state, cancel_event):
    loop = asyncio.get_running_loop()
    ticker = asyncio.create_task(progress_ticker(status_msg, meta_caption, state, cancel_event))
    try:
        download_task = loop.run_in_executor(None, func, *args)
        while not download_task.done():
            if cancel_event.is_set():
                raise asyncio.CancelledError("تم الإلغاء.")
            await asyncio.sleep(0.5)
        return await download_task
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False):
    size_label = format_size(os.path.getsize(filepath)) if os.path.exists(filepath) else None
    caption = f"{meta_caption}\n\n✅ تم التحميل بنجاح!"
    if size_label:
        caption += f"\n📦 الحجم: {size_label}"

    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f, caption=caption,
                    reply_markup=build_reselect_keyboard(), read_timeout=180
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption,
                    reply_markup=build_reselect_keyboard(), supports_streaming=True, read_timeout=180
                )
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
        try:
            await status_msg.delete()
        except Exception:
            pass

# ---------------- معالجات الأوامر والأحداث ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✨ أهلاً بك! أرسل لي أي رابط لتنزيله بالصيغة والجودة التي تفضلها 🚀")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.search(r'https?://[^\s]+', text)
    if not match:
        await update.message.reply_text("❌ يرجى إرسال رابط صحيح.")
        return

    url = clean_url(match.group(0))
    context.user_data['download_url'] = url

    checking_msg = await update.message.reply_text("🔍 جاري جلب الجودات والصيغ المتاحة...")
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, extract_info_only, url)
        context.user_data['ytdlp_info'] = info
        video_opts = get_available_options(info)
        
        await checking_msg.edit_text(
            "👇 **اختر الصيغة والجودة المطلوبة:**",
            reply_markup=build_quality_keyboard(video_opts),
            parse_mode='Markdown'
        )
    except Exception as e:
        await checking_msg.edit_text(f"❌ تعذر تحليل الرابط:\n`{str(e)[:150]}`", parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "action_cancel":
        await query.delete_message()
        return

    if query.data == "cancel_active_task":
        cancel_event = context.user_data.get("active_cancel_event")
        if cancel_event:
            cancel_event.set()
        return

    if query.data == "reselect_format":
        info = context.user_data.get('ytdlp_info')
        if info:
            video_opts = get_available_options(info)
            await query.message.reply_text(
                "👇 **اختر الصيغة والجودة المطلوبة:**",
                reply_markup=build_quality_keyboard(video_opts),
                parse_mode='Markdown'
            )
        return

    url = context.user_data.get('download_url')
    if not url:
        await query.edit_message_text("❌ انتهت الجلسة، أرسل الرابط مجدداً.")
        return

    info = context.user_data.get('ytdlp_info') or {}
    
    quality_str = None
    if query.data.startswith("q_"):
        height = query.data.split("_")[1]
        quality_str = f"{height}p"
    elif query.data == "a_mp3":
        quality_str = "MP3 (صوت)"
    elif query.data == "a_m4a":
        quality_str = "M4A (صوت أصلي)"
    elif query.data == "v_instant":
        quality_str = "أفضل جودة متاحة"

    meta_caption = build_meta_caption(
        uploader=info.get("uploader") or info.get("channel"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        title=info.get("title"),
        quality=quality_str
    )

    cancel_event = asyncio.Event()
    context.user_data["active_cancel_event"] = cancel_event

    status_msg = await query.edit_message_text(f"{meta_caption}\n\n⏳ جاري بدء التحميل السريع...")

    chat_id = query.message.chat_id
    bot = context.bot
    dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
    state = {"percent": None, "downloaded": 0}

    try:
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", height),
                status_msg, meta_caption, state, cancel_event
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False)

        elif query.data.startswith("a_"):
            mode = query.data.split("_")[1]
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, mode, None),
                status_msg, meta_caption, state, cancel_event
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=True)

        elif query.data == "v_instant":
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", None),
                status_msg, meta_caption, state, cancel_event
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False)

    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ **حدث خطأ أثناء التحميل:**\n`{str(e)[:150]}`", parse_mode='Markdown')
        except Exception:
            pass
    finally:
        context.user_data.pop("active_cancel_event", None)

def main():
    request = HTTPXRequest(connect_timeout=30, read_timeout=180, write_timeout=180)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل وجاهز لمعالجة وإعادة ضغط الفيديوهات...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
