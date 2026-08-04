import os
import asyncio
import sqlite3
import mimetypes
from datetime import datetime, timedelta
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand
)
import threading
from flask import Flask
import nest_asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

nest_asyncio.apply()

# ==================== خادم الويب ====================
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is Running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

# ==================== 1. الإعدادات والبيانات ====================
API_ID = 32087655
API_HASH = "0276a0250c2cfc8a1dde70b0f9f92fcd"
BOT_TOKEN = "8811469771:AAFYUx7hBFRCzD5cX6HsN0lGW71ZFnzDwP8"
OWNER_ID = 2071492262

POST_INTERVAL = 300            
is_paused = True               
last_post_time = None          
user_states = {}            
temp_posts = {}               
edit_posts = {}

app = Client("my_scheduler_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

BUTTON_TEXTS_AR = [
    "🟢 النشر شغال (اضغط للإيقاف)",
    "🔴 النشر متوقف (اضغط للتشغيل)",
    "⏱️ تغيير الفارق الزمني",
    "📢 إدارة القنوات",
    "📊 حالة النشر والطابور",
    "🔄 المنشورات المكررة",
    "🗑️ إفراغ الطابور",
    "📈 إحصائيات النشر",
    "🌐 تغيير اللغة"
]

BUTTON_TEXTS_EN = [
    "🟢 Publishing ON (Click to Pause)",
    "🔴 Publishing PAUSED (Click to Start)",
    "⏱️ Change Interval",
    "📢 Manage Channels",
    "📊 Queue & Status",
    "🔄 Recurring Posts",
    "🗑️ Clear Queue",
    "📈 Analytics",
    "🌐 Change Language"
]

ALL_BUTTON_TEXTS = BUTTON_TEXTS_AR + BUTTON_TEXTS_EN

# ==================== 2. إدارة قاعدة البيانات ====================

def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS channels (channel_username TEXT PRIMARY KEY)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            media_group_id TEXT,
            channels TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recurring_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            media_group_id TEXT,
            channels TEXT,
            next_run_timestamp REAL,
            repeat_interval_seconds INTEGER,
            remaining_repeats INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS publish_stats (
            channel_username TEXT PRIMARY KEY,
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'ar'
        )
    """)
    conn.commit()
    conn.close()

def set_user_lang(user_id, lang):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO user_settings (user_id, language) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET language=?", (user_id, lang, lang))
    conn.commit()
    conn.close()

def get_user_lang(user_id):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 'ar'

def increment_stat(channel, success=True):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO publish_stats (channel_username, success_count, fail_count) VALUES (?, 0, 0)", (channel,))
    if success:
        cursor.execute("UPDATE publish_stats SET success_count = success_count + 1 WHERE channel_username = ?", (channel,))
    else:
        cursor.execute("UPDATE publish_stats SET fail_count = fail_count + 1 WHERE channel_username = ?", (channel,))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_username, success_count, fail_count FROM publish_stats ORDER BY success_count DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_channels():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_username FROM channels")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def add_channel_db(ch):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO channels VALUES (?)", (ch,))
    conn.commit()
    conn.close()

def remove_channel_db(ch):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_username = ?", (ch,))
    conn.commit()
    conn.close()

def add_to_queue_db(chat_id, message_id, media_group_id, channels_list):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    chs_str = ",".join(channels_list)
    cursor.execute("INSERT INTO queue (chat_id, message_id, media_group_id, channels) VALUES (?, ?, ?, ?)", 
                   (chat_id, message_id, str(media_group_id), chs_str))
    conn.commit()
    conn.close()

def add_recurring_db(chat_id, message_id, media_group_id, channels_list, next_run_dt, interval_sec, repeats):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    chs_str = ",".join(channels_list)
    ts = next_run_dt.timestamp()
    cursor.execute("""
        INSERT INTO recurring_posts 
        (chat_id, message_id, media_group_id, channels, next_run_timestamp, repeat_interval_seconds, remaining_repeats)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, message_id, str(media_group_id), chs_str, ts, interval_sec, repeats))
    conn.commit()
    conn.close()

def get_queue_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message_id, media_group_id, channels FROM queue ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_recurring_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, message_id, media_group_id, channels, next_run_timestamp, repeat_interval_seconds, remaining_repeats FROM recurring_posts ORDER BY next_run_timestamp ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def pop_queue_db(queue_id):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
    conn.commit()
    conn.close()

def delete_recurring_db(rec_id):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM recurring_posts WHERE id = ?", (rec_id,))
    conn.commit()
    conn.close()

def update_recurring_next_run(rec_id, next_ts, remaining):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    if remaining == 0:
        cursor.execute("DELETE FROM recurring_posts WHERE id = ?", (rec_id,))
    else:
        cursor.execute("UPDATE recurring_posts SET next_run_timestamp = ?, remaining_repeats = ? WHERE id = ?", (next_ts, remaining, rec_id))
    conn.commit()
    conn.close()

def clear_queue_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM queue")
    conn.commit()
    conn.close()

# ==================== 3. الواجهات والتصميم واللغات ====================

def detect_video_source(text):
    if not text:
        return None
    text_lower = text.lower()
    if "pinterest.com" in text_lower or "pin.it" in text_lower:
        return "Pinterest 📌"
    elif "tiktok.com" in text_lower:
        return "TikTok 🎵"
    elif "instagram.com" in text_lower or "instagr.am" in text_lower:
        return "Instagram 📸"
    return None

def get_main_reply_keyboard(lang='ar'):
    if lang == 'en':
        status_btn = "🔴 Publishing PAUSED (Click to Start)" if is_paused else "🟢 Publishing ON (Click to Pause)"
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton(status_btn)],
                [KeyboardButton("⏱️ Change Interval"), KeyboardButton("📢 Manage Channels")],
                [KeyboardButton("📊 Queue & Status"), KeyboardButton("🔄 Recurring Posts")],
                [KeyboardButton("📈 Analytics"), KeyboardButton("🗑️ Clear Queue")],
                [KeyboardButton("🌐 Change Language")]
            ],
            resize_keyboard=True
        )
    else:
        status_btn = "🔴 النشر متوقف (اضغط للتشغيل)" if is_paused else "🟢 النشر شغال (اضغط للإيقاف)"
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton(status_btn)],
                [KeyboardButton("⏱️ تغيير الفارق الزمني"), KeyboardButton("📢 إدارة القنوات")],
                [KeyboardButton("📊 حالة النشر والطابور"), KeyboardButton("🔄 المنشورات المكررة")],
                [KeyboardButton("📈 إحصائيات النشر"), KeyboardButton("🗑️ إفراغ الطابور")],
                [KeyboardButton("🌐 تغيير اللغة")]
            ],
            resize_keyboard=True
        )
    return keyboard

def get_welcome_text(lang='ar'):
    if lang == 'en':
        return (
            "🌟 **Welcome to the Ultimate Media & Auto Scheduler Bot!** 🌟\n"
            "────────────────────────────────────────\n"
            "🚀 **Key Features & Capabilities:**\n\n"
            "📌 **1. Multi-Platform Video Source Detection:**\n"
            "• Automatically detects and tags downloaded videos from **Pinterest**, **TikTok**, and **Instagram**!\n\n"
            "⏱️ **2. Smart Queue Scheduler:**\n"
            "• Push messages/media to a queue and automatically publish them across your channels at custom intervals.\n\n"
            "🔄 **3. Advanced Recurring Posts:**\n"
            "• Schedule posts to repeat on flexible intervals, continuous loops, or set exact start dates/times (24-Hour Format).\n\n"
            "📢 **4. Multi-Channel Manager:**\n"
            "• Send content across multiple Telegram channels simultaneously.\n\n"
            "📊 **5. Live Analytics & Queue Controls:**\n"
            "• Detailed stats, success/fail percentages, queue management, and instant previews.\n"
            "────────────────────────────────────────\n"
            "👇 **Use the menu below to start managing your bot!**"
        )
    else:
        return (
            "🌟 **أهلاً بك في بوت النشر والتسمية الذكي والشامل!** 🌟\n"
            "────────────────────────────────────────\n"
            "🚀 **ميزات وخيارات البوت الاحترافية:**\n\n"
            "📌 **1. كشف مصدر الفيديوهات تلقائياً:**\n"
            "• التعرف التلقائي وطباعة اسم منصة التحميل عند إرسال الفيديو (**Pinterest**, **TikTok**, **Instagram**).\n\n"
            "⏱️ **2. جدولة النشر وسلسلة الطابور:**\n"
            "• إضافة الوسائط إلى الطابور الرئيسي والنشر التلقائي في القنوات حسب الفارق الزمني المحدد.\n\n"
            "🔄 **3. التحكم في المنشورات المكررة:**\n"
            "• جدولة المنشور للتكرار مع إمكانية تعديل تاريخ ووقت البدء بتنسيق (24 ساعة) وتحديد عدد التكرارات.\n\n"
            "📢 **4. إرسال متعدد للقنوات:**\n"
            "• إدارة ونشر الوسائط والنصوص إلى عدة قنوات في وقت واحد.\n\n"
            "📊 **5. إحصائيات دقيقة وتحكم كامل:**\n"
            "• معاينة المنشورات، نسبة النجاح/الفشل، وحذف الطابور بضغطة زر.\n"
            "────────────────────────────────────────\n"
            "👇 **استخدم القائمة بالأسفل للتحكم الكامل:**"
        )

def format_time_label(seconds, lang='ar'):
    if seconds == 0:
        return "Immediate (Now)" if lang == 'en' else "فوري (الآن)"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} mins" if lang == 'en' else f"{mins} دقيقة"
    hours = mins // 60
    rem_mins = mins % 60
    if rem_mins > 0:
        return f"{hours}h {rem_mins}m" if lang == 'en' else f"{hours} ساعة و {rem_mins} دقيقة"
    return f"{hours}h" if lang == 'en' else f"{hours} ساعة"

def build_recurring_main_kb(user_id, lang='ar'):
    data = temp_posts.get(user_id, {})
    rec_ts = data.get('rec_start_ts', None)
    interval_sec = data.get('rec_interval', 3600)
    repeats_val = data.get('rec_repeats', -1)

    if rec_ts is None:
        start_str = "Immediate" if lang == 'en' else "فوري (الآن)"
    else:
        start_str = datetime.fromtimestamp(rec_ts).strftime('%H:%M %Y-%m-%d')

    repeats_str = "♾️ Unlimited" if repeats_val == -1 else f"{repeats_val}"

    if lang == 'en':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏰ Start Time: {start_str}", callback_data="rec_menu_start")],
            [InlineKeyboardButton(f"⏱️ Interval: {format_time_label(interval_sec, 'en')}", callback_data="rec_menu_interval")],
            [InlineKeyboardButton(f"🔁 Repeats: {repeats_str}", callback_data="rec_menu_repeats")],
            [InlineKeyboardButton("✅ Confirm & Schedule", callback_data="rec_confirm_save")],
            [InlineKeyboardButton("🔙 Back", callback_data="rec_back_to_post"), InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏰ بدء النشر: {start_str}", callback_data="rec_menu_start")],
            [InlineKeyboardButton(f"⏱️ الزمن بين التكرارات: {format_time_label(interval_sec, 'ar')}", callback_data="rec_menu_interval")],
            [InlineKeyboardButton(f"🔁 عدد التكرارات: {repeats_str}", callback_data="rec_menu_repeats")],
            [InlineKeyboardButton("✅ تأكيد وجدولة النشر", callback_data="rec_confirm_save")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="rec_back_to_post"), InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")]
        ])
    return kb

def build_edit_recurring_kb(r_id, user_id, lang='ar'):
    p = edit_posts.get(user_id, {})
    next_ts = p.get('next_ts', datetime.now().timestamp())
    interval_sec = p.get('interval_sec', 3600)
    remaining = p.get('remaining', -1)

    dt_str = datetime.fromtimestamp(next_ts).strftime('%H:%M %Y-%m-%d (24H)')
    rem_str = "Unlimited" if remaining == -1 else f"{remaining}"

    if lang == 'en':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏰ Start Time: {dt_str}", callback_data=f"edit_field_start_{r_id}")],
            [InlineKeyboardButton(f"⏱️ Interval: {format_time_label(interval_sec, 'en')}", callback_data=f"edit_field_int_{r_id}")],
            [InlineKeyboardButton(f"🔁 Repeats: {rem_str}", callback_data=f"edit_field_rep_{r_id}")],
            [InlineKeyboardButton("✅ Save Changes", callback_data=f"edit_save_{r_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_recs"), InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⏰ موعد بدء النشر: {dt_str}", callback_data=f"edit_field_start_{r_id}")],
            [InlineKeyboardButton(f"⏱️ الزمن بين التكرارات: {format_time_label(interval_sec, 'ar')}", callback_data=f"edit_field_int_{r_id}")],
            [InlineKeyboardButton(f"🔁 عدد المرات: {rem_str}", callback_data=f"edit_field_rep_{r_id}")],
            [InlineKeyboardButton("✅ حفظ التعديلات", callback_data=f"edit_save_{r_id}")],
            [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="back_to_recs"), InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")]
        ])
    return kb

# ==================== 4. محرك النشر ====================

async def publish_item(chat_id, msg_id, media_group_id, channels_list):
    for ch in channels_list:
        ch = ch.strip()
        if not ch:
            continue
        try:
            if media_group_id and media_group_id != "None":
                await app.copy_media_group(chat_id=ch, from_chat_id=chat_id, message_id=msg_id)
            else:
                await app.copy_message(chat_id=ch, from_chat_id=chat_id, message_id=msg_id)
            increment_stat(ch, success=True)
        except Exception as e:
            increment_stat(ch, success=False)
            print(f"[!] Published error in {ch}: {e}")

async def publish_worker():
    global last_post_time, is_paused
    while True:
        try:
            now = datetime.now()
            now_ts = now.timestamp()

            # 1. المنشورات المكررة
            recurring_items = get_recurring_db()
            for r in recurring_items:
                r_id, chat_id, msg_id, media_group_id, chs_str, next_run_ts, interval_sec, remaining = r
                if now_ts >= next_run_ts:
                    channels = chs_str.split(",")
                    await publish_item(chat_id, msg_id, media_group_id, channels)
                    
                    new_remaining = remaining - 1 if remaining > 0 else -1
                    next_ts = now_ts + interval_sec
                    update_recurring_next_run(r_id, next_ts, new_remaining)

            # 2. الطابور الرئيسي
            if not is_paused:
                queue_items = get_queue_db()
                if queue_items:
                    should_publish = False
                    if last_post_time is None:
                        should_publish = True
                    else:
                        if (now - last_post_time).total_seconds() >= POST_INTERVAL:
                            should_publish = True

                    if should_publish:
                        item = queue_items[0]
                        q_id, chat_id, msg_id, media_group_id, chs_str = item
                        await publish_item(chat_id, msg_id, media_group_id, chs_str.split(","))
                        last_post_time = datetime.now()
                        pop_queue_db(q_id)

        except Exception as e:
            print(f"[!] Engine Error: {e}")
            
        await asyncio.sleep(3)

# ==================== 5. الأوامر وأزرار التحكم ====================

admin_filter = filters.private & filters.user(OWNER_ID)

@app.on_message(filters.command("start") & admin_filter)
async def start_cmd(client: Client, message: Message):
    lang_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇸🇦 العربية", callback_data="set_lang_ar"), InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")]
    ])
    await message.reply_text(
        "🌐 **Please choose your language / الرجاء اختيار اللغة:**",
        reply_markup=lang_kb
    )

@app.on_message(admin_filter & filters.text & filters.create(lambda _, __, m: m.text in ALL_BUTTON_TEXTS))
async def handle_reply_buttons(client: Client, message: Message):
    global is_paused, POST_INTERVAL, user_states, last_post_time
    text = message.text.strip()
    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    # تغيير اللغة
    if text in ["🌐 تغيير اللغة", "🌐 Change Language"]:
        lang_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🇸🇦 العربية", callback_data="set_lang_ar"), InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")]
        ])
        await message.reply_text("🌐 **اختر اللغة المفضلة / Select Language:**", reply_markup=lang_kb)
        return

    # 1. بدء / إيقاف النشر
    if text.startswith("🟢") or text.startswith("🔴"):
        is_paused = not is_paused
        if lang == 'en':
            status_text = "🔴 **Publishing is paused.**" if is_paused else f"🟢 **Publishing active!**\n⏱️ **Interval:** `{round(POST_INTERVAL/60, 1)}` mins."
        else:
            status_text = "🔴 **تم إيقاف النشر مؤقتاً.**" if is_paused else f"🟢 **تم تشغيل النشر بنجاح!**\n⏱️ **الفارق الزمني الحالي:** `{round(POST_INTERVAL/60, 1)}` دقيقة."
        await message.reply_text(status_text, reply_markup=get_main_reply_keyboard(lang))

    # 2. تغيير الفارق الزمني
    elif text in ["⏱️ تغيير الفارق الزمني", "⏱️ Change Interval"]:
        time_inline_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ 5 Min", callback_data="set_time_5"), InlineKeyboardButton("⏱️ 30 Min", callback_data="set_time_30"), InlineKeyboardButton("🕐 1 Hour", callback_data="set_time_60")],
            [InlineKeyboardButton("🕒 3 Hours", callback_data="set_time_180"), InlineKeyboardButton("🕕 12 Hours", callback_data="set_time_720")],
            [InlineKeyboardButton("✏️ Custom Input", callback_data="set_custom_time")],
            [InlineKeyboardButton("❌ Close", callback_data="action_cancel")]
        ])
        msg = f"⏱️ **Set Queue Interval:**\n💡 Current: `{round(POST_INTERVAL/60, 1)}` mins." if lang == 'en' else f"⏱️ **تحديد الفارق الزمني بين المنشورات:**\n💡 **الضبط الحالي:** `{round(POST_INTERVAL/60, 1)}` دقيقة."
        await message.reply_text(msg, reply_markup=time_inline_keyboard)

    # 3. إدارة القنوات
    elif text in ["📢 إدارة القنوات", "📢 Manage Channels"]:
        target_channels = get_channels()
        ch_text = "📢 **Target Channels:**\n" if lang == 'en' else "📢 **القنوات المضافة حالياً:**\n───────────────────\n"
        if target_channels:
            for i, c in enumerate(target_channels, 1):
                ch_text += f"**{i}.** 📌 `{c}`\n"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel"), InlineKeyboardButton("❌ Remove Channel", callback_data="remove_channel_menu")],
                [InlineKeyboardButton("❌ Close", callback_data="action_cancel")]
            ])
        else:
            ch_text += "⚠️ *No channels added yet!*" if lang == 'en' else "⚠️ *لم تقم بإضافة أي قناة بعد!*"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")],
                [InlineKeyboardButton("❌ Close", callback_data="action_cancel")]
            ])

        await message.reply_text(ch_text, reply_markup=kb)

    # 4. حالة النشر والطابور
    elif text in ["📊 حالة النشر والطابور", "📊 Queue & Status"]:
        queue = get_queue_db()
        queue_len = len(queue)
        
        status_icon = "🔴 PAUSED" if is_paused else "🟢 RUNNING"
        next_post_str = "Unknown" if lang == 'en' else "غير معروف"
        if not is_paused and last_post_time and queue_len > 0:
            remaining_sec = max(0, POST_INTERVAL - (datetime.now() - last_post_time).total_seconds())
            next_post_str = f"{int(remaining_sec // 60)}m {int(remaining_sec % 60)}s"
        elif not is_paused and queue_len > 0:
            next_post_str = "Immediate" if lang == 'en' else "فوري"

        msg_text = (
            f"📊 **Engine Queue Status:**\n• **Status:** {status_icon}\n• **Items in Queue:** `{queue_len}`\n• **Next Run:** `{next_post_str}`"
            if lang == 'en' else
            f"📊 **حالة النشر العامة:**\n───────────────────\n• **حالة المحرك:** {status_icon}\n• **عدد المنتظر بانتظار النشر:** `{queue_len}` منشور\n• **موعد المنشور القادم:** `{next_post_str}`"
        )
        
        buttons = []
        for idx, item in enumerate(queue, 1):
            q_id = item[0]
            buttons.append([
                InlineKeyboardButton(f"👁️ Preview #{idx}", callback_data=f"preview_main_q_{q_id}"),
                InlineKeyboardButton(f"🗑️ Delete #{idx}", callback_data=f"delete_main_q_{q_id}")
            ])

        buttons.append([InlineKeyboardButton("❌ Close", callback_data="action_cancel")])
        await message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons))

    # 5. المنشورات المكررة
    elif text in ["🔄 المنشورات المكررة", "🔄 Recurring Posts"]:
        recs = get_recurring_db()
        if not recs:
            await message.reply_text("🔄 **No recurring posts scheduled.**" if lang == 'en' else "🔄 **لا توجد منشورات مكررة نشطة حالياً.**")
            return
        
        msg_text = "🔄 **Active Recurring Posts:**\n\n" if lang == 'en' else "🔄 **قائمة المنشورات المكررة المبرمجة:**\n───────────────────\n"
        buttons = []
        for r in recs:
            r_id, _, _, _, _, next_run_ts, interval_sec, remaining = r
            dt_str = datetime.fromtimestamp(next_run_ts).strftime('%H:%M - %Y/%m/%d')
            rem_str = "Unlimited" if remaining == -1 else f"{remaining}"
            
            msg_text += f"📌 **ID:** `{r_id}` | ⏰ `{dt_str}`\n⏱️ `{format_time_label(interval_sec, lang)}` | 🔁 `{rem_str}`\n───────────────────\n"
            buttons.append([
                InlineKeyboardButton(f"✏️ Edit #{r_id}", callback_data=f"edit_rec_{r_id}"),
                InlineKeyboardButton(f"🗑️ Delete #{r_id}", callback_data=f"delete_rec_{r_id}")
            ])

        buttons.append([InlineKeyboardButton("❌ Close", callback_data="action_cancel")])
        await message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons))

    # 6. إفراغ الطابور
    elif text in ["🗑️ إفراغ الطابور", "🗑️ Clear Queue"]:
        queue_len = len(get_queue_db())
        if queue_len == 0:
            await message.reply_text("⚠️ **Queue is empty.**" if lang == 'en' else "⚠️ **الطابور الرئيسي فارغ بالفعل.**")
            return
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Clear All", callback_data="confirm_clear_queue")],
            [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
        ])
        await message.reply_text(f"⚠️ **Confirm Clear Queue (`{queue_len}` items)?**", reply_markup=confirm_kb)

    # 7. إحصائيات النشر
    elif text in ["📈 إحصائيات النشر", "📈 Analytics"]:
        stats = get_stats()
        if not stats:
            await message.reply_text("📈 **No analytics available.**" if lang == 'en' else "📈 **لا توجد إحصائيات نشر مسجلة بعد.**")
            return
        
        msg_text = "📈 **Publishing Analytics:**\n\n" if lang == 'en' else "📈 **سجل إحصائيات النشر بالقنوات:**\n───────────────────\n"
        for ch, success, fail in stats:
            msg_text += f"📢 `{ch}`\n✅ Success: `{success}` | ❌ Fail: `{fail}`\n───────────────────\n"
            
        await message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="action_cancel")]]))

# ==================== 6. التعامل مع كول باك الأزرار ====================

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    global POST_INTERVAL, user_states, temp_posts, edit_posts
    data = query.data
    user_id = query.from_user.id
    lang = get_user_lang(user_id)

    try:
        target_channels = get_channels()

        if data.startswith("set_lang_"):
            selected_lang = data.replace("set_lang_", "")
            set_user_lang(user_id, selected_lang)
            await query.answer("Language updated!" if selected_lang == 'en' else "تم تغيير اللغة!")
            await query.message.delete()
            await query.message.reply_text(get_welcome_text(selected_lang), reply_markup=get_main_reply_keyboard(selected_lang))

        elif data == "action_cancel":
            user_states[user_id] = None
            if user_id in temp_posts:
                del temp_posts[user_id]
            if user_id in edit_posts:
                del edit_posts[user_id]
            await query.message.delete()

        elif data == "add_channel":
            user_states[user_id] = "waiting_add_channel"
            await query.message.edit_text("📢 **Send channel username (e.g. `@mychannel`):**" if lang == 'en' else "📢 **أرسل معرّف القناة الآن (`@mychannel`):**")

        elif data == "remove_channel_menu":
            buttons = []
            for ch in target_channels:
                buttons.append([InlineKeyboardButton(f"❌ {ch}", callback_data=f"remove_ch_{ch}")])
            buttons.append([InlineKeyboardButton("❌ Close", callback_data="action_cancel")])
            await query.message.edit_text("❌ Select channel to remove:" if lang == 'en' else "❌ **اختر القناة المراد إزالتها:**", reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("remove_ch_"):
            ch_to_rem = data.replace("remove_ch_", "")
            remove_channel_db(ch_to_rem)
            await query.answer("Channel removed!" if lang == 'en' else "تم حذف القناة!")
            await query.message.edit_text(f"✅ Removed `{ch_to_rem}`")

        elif data.startswith("set_time_"):
            val = data.replace("set_time_", "")
            if val == "custom":
                user_states[user_id] = "waiting_custom_time"
                await query.message.edit_text("⏱️ **Enter interval in minutes:**" if lang == 'en' else "⏱️ **أرسل الفارق الزمني بالدقائق:**")
            else:
                POST_INTERVAL = int(val) * 60
                await query.message.edit_text(f"✅ Interval set to `{val}` mins.")

        elif data.startswith("delete_rec_"):
            r_id = int(data.replace("delete_rec_", ""))
            delete_recurring_db(r_id)
            await query.answer("Deleted!")
            await query.message.edit_text("🗑️ Recurring post removed.")

        elif data.startswith("edit_rec_"):
            r_id = int(data.replace("edit_rec_", ""))
            conn = sqlite3.connect("bot_data.db")
            cursor = conn.cursor()
            cursor.execute("SELECT id, chat_id, message_id, media_group_id, channels, next_run_timestamp, repeat_interval_seconds, remaining_repeats FROM recurring_posts WHERE id = ?", (r_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                r_id, chat_id, msg_id, media_group_id, chs, next_ts, interval_sec, remaining = row
                edit_posts[user_id] = {'r_id': r_id, 'next_ts': next_ts, 'interval_sec': interval_sec, 'remaining': remaining}
                await query.message.edit_text(f"✏️ **Edit Recurring Post #{r_id}**", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))

        elif data.startswith("edit_field_start_"):
            r_id = int(data.replace("edit_field_start_", ""))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Immediate", callback_data=f"set_edit_start_{r_id}_0"), InlineKeyboardButton("⏱️ 5 Min", callback_data=f"set_edit_start_{r_id}_300")],
                [InlineKeyboardButton("⏱️ 30 Min", callback_data=f"set_edit_start_{r_id}_1800"), InlineKeyboardButton("🕐 1 Hour", callback_data=f"set_edit_start_{r_id}_3600")],
                [InlineKeyboardButton("🗓️ Custom Date/Time 24H", callback_data=f"custom_edit_start_{r_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"edit_rec_{r_id}")]
            ])
            await query.message.edit_text("⏰ **Select start schedule:**", reply_markup=kb)

        elif data.startswith("custom_edit_start_"):
            r_id = int(data.replace("custom_edit_start_", ""))
            user_states[user_id] = f"waiting_custom_edit_start_{r_id}"
            await query.message.edit_text("📅 **Send start time format 24H (`14:30 2026-08-02` or `14:30`):**")

        elif data.startswith("set_edit_start_"):
            parts = data.replace("set_edit_start_", "").split("_")
            r_id, sec = int(parts[0]), int(parts[1])
            new_ts = datetime.now().timestamp() + sec
            if user_id in edit_posts:
                edit_posts[user_id]['next_ts'] = new_ts
            await query.message.edit_text(f"✏️ **Edit Recurring Post #{r_id}**", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))

        elif data.startswith("edit_field_int_"):
            r_id = int(data.replace("edit_field_int_", ""))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏱️ 5 Min", callback_data=f"set_edit_int_{r_id}_300"), InlineKeyboardButton("⏱️ 30 Min", callback_data=f"set_edit_int_{r_id}_1800")],
                [InlineKeyboardButton("🕐 1 Hour", callback_data=f"set_edit_int_{r_id}_3600"), InlineKeyboardButton("🕒 5 Hours", callback_data=f"set_edit_int_{r_id}_18000")],
                [InlineKeyboardButton("✏️ Custom Input", callback_data=f"custom_edit_int_{r_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"edit_rec_{r_id}")]
            ])
            await query.message.edit_text("⏱️ **Select new interval:**", reply_markup=kb)

        elif data.startswith("custom_edit_int_"):
            r_id = int(data.replace("custom_edit_int_", ""))
            user_states[user_id] = f"waiting_custom_edit_int_{r_id}"
            await query.message.edit_text("✏️ **Enter interval minutes:**")

        elif data.startswith("set_edit_int_"):
            parts = data.replace("set_edit_int_", "").split("_")
            r_id, sec = int(parts[0]), int(parts[1])
            if user_id in edit_posts:
                edit_posts[user_id]['interval_sec'] = sec
            await query.message.edit_text(f"✏️ **Edit Recurring Post #{r_id}**", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))

        elif data.startswith("edit_field_rep_"):
            r_id = int(data.replace("edit_field_rep_", ""))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("♾️ Unlimited", callback_data=f"set_edit_rep_{r_id}_-1"), InlineKeyboardButton("1️⃣ Once", callback_data=f"set_edit_rep_{r_id}_1")],
                [InlineKeyboardButton("3️⃣ 3 Times", callback_data=f"set_edit_rep_{r_id}_3"), InlineKeyboardButton("5️⃣ 5 Times", callback_data=f"set_edit_rep_{r_id}_5")],
                [InlineKeyboardButton("✏️ Custom Repeats", callback_data=f"custom_edit_rep_{r_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"edit_rec_{r_id}")]
            ])
            await query.message.edit_text("🔁 **Select repeats count:**", reply_markup=kb)

        elif data.startswith("custom_edit_rep_"):
            r_id = int(data.replace("custom_edit_rep_", ""))
            user_states[user_id] = f"waiting_custom_edit_rep_{r_id}"
            await query.message.edit_text("✏️ **Enter repeat count:**")

        elif data.startswith("set_edit_rep_"):
            parts = data.replace("set_edit_rep_", "").split("_")
            r_id, rep = int(parts[0]), int(parts[1])
            if user_id in edit_posts:
                edit_posts[user_id]['remaining'] = rep
            await query.message.edit_text(f"✏️ **Edit Recurring Post #{r_id}**", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))

        elif data.startswith("edit_save_"):
            r_id = int(data.replace("edit_save_", ""))
            if user_id in edit_posts:
                p = edit_posts[user_id]
                conn = sqlite3.connect("bot_data.db")
                cursor = conn.cursor()
                cursor.execute("UPDATE recurring_posts SET next_run_timestamp = ?, repeat_interval_seconds = ?, remaining_repeats = ? WHERE id = ?", (p['next_ts'], p['interval_sec'], p['remaining'], r_id))
                conn.commit()
                conn.close()
                del edit_posts[user_id]
                await query.answer("Saved!")
                await query.message.edit_text("✅ Changes saved successfully.")

        elif data == "back_to_recs":
            if user_id in edit_posts:
                del edit_posts[user_id]
            recs = get_recurring_db()
            msg_text = "🔄 **Active Recurring Posts:**\n\n"
            buttons = []
            for r in recs:
                r_id, _, _, _, _, next_run_ts, interval_sec, remaining = r
                dt_str = datetime.fromtimestamp(next_run_ts).strftime('%H:%M - %Y/%m/%d')
                msg_text += f"📌 **ID:** `{r_id}` | `{dt_str}`\n"
                buttons.append([InlineKeyboardButton(f"✏️ Edit #{r_id}", callback_data=f"edit_rec_{r_id}"), InlineKeyboardButton(f"🗑️ Delete #{r_id}", callback_data=f"delete_rec_{r_id}")])
            buttons.append([InlineKeyboardButton("❌ Close", callback_data="action_cancel")])
            await query.message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("preview_main_q_"):
            q_id = int(data.replace("preview_main_q_", ""))
            conn = sqlite3.connect("bot_data.db")
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, message_id, media_group_id FROM queue WHERE id = ?", (q_id,))
            row = cursor.fetchone()
            conn.close()

            if row:
                chat_id, msg_id, media_group_id = row
                if media_group_id and media_group_id != "None":
                    await app.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=msg_id)
                else:
                    await app.copy_message(chat_id=user_id, from_chat_id=chat_id, message_id=msg_id)
                await query.answer("Preview sent!")

        elif data.startswith("delete_main_q_"):
            q_id = int(data.replace("delete_main_q_", ""))
            pop_queue_db(q_id)
            await query.answer("Deleted!")
            await query.message.edit_text("🗑️ Item deleted from queue.")

        elif data == "confirm_clear_queue":
            clear_queue_db()
            await query.answer("Cleared!")
            await query.message.edit_text("🗑️ Queue cleared.")

        elif data == "post_type_recurring":
            temp_posts[user_id]['rec_start_ts'] = None
            temp_posts[user_id]['rec_interval'] = 3600
            temp_posts[user_id]['rec_repeats'] = -1
            await query.message.edit_text("🔄 **Configure Recurring Post:**", reply_markup=build_recurring_main_kb(user_id, lang))

        elif data == "rec_menu_start":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Immediate", callback_data="rec_set_start_0"), InlineKeyboardButton("⏱️ 5 Min", callback_data="rec_set_start_300")],
                [InlineKeyboardButton("⏱️ 30 Min", callback_data="rec_set_start_1800"), InlineKeyboardButton("🕐 1 Hour", callback_data="rec_set_start_3600")],
                [InlineKeyboardButton("🗓️ Custom Date 24H", callback_data="rec_custom_start")],
                [InlineKeyboardButton("🔙 Back", callback_data="post_type_recurring")]
            ])
            await query.message.edit_text("⏰ **Select initial start time:**", reply_markup=kb)

        elif data.startswith("rec_set_start_"):
            sec = int(data.replace("rec_set_start_", ""))
            temp_posts[user_id]['rec_start_ts'] = datetime.now().timestamp() + sec
            await query.message.edit_text("🔄 **Configure Recurring Post:**", reply_markup=build_recurring_main_kb(user_id, lang))

        elif data == "rec_custom_start":
            user_states[user_id] = "waiting_rec_custom_start"
            await query.message.edit_text("📅 **Enter start date/time 24H format (`15:30 2026-08-02` or `15:30`):**")

        elif data == "rec_menu_interval":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏱️ 5 Min", callback_data="rec_set_interval_300"), InlineKeyboardButton("⏱️ 30 Min", callback_data="rec_set_interval_1800")],
                [InlineKeyboardButton("🕐 1 Hour", callback_data="rec_set_interval_3600"), InlineKeyboardButton("🕒 5 Hours", callback_data="rec_set_interval_18000")],
                [InlineKeyboardButton("✏️ Custom Input", callback_data="rec_custom_interval")],
                [InlineKeyboardButton("🔙 Back", callback_data="post_type_recurring")]
            ])
            await query.message.edit_text("⏱️ **Select repeat interval:**", reply_markup=kb)

        elif data.startswith("rec_set_interval_"):
            sec = int(data.replace("rec_set_interval_", ""))
            temp_posts[user_id]['rec_interval'] = sec
            await query.message.edit_text("🔄 **Configure Recurring Post:**", reply_markup=build_recurring_main_kb(user_id, lang))

        elif data == "rec_custom_interval":
            user_states[user_id] = "waiting_rec_custom_interval"
            await query.message.edit_text("✏️ **Enter repeat interval in minutes:**")

        elif data == "rec_menu_repeats":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("♾️ Unlimited", callback_data="rec_set_repeats_-1"), InlineKeyboardButton("1️⃣ Once", callback_data="rec_set_repeats_1")],
                [InlineKeyboardButton("3️⃣ 3 Times", callback_data="rec_set_repeats_3"), InlineKeyboardButton("5️⃣ 5 Times", callback_data="rec_set_repeats_5")],
                [InlineKeyboardButton("✏️ Custom Input", callback_data="rec_custom_repeats")],
                [InlineKeyboardButton("🔙 Back", callback_data="post_type_recurring")]
            ])
            await query.message.edit_text("🔁 **Select repeats limit:**", reply_markup=kb)

        elif data.startswith("rec_set_repeats_"):
            rep = int(data.replace("rec_set_repeats_", ""))
            temp_posts[user_id]['rec_repeats'] = rep
            await query.message.edit_text("🔄 **Configure Recurring Post:**", reply_markup=build_recurring_main_kb(user_id, lang))

        elif data == "rec_custom_repeats":
            user_states[user_id] = "waiting_rec_custom_repeats"
            await query.message.edit_text("✏️ **Enter repeats limit count:**")

        elif data == "rec_confirm_save":
            if user_id in temp_posts:
                post = temp_posts[user_id]
                rec_ts = post.get('rec_start_ts')
                start_dt = datetime.now() if rec_ts is None else datetime.fromtimestamp(rec_ts)

                add_recurring_db(
                    chat_id=post['chat_id'],
                    message_id=post['message_id'],
                    media_group_id=post['media_group_id'],
                    channels_list=target_channels,
                    next_run_dt=start_dt,
                    interval_sec=post['rec_interval'],
                    repeats=post['rec_repeats']
                )
                del temp_posts[user_id]
                await query.message.edit_text("✅ **Post successfully scheduled!**" if lang == 'en' else "✅ **تم حفظ وجدولة المنشور المكرر بنجاح!**")

        elif data == "rec_back_to_post":
            if user_id in temp_posts:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Standard Queue", callback_data="post_type_queue")],
                    [InlineKeyboardButton("🔄 Recurring Post", callback_data="post_type_recurring")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
                ])
                await query.message.edit_text("📌 Post Received. Choose action:" if lang == 'en' else "📌 **تم استلام المنشور.** اختر طريقة النشر:", reply_markup=kb)

        elif data == "post_type_queue":
            if user_id in temp_posts:
                post = temp_posts[user_id]
                add_to_queue_db(post['chat_id'], post['message_id'], post['media_group_id'], target_channels)
                del temp_posts[user_id]
                await query.message.edit_text("✅ **Post added to queue!**" if lang == 'en' else "✅ **تمت إضافة المنشور بنجاح إلى الطابور الرئيسي!**")

    except Exception as e:
        print(f"[!] Callback Error: {e}")

# ==================== 7. استقبال النصوص ومعالجة الوسائط وسورس الفيديوهات ====================

def parse_24h_datetime(text_input):
    text_input = text_input.strip()
    now = datetime.now()
    try:
        if len(text_input.split()) == 1 and ":" in text_input:
            t = datetime.strptime(text_input, "%H:%M")
            target_dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if target_dt < now:
                target_dt += timedelta(days=1)
            return target_dt
        elif len(text_input.split()) == 2:
            return datetime.strptime(text_input, "%H:%M %Y-%m-%d")
    except Exception:
        return None
    return None

@app.on_message(admin_filter)
async def handle_incoming_messages(client: Client, message: Message):
    global user_states, temp_posts, POST_INTERVAL
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    state = user_states.get(user_id)

    # 1. حالة إضافة قناة
    if state == "waiting_add_channel":
        ch = message.text.strip()
        if not ch.startswith("@") and not ch.startswith("-100"):
            await message.reply_text("⚠️ Enter valid username `@channel`" if lang == 'en' else "⚠️ **يرجى إدخال معرّف صحيح يبدأ بـ `@`.**")
            return
        add_channel_db(ch)
        user_states[user_id] = None
        await message.reply_text(f"✅ Channel `{ch}` added." if lang == 'en' else f"✅ **تمت إضافة القناة `{ch}` بنجاح.**", reply_markup=get_main_reply_keyboard(lang))
        return

    # 2. الوقت المخصص للطابور
    elif state == "waiting_custom_time":
        if not message.text.isdigit():
            await message.reply_text("⚠️ Please send digits only.")
            return
        POST_INTERVAL = int(message.text) * 60
        user_states[user_id] = None
        await message.reply_text(f"✅ Interval set to `{message.text}` mins.", reply_markup=get_main_reply_keyboard(lang))
        return

    # 3. إدخال وقت 24H مخصص
    elif state == "waiting_rec_custom_start":
        parsed_dt = parse_24h_datetime(message.text)
        if not parsed_dt:
            await message.reply_text("⚠️ Invalid 24H format (`15:30 2026-08-02` or `15:30`)")
            return
        temp_posts[user_id]['rec_start_ts'] = parsed_dt.timestamp()
        user_states[user_id] = None
        await message.reply_text(f"🔄 Start time set: `{parsed_dt.strftime('%H:%M %Y-%m-%d')}`", reply_markup=build_recurring_main_kb(user_id, lang))
        return

    elif state == "waiting_rec_custom_interval":
        if not message.text.isdigit():
            await message.reply_text("⚠️ Digits only.")
            return
        temp_posts[user_id]['rec_interval'] = int(message.text) * 60
        user_states[user_id] = None
        await message.reply_text("🔄 Updated interval:", reply_markup=build_recurring_main_kb(user_id, lang))
        return

    elif state == "waiting_rec_custom_repeats":
        if not message.text.isdigit():
            await message.reply_text("⚠️ Digits only.")
            return
        temp_posts[user_id]['rec_repeats'] = int(message.text)
        user_states[user_id] = None
        await message.reply_text("🔄 Updated repeats:", reply_markup=build_recurring_main_kb(user_id, lang))
        return

    elif state and state.startswith("waiting_custom_edit_start_"):
        r_id = int(state.replace("waiting_custom_edit_start_", ""))
        parsed_dt = parse_24h_datetime(message.text)
        if not parsed_dt:
            await message.reply_text("⚠️ Invalid format.")
            return
        if user_id in edit_posts:
            edit_posts[user_id]['next_ts'] = parsed_dt.timestamp()
        user_states[user_id] = None
        await message.reply_text(f"✏️ Edit Post #{r_id}", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))
        return

    elif state and state.startswith("waiting_custom_edit_int_"):
        r_id = int(state.replace("waiting_custom_edit_int_", ""))
        if not message.text.isdigit():
            await message.reply_text("⚠️ Digits only.")
            return
        if user_id in edit_posts:
            edit_posts[user_id]['interval_sec'] = int(message.text) * 60
        user_states[user_id] = None
        await message.reply_text(f"✏️ Edit Post #{r_id}", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))
        return

    elif state and state.startswith("waiting_custom_edit_rep_"):
        r_id = int(state.replace("waiting_custom_edit_rep_", ""))
        if not message.text.isdigit():
            await message.reply_text("⚠️ Digits only.")
            return
        if user_id in edit_posts:
            edit_posts[user_id]['remaining'] = int(message.text)
        user_states[user_id] = None
        await message.reply_text(f"✏️ Edit Post #{r_id}", reply_markup=build_edit_recurring_kb(r_id, user_id, lang))
        return

    # 4. استقبال الفيديوهات والوسائط ومعالجة موقع التحميل
    target_channels = get_channels()
    if not target_channels:
        await message.reply_text("⚠️ Add at least one channel first!" if lang == 'en' else "⚠️ **يرجى إضافة قناة واحدة على الأقل قبل إضافة المنشورات!**")
        return

    # طباعة كشف مصدر الفيديو في الوصف
    if message.video or message.animation:
        caption_text = message.caption or message.text or ""
        detected_source = detect_video_source(caption_text)
        
        if detected_source:
            source_tag = f"\n\n📥 **تم التحميل بواسطة / Downloaded from:** {detected_source}"
            try:
                await message.edit_caption(caption_text + source_tag)
            except Exception:
                pass

    if message.media_group_id:
        if user_id in temp_posts and temp_posts[user_id].get('media_group_id') == message.media_group_id:
            return
        temp_posts[user_id] = {
            'chat_id': message.chat.id,
            'message_id': message.id,
            'media_group_id': message.media_group_id
        }
    else:
        temp_posts[user_id] = {
            'chat_id': message.chat.id,
            'message_id': message.id,
            'media_group_id': None
        }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Queue Post", callback_data="post_type_queue")],
        [InlineKeyboardButton("🔄 Recurring Post", callback_data="post_type_recurring")],
        [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
    ]) if lang == 'en' else InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 إضافة للطابور العادي", callback_data="post_type_queue")],
        [InlineKeyboardButton("🔄 إعداد منشور مكرر", callback_data="post_type_recurring")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action_cancel")]
    ])

    await message.reply_text(
        f"📌 **Post received.** Target channels: `{len(target_channels)}`" if lang == 'en' else f"📌 **تم استلام المنشور.**\n───────────────────\n📢 **القنوات المستهدفة:** `{len(target_channels)}` قناة.",
        reply_markup=kb
    )

# ==================== 8. تشغيل البوت ====================

async def main():
    init_db()
    await app.start()
    
    await app.set_bot_commands([
        BotCommand("start", "Start Bot & Language Options / بدء التشغيل واختيار اللغة")
    ])

    print("=== [ Multi-Lang & Video Source Scheduler Bot Running ] ===")
    
    asyncio.create_task(publish_worker())
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
