#!/bin/sh
set -e

if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ]; then
    echo "❌ متغيرا البيئة TELEGRAM_API_ID و TELEGRAM_API_HASH مطلوبان."
    echo "احصل عليهما مجاناً من https://my.telegram.org (API development tools)."
    exit 1
fi

echo "🚀 تشغيل سيرفر Bot API المحلي على المنفذ 8081..."
telegram-bot-api \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --http-port=8081 \
    --dir=/app/tdata \
    --log=/app/tdata/tba.log &

# ننتظر السيرفر المحلي حتى يبدأ الاستماع فعلياً قبل تشغيل البوت
echo "⏳ انتظار جاهزية سيرفر Bot API المحلي..."
for i in $(seq 1 20); do
    if curl -sf "http://localhost:8081/" >/dev/null 2>&1; then
        echo "✅ سيرفر Bot API المحلي جاهز."
        break
    fi
    sleep 1
done

echo "🚀 تشغيل البوت..."
exec python3 bot.py
