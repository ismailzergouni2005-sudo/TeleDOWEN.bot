import os
import asyncio
import logging
from aiohttp import web
from hydrogram import Client, filters

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_HASH = os.environ.get("API_HASH")
API_ID = int(os.environ.get("API_ID", 0))

bot = Client("my_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# سيرفر إبقاء Render يعمل
async def handle_ping(request):
    return web.Response(text="Running")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# الرد على كل الرسائل فوراً للتأكد
@bot.on_message()
async def echo(client, message):
    logging.info(f"تم استقبال رسالة من: {message.from_user.id}")
    await message.reply_text("✅ البوت متصل ويستجيب بنجاح!")

async def main():
    await start_web()
    await bot.start()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 البوت مستعد الآن تماماً!")
    from hydrogram import idle
    await idle()

if __name__ == "__main__":
    asyncio.run(main())
