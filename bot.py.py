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

def clean_url(url: str) -> str:
    if "instagram.com" in url:
        match = re.search(r'(https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+)', url)
        if match:
            return match.group(1) + "/"
    return url

def make_progress_bar(percent):
    """إنشاء شريط النسبة المئوية [████░░░░] 50%"""
    filled_length = int(10 * percent // 100)
    bar = '█' * filled_length + '░' * (10 - filled_length)
    return f"[{bar}] {percent:.0f}%"

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
        [InlineKeyboardButton("⚡ تحميل المقطع", callback_data="v_download")],
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

    loop = asyncio.get_running_loop()

    # 1️⃣ جلب معلومات المقطع والعنوان والصورة المعاينة أولاً
    await query.edit_message_text("⏳ **جاري جلب معطيات المقطع...**")
    
    info_dict = None
    try:
        def fetch_info():
            ydl_opts = {'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info_dict = await loop.run_in_executor(None, fetch_info)
    except Exception:
        pass

    title = info_dict.get('title', 'تحميل فيديو') if info_dict else 'جاري التحميل...'
    thumbnail = info_dict.get('thumbnail') if info_dict else None

    # نص الحالة الأولية
    init_caption = f"**{title[:35]}**\n\n⭕ جاري التنزيل\n`[{'░'*10}] 0%`"

    # حذف الرسالة القديمة وإرسال صورة مع النص المتحرك (أو رسالة نصية إذا لم توجد صورة)
    await query.delete_message()
    
    if thumbnail:
        status_msg = await query.message.reply_photo(photo=thumbnail, caption=init_caption, parse_mode='Markdown')
    else:
        status_msg = await query.message.reply_text(text=init_caption, parse_mode='Markdown')

    # متغيرة لمنع التعديل المفرط للرسائل (تجنب الحظر من تليجرام)
    last_update_time = [time.time()]

    def progress_hook(d):
        if d['status'] == 'downloading':
            now = time.time()
            # التحديث كل 1.5 ثانية فقط
            if now - last_update_time[0] > 1.5:
                last_update_time[0] = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                
                if total > 0:
                    percent = (downloaded / total) * 100
                    bar_text = make_progress_bar(percent)
                    new_caption = f"**{title[:35]}**\n\n⭕ جاري التنزيل\n`{bar_text}`"
                    
                    # تعديل نص الصورة
                    try:
                        asyncio.run_coroutine_threadsafe(
                            status_msg.edit_caption(caption=new_caption, parse_mode='Markdown'),
                            loop
                        )
                    except Exception:
                        pass

    # 2️⃣ بدء عملية التحميل المحلية
    try:
        filename = f"downloads/file_{int(time.time())}.mp4"
        ydl_opts = {
            'quiet': True,
            'format': 'b/best',
            'outtmpl': filename,
            'progress_hooks': [progress_hook],
            'socket_timeout': 15,
        }

        def run_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, run_download)

        # 3️⃣ رفع الفيديو بعد انتهاء التحميل
        try:
            await status_msg.edit_caption(caption=f"**{title[:35]}**\n\n📤 **جاري الرفع إلى تليجرام...**", parse_mode='Markdown')
        except Exception:
            pass

        with open(filename, 'rb') as f:
            await query.message.reply_video(video=f, caption="تم تحميل الفيديو بنجاح! ✨🚀")

        # تنظيف وحذف
        await status_msg.delete()
        if os.path.exists(filename):
            os.remove(filename)

    except Exception as e:
        try:
            await status_msg.edit_caption(caption=f"❌ **حدث خطأ أثناء التحميل:**\n`{str(e)[:100]}`", parse_mode='Markdown')
        except Exception:
            pass

def main():
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    app = Application.builder().token(TOKEN).request(request).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🚀 البوت يعمل مع شريط النسبة وصورة المعاينة...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
