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

# معرف الملصق المتحرك الترحيبي الخاص بك
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

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

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

def build_meta_caption(uploader=None, uploader_id=None, duration=None, views=None, title=None, description=None, quality=None):
    lines = []
    
    if uploader_id:
        uploader_link = f'<a href="https://instagram.com/{uploader_id}">{uploader or uploader_id}</a>'
        lines.append(f"👤 الحساب: {uploader_link}")
    elif uploader:
        lines.append(f"👤 الحساب: <b>{uploader}</b>")
    else:
        lines.append("👤 الحساب: غير معروف")

    text_content = description or title
    if text_content:
        clean_text = text_content.strip().split('\n')[0]
        short_text = clean_text if len(clean_text) <= 80 else clean_text[:77] + "..."
        lines.append(f"📝 الوصف: {short_text}")
        
    formatted_duration = format_duration(duration)
    if formatted_duration:
        lines.append(f"⏱ المدة: {formatted_duration}")
        
    formatted_views = format_count(views)
    if formatted_views and views != 0:
        lines.append(f"👁 المشاهدات: {formatted_views}")
        
    if quality:
        lines.append(f"🎬 الجودة/الصيغة: {quality}")
        
    return "\n".join(lines)

# ---------------- تجميع خيارات الجودة ----------------

def get_available_options(info):
    formats = info.get("formats") or []
    candidates = {}

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

        prev = candidates.get(h)
        if prev is None or (ext == "mp4" and prev.get("ext") != "mp4"):
            candidates[h] = {"ext": ext}

    if not candidates:
        height_from_info = info.get("height")
        if height_from_info:
            candidates[height_from_info] = {"ext": "mp4"}
        else:
            return {1080: {"ext": "mp4"}, 720: {"ext": "mp4"}, 480: {"ext": "mp4"}}

    return candidates

def build_quality_keyboard(video_opts):
    rows = []
    heights = sorted(video_opts.keys(), reverse=True)[:6]

    buttons = []
    for h in heights:
        ext = video_opts[h]["ext"].upper()
        label = f"🎬 {h}p ({ext})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"q_{h}"))

    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])

    rows.append([
        InlineKeyboardButton("🎵 MP3 (صوت فقط)", callback_data="a_mp3")
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

# ---------------- منطق التحميل والدمج مع الصوت ----------------

def extract_info_only(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

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

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": dest_template,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 5,
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
    elif height:
        ydl_opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        ydl_opts["merge_output_format"] = "mp4"
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

        if mode == "mp3":
            base, _ = os.path.splitext(filename)
            filename = base + ".mp3"

        final_file = filename
        if not os.path.exists(final_file):
            base_no_ext, _ = os.path.splitext(filename)
            directory = os.path.dirname(base_no_ext) or "."
            base_name = os.path.basename(base_no_ext)
            for fname in os.listdir(directory):
                if fname.startswith(base_name):
                    final_file = os.path.join(directory, fname)
                    break

        return final_file, info

# ---------------- إدارة البث والتحديث ----------------

async def edit_progress_message(status_msg, text, reply_markup=None):
    try:
        await status_msg.edit_caption(caption=text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception:
        try:
            await status_msg.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
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

            text = f"{meta_caption}\n\n{SPINNER_FRAMES[frame]} جاري التحميل والمعالجة...\n{body}"

            now = time.time()
            if text != last_text and (now - last_update) >= 2.0:
                last_text = text
                last_update = now
                await edit_progress_message(status_msg, text, reply_markup=build_cancel_keyboard())

            await asyncio.sleep(0.8)
    except asyncio.CancelledError:
        pass

async def run_with_progress(func, args, status_msg, meta_caption, state, cancel_event, timeout=240):
    loop = asyncio.get_running_loop()
    ticker = asyncio.create_task(progress_ticker(status_msg, meta_caption, state, cancel_event))
    start_time = time.time()
    try:
        download_task = loop.run_in_executor(None, func, *args)
        while not download_task.done():
            if cancel_event.is_set():
                download_task.cancel()
                raise asyncio.CancelledError("تم الإلغاء بواسطة المستخدم.")
            if time.time() - start_time > timeout:
                cancel_event.set()
                raise asyncio.TimeoutError("انتهت المهلة الزمنية للتحميل.")
            await asyncio.sleep(0.3)
        return await download_task
    finally:
        ticker.cancel()
        try:
            await ticker
        except Exception:
            pass

async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, is_audio=False):
    size_mb, file_size_str = format_file_size(filepath)
    
    if size_mb > 50:
        await edit_progress_message(
            status_msg,
            f"{meta_caption}\n\n❌ **حجم الملف كبير جداً ({file_size_str})**.\nحد التحميل المسموح للبوتات هو 50MB.",
            reply_markup=build_reselect_keyboard()
        )
        if os.path.exists(filepath):
            os.remove(filepath)
        return

    size_line = f"\n💾 الحجم: {file_size_str}" if file_size_str else ""
    caption = f"{meta_caption}{size_line}\n\n✅ تم التحميل بنجاح!"

    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(), read_timeout=120
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(), supports_streaming=True, read_timeout=120
                )
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
        try:
            await status_msg.delete()
        except Exception:
            pass

# ---------------- معالجات الأوامر والرسالة الترحيبية المميزة ----------------

def build_welcome_message(user):
    name = user.first_name or "المستخدم"
    user_id = user.id
    
    blue_user_link = f'<a href="tg://user?id={user_id}">« {name} »</a>'

    return (
        f"✨ أهلاً وسهلاً بك يا ✦ {blue_user_link} ✦\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote><b> ⚡ مرحباً بك في بوت التحميل السريع! ⚡ </b></blockquote>\n\n"
        f"أنا هنا لمساعدتك في تحميل الفيديوهات والمقاطع الصوتية بأعلى جودة ممكنة.\n\n"
        f"🌐 <b>المنصات المدعومة:</b>\n"
        f"├ 🎵 <b>TikTok</b>\n"
        f"├ 📸 <b>Instagram</b>\n"
        f"├ ▶️ <b>YouTube</b>\n"
        f"├ 📌 <b>Pinterest</b>\n"
        f"├ 👍 <b>Facebook</b>\n"
        f"└ 🐦 <b>X (Twitter)</b>\n\n"
        f"⚡ <i>كل ما عليك هو إرسال رابط الفيديو الآن!</i>"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. إرسال الرسالة الترحيبية المزخرفة
    await update.message.reply_text(
        build_welcome_message(update.effective_user),
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    
    # 2. إرسال الملصق المتحرك بعدها مباشرة
    try:
        await update.message.reply_sticker(sticker=WELCOME_STICKER_ID)
    except Exception as e:
        logging.error(f"فشل إرسال الملصق الترحيبي: {e}")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.search(r'https?://[^\s]+', text)
    if not match:
        await update.message.reply_text("❌ يرجى إرسال رابط صحيح.")
        return

    url = clean_url(match.group(0))
    context.user_data['download_url'] = url

    checking_msg = await update.message.reply_text("🔍 جاري فحص الرابط والجودات...")
    try:
        info = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, extract_info_only, url),
            timeout=25
        )
        context.user_data['ytdlp_info'] = info
        video_opts = get_available_options(info)
        context.user_data['video_opts'] = video_opts

        await checking_msg.edit_text(
            "👇 **اختر الصيغة والجودة المطلوبة:**",
            reply_markup=build_quality_keyboard(video_opts),
            parse_mode='Markdown'
        )
    except asyncio.TimeoutError:
        await checking_msg.edit_text("⏱ استغرق فحص الرابط وقتاً طويلاً. أعد المحاولة لاحقاً.")
    except Exception as e:
        await checking_msg.edit_text(f"❌ تعذر تحليل الرابط:\n`{str(e)[:100]}`", parse_mode='Markdown')

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
        await query.edit_message_text("🛑 تم إلغاء عملية التحميل.")
        return

    if query.data == "reselect_format":
        video_opts = context.user_data.get('video_opts') or {}
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
    elif query.data == "v_instant":
        quality_str = "أفضل جودة متاحة"

    meta_caption = build_meta_caption(
        uploader=info.get("uploader") or info.get("channel"),
        uploader_id=info.get("uploader_id") or info.get("channel_id"),
        duration=info.get("duration"),
        views=info.get("view_count"),
        title=info.get("title"),
        description=info.get("description"),
        quality=quality_str
    )

    cancel_event = asyncio.Event()
    context.user_data["active_cancel_event"] = cancel_event

    status_msg = await query.edit_message_text(f"{meta_caption}\n\n⏳ جاري بدء التحميل...", parse_mode='HTML', reply_markup=build_cancel_keyboard())

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

    except (asyncio.CancelledError, RuntimeError):
        pass
    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ **حدث خطأ أثناء التحميل:**\n`{str(e)[:120]}`", parse_mode='Markdown')
        except Exception:
            pass
    finally:
        context.user_data.pop("active_cancel_event", None)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("حدث خطأ:", exc_info=context.error)

def main():
    request = HTTPXRequest(connect_timeout=20, read_timeout=120, write_timeout=120)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    print("🚀 البوت يعمل...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
