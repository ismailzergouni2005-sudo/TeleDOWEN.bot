import os
import re
import glob
import logging
import time
import asyncio
import threading
from flask import Flask
import imageio_ffmpeg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp

# 🌐 سيرفر وهمي صغير لفتح Port وإرضاء Render مجانياً
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# تشغيل سيرفر الويب في خلفية النظام
threading.Thread(target=run_web, daemon=True).start()

# 🎯 مسار FFmpeg المباشر
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = "8846997512:AAFfc2HSrJHWmXHfiEMO_M5I4F-OPc3zrrk"
MAX_SIZE = 50 * 1024 * 1024  # 50MB

COMMON_YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'ffmpeg_location': FFMPEG_PATH,
    'socket_timeout': 30,
    'retries': 5,
    'fragment_retries': 5,
    'extractor_args': {
        'youtube': {
            'player_client': ['ios', 'android', 'web']
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
}

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    elif "tiktok.com" in url and "?" in url and not ("vt.tiktok" in url or "vm.tiktok" in url):
        return url.split('?')[0]
    return url

def format_size(bytes_size):
    if not bytes_size:
        return "غير محدد"
    mb = bytes_size / (1024 * 1024)
    return f"{mb:.1f} MB"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "✨ **مرحباً بك في بوت التحميل الشامل!** 🚀\n\n"
        "أنا هنا لمساعدتك في تحميل الفيديوهات والصوتيات بأعلى جودة ممكنة ومن مختلف المنصات بكل سهولة.\n\n"
        "🌐 **المنصات المدعومة:**\n"
        "• 🎬 يوتيوب (YouTube)\n"
        "• 📸 إنستجرام (Instagram)\n"
        "• 🎵 تيك توك (TikTok)\n"
        "• 📘 فيسبوك (Facebook)\n\n"
        "⚙️ **كيفية الاستخدام:**\n"
        "1️⃣ أرسل رابط الفيديو هنا.\n"
        "2️⃣ اختر الجودة أو الصيغة المناسبة.\n"
        "3️⃣ سيتم إرسال الملف لك مباشرة! 📥"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        await update.message.reply_text("❌ يرجى إرسال رابط صحيح (يجب أن يبدأ بـ http:// أو https://).")
        return

    raw_url = url_match.group(0)
    url = clean_url(raw_url)
    context.user_data['download_url'] = url

    is_single_quality_platform = any(p in url for p in ["tiktok.com", "instagram.com"])

    if is_single_quality_platform:
        platform_name = "TikTok 🎵" if "tiktok.com" in url else "Instagram 📸"
        
        info_message = (
            f"📌 **ملاحظة حول مقاطع ({platform_name}):**\n"
            "هذا المقطع يتاح بأعلى جودة متوفرة فقط ولا يدعم التحميل بجودات متعددة.\n\n"
            "👇 **اختر نوع التحميل المطلوب:**"
        )

        keyboard = [
            [
                InlineKeyboardButton("📥 تحميل بأعلى جودة", callback_data="v_best"),
            ],
            [
                InlineKeyboardButton("🎵 تحميل MP3 (صوت فقط)", callback_data="a_128"),
            ],
            [
                InlineKeyboardButton("❌ إلغاء الطلب", callback_data="action_cancel"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(info_message, reply_markup=reply_markup, parse_mode='Markdown')
        return

    status_msg = await update.message.reply_text("🔍 جاري جلب الجودات ومعلومات الفيديو... ⏳")
    sizes = {}
    loop = asyncio.get_event_loop()

    def extract_info():
        opts = COMMON_YDL_OPTS.copy()
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, extract_info)
        formats = info.get('formats', [])
        for f in formats:
            height = f.get('height')
            filesize = f.get('filesize') or f.get('filesize_approx')
            if height and filesize and height not in sizes:
                sizes[height] = filesize
    except Exception as e:
        logging.warning(f"تعذر استخراج الأبعاد تلقائياً: {e}")

    def get_s(res_target):
        for h, sz in sizes.items():
            if abs(h - res_target) <= 40:
                return f" ({format_size(sz)})"
        return ""

    keyboard = [
        [
            InlineKeyboardButton(f"📹 1080p{get_s(1080)}", callback_data="v_1080"),
            InlineKeyboardButton(f"📹 720p{get_s(720)}", callback_data="v_720"),
        ],
        [
            InlineKeyboardButton(f"📹 480p{get_s(480)}", callback_data="v_480"),
            InlineKeyboardButton(f"📹 360p{get_s(360)}", callback_data="v_360"),
        ],
        [
            InlineKeyboardButton(f"📹 240p{get_s(240)}", callback_data="v_240"),
            InlineKeyboardButton(f"📹 144p{get_s(144)}", callback_data="v_144"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 - 128kbps", callback_data="a_128"),
            InlineKeyboardButton("🎵 MP3 - 64kbps", callback_data="a_64"),
        ],
        [
            InlineKeyboardButton("🔙 رجوع للخلف", callback_data="action_back"),
            InlineKeyboardButton("❌ إلغاء الطلب", callback_data="action_cancel"),
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.edit_text("👇 **اختر الجودة أو الصيغة المطلوبة:**", reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data

    if choice == "action_cancel":
        context.user_data.pop('download_url', None)
        await query.edit_message_text("❌ **تم إلغاء عملية التحميل.**", parse_mode='Markdown')
        return

    if choice == "action_back":
        context.user_data.pop('download_url', None)
        await query.edit_message_text("🔙 **تم الرجوع.** يمكنك الآن إرسال رابط جديد لتحميله.", parse_mode='Markdown')
        return

    url = context.user_data.get('download_url')
    if not url:
        await query.edit_message_text("❌ انتهت صلاحية الطلب، يرجى إرسال الرابط مجدداً.")
        return

    ydl_opts = COMMON_YDL_OPTS.copy()
    ydl_opts['outtmpl'] = 'downloads/%(id)s.%(ext)s'

    is_audio = False

    if choice == "v_best":
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'

    elif choice.startswith("v_"):
        height = choice.split("_")[1]
        ydl_opts['format'] = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best'
        ydl_opts['merge_output_format'] = 'mp4'

    elif choice.startswith("a_"):
        is_audio = True
        abr = choice.split("_")[1]
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': abr,
        }]

    last_update_time = [0]
    loop = asyncio.get_running_loop()

    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time[0] >= 2.0:
                last_update_time[0] = current_time
                
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)

                if total > 0:
                    percent = downloaded / total
                    percent_int = int(percent * 100)
                    
                    filled_length = int(10 * percent)
                    bar = '🟩' * filled_length + '⬜' * (10 - filled_length)
                    
                    text = (
                        f"⏳ **جاري التحميل...**\n\n"
                        f"{bar} **{percent_int}%**\n\n"
                        f"🚀 *يرجى الانتظار لحين إرسال الملف...*"
                    )
                    
                    asyncio.run_coroutine_threadsafe(
                        query.edit_message_text(text, parse_mode='Markdown'),
                        loop
                    )

    ydl_opts['progress_hooks'] = [progress_hook]

    await query.edit_message_text("⏳ **جاري التحميل...**\n\n⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ **0%**", parse_mode='Markdown')

    filename = None
    try:
        def run_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info.get('id')

        video_id = await loop.run_in_executor(None, run_download)

        matches = glob.glob(f'downloads/{video_id}.*')
        if not matches:
            await query.edit_message_text("❌ لم يتم العثور على الملف بعد التحميل.")
            return
        filename = matches[0]

        if os.path.getsize(filename) > MAX_SIZE:
            os.remove(filename)
            await query.edit_message_text("❌ حجم الملف المختار يتجاوز 50 ميجابايت (حد تليجرام الأقصى).")
            return

        await query.edit_message_text("📤 **جاري الرفع إلى تليجرام...**")

        with open(filename, 'rb') as file_data:
            if is_audio:
                await query.message.reply_audio(
                    audio=file_data, 
                    caption="تم تحميل الصوت بنجاح! 🎵",
                    read_timeout=300,
                    write_timeout=300
                )
            else:
                await query.message.reply_video(
                    video=file_data, 
                    caption="تم تحميل الفيديو بنجاح! ✨",
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300
                )

        os.remove(filename)
        await query.delete_message()

    except Exception as e:
        if filename and os.path.exists(filename):
            os.remove(filename)
        
        err_msg = str(e)
        clean_err = "❌ تعذر تحميل المقطع من المنصة حالياً، يرجى المحاولة لاحقاً."
        
        if "Sign in to confirm" in err_msg:
            clean_err = "❌ يوتيوب يتطلب تأكيد الحساب لهذا المقطع على السيرفر الخارجي."
        elif "Gateway Timeout" in err_msg:
            clean_err = "❌ تعذر الاتصال بالمنصة (تأخير في الاستجابة). يرجى المحاولة بعد قليل."

        try:
            await query.edit_message_text(clean_err, disable_web_page_preview=True)
        except:
            pass

def main():
    if not os.path.exists('downloads'):
        os.makedirs('downloads')

    request = HTTPXRequest(
        connect_timeout=300,
        read_timeout=300,
        write_timeout=300,
        pool_timeout=300,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل الآن بشكل كامل خالي من الأخطاء...")
    app.run_polling()

if __name__ == '__main__':
    while True:
        try:
            main()
        except Exception as e:
            logging.error(f"البوت توقف بسبب خطأ: {e}")
            print("إعادة تشغيل البوت خلال 5 ثوانٍ...")
            time.sleep(5)
