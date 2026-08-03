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

# 🌐 سيرفر وهمي لفتح Port على Render
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
logging.basicConfig(level=logging.INFO)
TOKEN = "8846997512:AAFfc2HSrJHWmXHfiEMO_M5I4F-OPc3zrrk"

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

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
        [InlineKeyboardButton("⚡ تحميل سريع", callback_data="v_instant")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")]
    ]
    await update.message.reply_text("👇 **اختر التحميل:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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

    status_msg = await query.edit_message_text("⚡ **جاري معالجة الفيديو...**")
    loop = asyncio.get_running_loop()

    try:
        # معالجة تيك توك عبر التحميل المحلي السريع والتنظيف
        if "tiktok.com" in url:
            filename = f"downloads/tiktok_{int(time.time())}.mp4"
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'format': 'b/best',
                'outtmpl': filename,
                'ffmpeg_location': FFMPEG_PATH,
                'socket_timeout': 15,
            }

            def download_tiktok():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            await loop.run_in_executor(None, download_tiktok)

            # البحث عن الملف المنزل
            matches = glob.glob(filename.rsplit('.', 1)[0] + '.*')
            if matches:
                filename = matches[0]

            with open(filename, 'rb') as f:
                await query.message.reply_video(video=f, caption="تم تحميل تيك توك بنجاح! 🎵✨")
            
            if os.path.exists(filename):
                os.remove(filename)
            await status_msg.delete()

        # باقي المنصات (إنستغرام، يوتيوب... إلخ) بالطريقة الفورية المباشرة
        else:
            def get_direct_media_url(target_url):
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'format': 'b/best',
                    'socket_timeout': 10,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=False)
                    if 'url' in info:
                        return info['url']
                    elif 'formats' in info:
                        for f in reversed(info['formats']):
                            if f.get('url') and f.get('vcodec') != 'none':
                                return f['url']
                return None

            direct_video_url = await loop.run_in_executor(None, get_direct_media_url, url)

            if not direct_video_url:
                await query.edit_message_text("❌ تعذر جلب المقطع، جرب رابطاً آخر.")
                return

            await query.message.reply_video(
                video=direct_video_url, 
                caption="تم التحميل الفوري! 🚀✨",
                supports_streaming=True
            )
            await status_msg.delete()

    except Exception as e:
        await query.edit_message_text(f"❌ **حدث خطأ:**\n`{str(e)[:100]}`", parse_mode='Markdown')

def main():
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل مع دعم تيك توك المباشر...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
