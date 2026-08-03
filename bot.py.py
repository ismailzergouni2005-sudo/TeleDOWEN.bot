import os
import re
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

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

def download_tiktok_fast(tiktok_url):
    """جلب رابط فيديو تيك توك بدون علامة مائية فوراً عبر TikWM API"""
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    params = {"url": tiktok_url, "hd": 1}
    
    response = requests.get(api_url, params=params, headers=headers, timeout=10)
    data = response.json()
    
    if data.get("code") == 0:
        # إرجاع رابط الفيديو المباشر (HD أو عادي)
        video_link = data["data"].get("hdplay") or data["data"].get("play")
        return video_link
    return None

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
        [InlineKeyboardButton("⚡ تحميل سريع فوري", callback_data="v_instant")],
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

    status_msg = await query.edit_message_text("⚡ **جاري جلب الفيديو فوراً...**")
    loop = asyncio.get_running_loop()

    try:
        # 1️⃣ معالجة فائقة السرعة لتيك توك عبر API
        if "tiktok.com" in url:
            tiktok_direct_url = await loop.run_in_executor(None, download_tiktok_fast, url)
            
            if tiktok_direct_url:
                await query.message.reply_video(
                    video=tiktok_direct_url,
                    caption="تم تحميل تيك توك بنجاح! 🎵✨",
                    supports_streaming=True
                )
                await status_msg.delete()
                return
            else:
                await query.edit_message_text("❌ تعذر جلب مقطع تيك توك، تأكد من صحة الرابط.")
                return

        # 2️⃣ باقي المنصات (إنستغرام، يوتيوب... إلخ) بالطريقة الفورية المباشرة
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
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل الآن بنجاح وتكامل تام...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
