import os
import re
import sys
import time
import shutil
import logging
import asyncio
import threading
import subprocess

logging.basicConfig(level=logging.INFO)

# --- تحديث yt-dlp تلقائياً قبل الاستيراد ---
# يوتيوب يغيّر آلياته باستمرار، ونسخة yt-dlp قديمة = فشل متكرر بأخطاء مثل
# "Sign in to confirm you're not a bot". نحاول تحديثها لآخر إصدار عند كل تشغيل.
def update_yt_dlp():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0:
            logging.info("✅ yt-dlp تم فحصه/تحديثه بنجاح.")
        else:
            logging.warning(f"⚠️ فشل تحديث yt-dlp: {result.stderr[:300]}")
    except Exception as e:
        logging.warning(f"⚠️ تعذر تحديث yt-dlp: {e}")

update_yt_dlp()
# ----------------------------------------

# --- تفعيل مكتبة static-ffmpeg تلقائياً ---
import static_ffmpeg
static_ffmpeg.add_paths()
# ----------------------------------------

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

def periodic_yt_dlp_update(interval_hours=12):
    """يعيد فحص التحديث دورياً؛ يُطبَّق فعلياً بعد إعادة تشغيل العملية القادمة
    (Render/Railway تعيد التشغيل بشكل دوري، فهذا يمنع بقاء نسخة قديمة طويلاً)."""
    while True:
        time.sleep(interval_hours * 3600)
        update_yt_dlp()

threading.Thread(target=periodic_yt_dlp_update, daemon=True).start()

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ لم يتم تعيين متغير البيئة BOT_TOKEN.")

WELCOME_IMAGE_URL = "https://files.catbox.moe/heevbw.jpg"
WELCOME_STICKER_ID = "CAACAgIAAxkBAAEtNrJqciCsb_KyhKNta-pPJzCKUefSigACVAADQbVWDGq3-McIjQH6PQQ"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# سقف حجم الملف: 50MB افتراضياً (حد Telegram Bot API القياسي)، أو 2000MB (2GB)
# تلقائياً إذا تم ضبط LOCAL_BOT_API_URL لاستخدام سيرفر Bot API محلي.
MAX_UPLOAD_MB = 2000 if os.environ.get("LOCAL_BOT_API_URL") else 50

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = None

# سبينر دائري حقيقي: عقارب ساعة تدور بشكل متصل (12 إطار)، يعطي شكل دائرة تدور فعلياً
# كأنها مؤشر بحث/تحميل (بدل الـ braille السابق الذي شكله نقاط وليس دائرة)
SPINNER_FRAMES = ["🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛"]

# ---------------- النصوص متعددة اللغات ----------------

TEXTS = {
    "ar": {
        "welcome": (
            "✨ أهلاً وسهلاً بك يا ✦ {user_link} ✦\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote><b> ⚡ مرحباً بك في بوت التحميل السريع! ⚡ </b></blockquote>\n\n"
            "أنا هنا لمساعدتك في تحميل الفيديوهات والمقاطع الصوتية بأعلى جودة ممكنة.\n\n"
            "🌐 <b><u>المنصات المدعومة حالياً:</u></b>\n\n"
            "<code> ▌ T I K T O K </code>\n"
            "<code> ▌ I N S T A G R A M </code>\n"
            "<code> ▌ P I N T E R E S T </code>\n"
            "<code> ▌ F A C E B O O K </code>\n"
            "<code> ▌ X ( T W I T T E R ) </code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <i>كل ما عليك هو إرسال رابط الفيديو الآن!</i>"
        ),
        "invalid_url": "❌ يرجى إرسال رابط صحيح.",
        "checking": "🔍 جاري فحص الرابط والجودات...",
        "choose_format": "👇 **اختر الصيغة والجودة المطلوبة:**",
        "timeout": "⏱ استغرق فحص الرابط وقتاً طويلاً. أعد المحاولة لاحقاً.",
        "analyze_error": "❌ تعذر تحليل الرابط:\n`{error}`",
        "youtube_unsupported": "⚠️ عذراً، منصة يوتيوب غير مدعومة حالياً في البوت.\nيرجى استخدام رابط من: تيك توك، إنستغرام، بينتيريست، فيسبوك، أو إكس (تويتر).",
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
        "too_large": "❌ **حجم الملف كبير جداً ({size})**.\nحد الرفع الحالي للبوت هو {cap}MB.",
        "success": "✅ تم التحميل بنجاح!",
        "size": "💾 الحجم",
        "download_error": "❌ **حدث خطأ أثناء التحميل:**\n`{error}`"
    },
    "en": {
        "welcome": (
            "✨ Welcome ✦ {user_link} ✦\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote><b> ⚡ Welcome to Fast Downloader Bot! ⚡ </b></blockquote>\n\n"
            "I'm here to help you download videos and audio clips in the highest possible quality.\n\n"
            "🌐 <b><u>Currently Supported Platforms:</u></b>\n\n"
            "<code> ▌ T I K T O K </code>\n"
            "<code> ▌ I N S T A G R A M </code>\n"
            "<code> ▌ P I N T E R E S T </code>\n"
            "<code> ▌ F A C E B O O K </code>\n"
            "<code> ▌ X ( T W I T T E R ) </code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <i>Just send any video link now!</i>"
        ),
        "invalid_url": "❌ Please send a valid link.",
        "checking": "🔍 Checking link and qualities...",
        "choose_format": "👇 **Select the desired format and quality:**",
        "timeout": "⏱ Checking took too long. Please try again later.",
        "analyze_error": "❌ Failed to analyze link:\n`{error}`",
        "youtube_unsupported": "⚠️ Sorry, YouTube is not supported by this bot right now.\nPlease use a link from: TikTok, Instagram, Pinterest, Facebook, or X (Twitter).",
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
        "too_large": "❌ **File size too large ({size})**.\nCurrent bot upload limit is {cap}MB.",
        "success": "✅ Downloaded successfully!",
        "size": "💾 Size",
        "download_error": "❌ **An error occurred during download:**\n`{error}`"
    }
}

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

def is_youtube(url: str) -> bool:
    return bool(re.search(r'(youtube\.com|youtu\.be)', url))

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

def build_meta_caption(lang, uploader=None, uploader_id=None, duration=None, views=None, title=None, description=None, quality=None):
    t = TEXTS[lang]
    lines = []
    
    if uploader_id:
        uploader_link = f'<a href="https://instagram.com/{uploader_id}">{uploader or uploader_id}</a>'
        lines.append(f"{t['account']}: {uploader_link}")
    elif uploader:
        lines.append(f"{t['account']}: <b>{uploader}</b>")
    else:
        lines.append(f"{t['account']}: {t['unknown']}")

    text_content = description or title
    if text_content:
        clean_text = text_content.strip().split('\n')[0]
        short_text = clean_text if len(clean_text) <= 80 else clean_text[:77] + "..."
        lines.append(f"{t['description']}: {short_text}")
        
    formatted_duration = format_duration(duration)
    if formatted_duration:
        lines.append(f"{t['duration']}: {formatted_duration}")
        
    formatted_views = format_count(views)
    if formatted_views and views != 0:
        lines.append(f"{t['views']}: {formatted_views}")
        
    if quality:
        lines.append(f"{t['quality']}: {quality}")
        
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

def build_quality_keyboard(lang, video_opts):
    t = TEXTS[lang]
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
        InlineKeyboardButton(t["audio_only"], callback_data="a_mp3")
    ])
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

# ---------------- خيارات الهيدرز وتخطي الحظر ----------------

INSTAGRAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

def get_base_opts(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 5,
        "http_headers": INSTAGRAM_HEADERS if "instagram.com" in url else {},
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    return ydl_opts

def extract_info_only(url):
    ydl_opts = get_base_opts(url)
    ydl_opts["skip_download"] = True
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

    ydl_opts = get_base_opts(url)
    ydl_opts["outtmpl"] = dest_template
    ydl_opts["progress_hooks"] = [hook]

    if mode == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif height:
        ydl_opts["format"] = f"bv*[height<={height}]+ba/b[height<={height}]/best"
    else:
        ydl_opts["format"] = "bv*+ba/b/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

        if mode == "mp3":
            base, _ = os.path.splitext(filename)
            if os.path.exists(base + ".mp3"):
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

async def progress_ticker(status_msg, meta_caption, state, cancel_event, lang, spinner_msg=None):
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

            text = f"{meta_caption}\n\n{t['processing']}\n{body}"

            now = time.time()
            if (text != last_text or spinner_msg) and (now - last_update) >= 1.5:
                last_text = text
                last_update = now
                await edit_progress_message(status_msg, text, reply_markup=build_cancel_keyboard(lang))
                # رسالة منفصلة تحتوي على رمز الساعة فقط بدون أي نص آخر —
                # تيليجرام يكبّر تلقائياً الرسائل المكوّنة من إيموجي واحد فقط (حتى 3)،
                # وهذه هي الطريقة الوحيدة لعرض الساعة بحجم أكبر داخل بوت (لا يوجد تحكم بحجم الخط في الـ Bot API).
                if spinner_msg:
                    try:
                        await spinner_msg.edit_text(SPINNER_FRAMES[frame])
                    except Exception:
                        pass

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass

async def run_with_progress(func, args, status_msg, meta_caption, state, cancel_event, lang, timeout=180, spinner_msg=None):
    loop = asyncio.get_running_loop()
    ticker = asyncio.create_task(progress_ticker(status_msg, meta_caption, state, cancel_event, lang, spinner_msg))
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

async def send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False):
    t = TEXTS[lang]
    size_mb, file_size_str = format_file_size(filepath)
    
    if size_mb > MAX_UPLOAD_MB:
        await edit_progress_message(
            status_msg,
            f"{meta_caption}\n\n{t['too_large'].format(size=file_size_str, cap=MAX_UPLOAD_MB)}",
            reply_markup=build_reselect_keyboard(lang)
        )
        if os.path.exists(filepath):
            os.remove(filepath)
        return

    size_line = f"\n{t['size']}: {file_size_str}" if file_size_str else ""
    caption = f"{meta_caption}{size_line}\n\n{t['success']}"

    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(lang), read_timeout=120
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f, caption=caption,
                    parse_mode='HTML', reply_markup=build_reselect_keyboard(lang), supports_streaming=True, read_timeout=120
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
        "🌐 **الرجاء اختيار اللغة / Please select your language:**",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def send_welcome(query, context: ContextTypes.DEFAULT_TYPE, lang: str):
    t = TEXTS[lang]
    user = query.from_user
    name = user.first_name or "User"
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

    text = update.message.text.strip()
    match = re.search(r'https?://[^\s]+', text)
    if not match:
        await update.message.reply_text(t["invalid_url"])
        return

    url = clean_url(match.group(0))

    # يوتيوب غير مدعوم حالياً بسبب حظر يوتيوب المتكرر لطلبات السيرفرات (راجع الشرح في اللوجات).
    # بدل محاولة التحليل والفشل، نخبر المستخدم فوراً بوضوح.
    if is_youtube(url):
        await update.message.reply_text(t["youtube_unsupported"])
        return

    context.user_data['download_url'] = url

    checking_msg = await update.message.reply_text(t["checking"])
    try:
        info = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, extract_info_only, url),
            timeout=25
        )
        context.user_data['ytdlp_info'] = info
        video_opts = get_available_options(info)
        context.user_data['video_opts'] = video_opts

        await checking_msg.edit_text(
            t["choose_format"],
            reply_markup=build_quality_keyboard(lang, video_opts),
            parse_mode='Markdown'
        )
    except asyncio.TimeoutError:
        await checking_msg.edit_text(t["timeout"])
    except Exception as e:
        await checking_msg.edit_text(t["analyze_error"].format(error=str(e)[:100]), parse_mode='Markdown')

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
        await query.edit_message_text(t["cancelled"])
        return

    if query.data == "reselect_format":
        video_opts = context.user_data.get('video_opts') or {}
        await query.message.reply_text(
            t["choose_format"],
            reply_markup=build_quality_keyboard(lang, video_opts),
            parse_mode='Markdown'
        )
        return

    url = context.user_data.get('download_url')
    if not url:
        await query.edit_message_text(t["expired"])
        return

    info = context.user_data.get('ytdlp_info') or {}

    quality_str = None
    if query.data.startswith("q_"):
        height = query.data.split("_")[1]
        quality_str = f"{height}p"
    elif query.data == "a_mp3":
        quality_str = t["audio_only"]
    elif query.data == "v_instant":
        quality_str = t["best_quality"]

    meta_caption = build_meta_caption(
        lang=lang,
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

    status_msg = await query.edit_message_text(f"{meta_caption}\n\n{t['downloading']}", parse_mode='HTML', reply_markup=build_cancel_keyboard(lang))

    chat_id = query.message.chat_id
    bot = context.bot
    dest_template = f"{DOWNLOAD_DIR}/%(id)s_{status_msg.message_id}.%(ext)s"
    state = {"percent": None, "downloaded": 0}

    # رسالة منفصلة للساعة الدوارة فقط، تظهر بحجم مكبّر تلقائياً (راجع الشرح في progress_ticker)
    spinner_msg = None
    try:
        spinner_msg = await bot.send_message(chat_id=chat_id, text=SPINNER_FRAMES[0])
    except Exception:
        spinner_msg = None

    try:
        if query.data.startswith("q_"):
            height = int(query.data.split("_")[1])
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", height),
                status_msg, meta_caption, state, cancel_event, lang, spinner_msg=spinner_msg
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False)

        elif query.data.startswith("a_"):
            mode = query.data.split("_")[1]
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, mode, None),
                status_msg, meta_caption, state, cancel_event, lang, spinner_msg=spinner_msg
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=True)

        elif query.data == "v_instant":
            filepath, _ = await run_with_progress(
                yt_dlp_download_one_pass, (url, dest_template, state, cancel_event, "video", None),
                status_msg, meta_caption, state, cancel_event, lang, spinner_msg=spinner_msg
            )
            await send_final_file(bot, chat_id, status_msg, filepath, meta_caption, lang, is_audio=False)

    except (asyncio.CancelledError, RuntimeError):
        pass
    except Exception as e:
        try:
            await status_msg.edit_text(t["download_error"].format(error=str(e)[:120]), parse_mode='Markdown')
        except Exception:
            pass
    finally:
        context.user_data.pop("active_cancel_event", None)
        if spinner_msg:
            try:
                await spinner_msg.delete()
            except Exception:
                pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("حدث خطأ:", exc_info=context.error)

def main():
    request = HTTPXRequest(connect_timeout=15, read_timeout=100, write_timeout=100)
    builder = Application.builder().token(TOKEN).request(request).concurrent_updates(True)

    # سيرفر Bot API محلي (self-hosted) يرفع سقف الرفع من 50MB إلى ~2GB.
    # فعّله بضبط متغير البيئة LOCAL_BOT_API_URL على عنوان السيرفر المحلي، مثل:
    # http://localhost:8081  (راجع telegram-bot-api / tdlib على GitHub لتشغيله)
    local_api_url = os.environ.get("LOCAL_BOT_API_URL")
    if local_api_url:
        builder = builder.base_url(f"{local_api_url}/bot").base_file_url(f"{local_api_url}/file/bot")
        logging.info(f"✅ استخدام سيرفر Bot API محلي: {local_api_url} — سقف الرفع أصبح ~2GB.")

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    print("🚀 البوت يعمل...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
