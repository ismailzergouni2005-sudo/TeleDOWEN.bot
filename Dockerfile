# نأخذ فقط ملف telegram-bot-api الجاهز (binary مُصرَّف مسبقاً) من صورة جاهزة
# بدل تجميعه بأنفسنا (يستغرق دقائق طويلة ويحتاج مكتبات بناء ثقيلة)
FROM aiogram/telegram-bot-api:latest AS botapi

FROM python:3.11-slim

# ffmpeg مطلوب لمعالجة/تحويل الصوت والفيديو
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# انسخ الـ binary الجاهز لسيرفر Bot API المحلي
COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# مجلد بيانات سيرفر Bot API المحلي
RUN mkdir -p /app/tdata

# البوت يتحدث مع السيرفر المحلي عبر localhost داخل نفس الحاوية
ENV LOCAL_BOT_API_URL=http://localhost:8081

RUN chmod +x start.sh

CMD ["./start.sh"]
