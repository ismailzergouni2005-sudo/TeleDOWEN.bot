import os
import re
import time
import shutil
import logging
import asyncio
import threading

# --- تفعيل مكتبة static-ffmpeg لتجهيز المحول تلقائياً ---
import static_ffmpeg
static_ffmpeg.add_paths()
# ----------------------------------------------------

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message
import yt_dlp

# --- إعداد خادم Flask لإبقاء البوت نشطاً على Render ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

logging.basicConfig(level=logging.INFO)

# --- قراءة متغيرات البيئة بآمان ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_HASH = os.environ.get("API_HASH")
raw_api_id = os.environ.get("API_ID")

if not BOT_TOKEN or not API_HASH or not raw_api_id:
    logging.error("❌ خطأ: يرجى إضافة BOT_TOKEN و API_HASH و API_ID في إعدادات Render (Environment Variables)!")
    exit(1)

try:
    API_ID = int(raw_api_id)
except ValueError:
    logging.error("❌ خطأ: يجب أن يكون API_ID رقماً فقط بدون حروف!")
    exit(1)

# إنشاء جلسة العميل الخاصة بـ Pyrogram
bot = Client("my_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg")

def format_file_size(filepath):
    if os.path.exists(filepath):
        size_bytes = os.path.getsize(filepath)
        size_mb = size_bytes / (1024 * 1024)
        return size_mb, f"{size_mb:.1f} MB"
    return 0, "0 MB"

def download_video(url, dest_template):
    ydl_opts = {
        "quiet": True,
        "outtmpl": dest_template,
        "format": "bv*+ba/b/best",
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info

@bot.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "⚡ **أهلاً بك في بوت التحميل السريع!**\n\n"
        "أرسل لي أي رابط فيديو (TikTok, Instagram, YouTube, Facebook...) "
        "وسأقوم بتحميله وإرساله لك كفيديو بدعم حجم يصل حتى **2GB**."
    )

@bot.on_message(filters.text & ~filters.command(["start"]))
async def handle_url(client: Client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.reply_text("❌ يرجى إرسال رابط صحيح يبتدئ بـ http أو https.")
        return

    status_msg = await message.reply_text("⏳ جاري تحليل الرابط وبدء التحميل...")
    dest_template = f"{DOWNLOAD_DIR}/{message.id}_%(id)s.%(ext)s"
    filepath = None

    try:
        # تشغيل التنزيل في مسار منفصل لمنع تجميد البوت
        loop = asyncio.get_running_loop()
        filepath, info = await loop.run_in_executor(None, download_video, url, dest_template)
        
        size_mb, size_str = format_file_size(filepath)

        # التحقق من أن حجم الملف لا يتجاوز الحد الأقصى المطلق لتليجرام (2GB)
        if size_mb > 2000:
            await status_msg.edit_text(f"❌ **عذراً، حجم الملف ({size_str}) يتجاوز الحد الأقصى المسموح به في تليجرام (2000MB).**")
            return

        await status_msg.edit_text(f"⬆️ **جاري رفع الفيديو إلى تليجرام...**\n💾 **الحجم:** `{size_str}`")

        # رفع الفيديو باستخدام Pyrogram لدعم حتى 2GB
        await client.send_video(
            chat_id=message.chat.id,
            video=filepath,
            caption=(
                f"🎬 <b>{info.get('title', 'فيديو')}</b>\n"
                f"💾 <b>الحجم:</b> {size_str}\n\n"
                f"✅ <i>تم التحميل بنجاح!</i>"
            ),
            supports_streaming=True
        )
        await status_msg.delete()

    except Exception as e:
        logging.error("Download/Upload Error:", exc_info=True)
        await status_msg.edit_text(f"❌ **حدث خطأ أثناء العملية:**\n`{str(e)[:150]}`")
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)

if __name__ == '__main__':
    print("🚀 جاري تشغيل البوت مع دعم الرفع حتى 2GB...")
    bot.run()
