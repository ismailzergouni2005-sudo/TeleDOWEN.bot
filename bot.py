import os
import re
import time
import shutil
import logging
import asyncio
import threading

import static_ffmpeg
static_ffmpeg.add_paths()

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
import yt_dlp

# --- إعداد خادم Flask للبقاء حياً على Render ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

logging.basicConfig(level=logging.INFO)

# --- البيانات الحساسة من متغيرات البيئة ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))      # احصل عليه من my.telegram.org
API_HASH = os.environ.get("API_HASH")          # احصل عليه من my.telegram.org

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("❌ يرجى ضبط BOT_TOKEN و API_ID و API_HASH في متغيرات البيئة.")

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
async def start_cmd(client, message: Message):
    await message.reply_text("⚡ أهلاً بك! أرسل لي أي رابط فيديو وسأقوم بتحميله لك حتى حجم 2GB كفيديو عادي.")

@bot.on_message(filters.text & ~filters.command(["start"]))
async def handle_url(client, message: Message):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.reply_text("❌ يرجى إرسال رابط صحيح.")
        return

    status_msg = await message.reply_text("⏳ جاري جلب المقطع وتحميله...")
    dest_template = f"{DOWNLOAD_DIR}/{message.id}_%(id)s.%(ext)s"

    try:
        # التحميل في خلفية منفصلة
        loop = asyncio.get_running_loop()
        filepath, info = await loop.run_in_executor(None, download_video, url, dest_template)
        
        size_mb, size_str = format_file_size(filepath)
        await status_msg.edit_text(f"⬆️ جاري الرفع إلى تليجرام...\n💾 الحجم: {size_str}")

        # دالة الرفع التابعة لـ Pyrogram تتيح حتى 2000MB (2GB) كفيديو
        await client.send_video(
            chat_id=message.chat.id,
            video=filepath,
            caption=f"✅ **تم التحميل بنجاح!**\n🎬 {info.get('title', 'فيديو')}\n💾 الحجم: {size_str}",
            supports_streaming=True
        )
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ حدث خطأ أثناء العملية:\n`{str(e)[:150]}`")
    finally:
        if 'filepath' in locals() and os.path.exists(filepath):
            os.remove(filepath)

print("🚀 البوت يعمل بنجاح مع دعم رفع حتى 2GB...")
bot.run()
