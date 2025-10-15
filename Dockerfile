# Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# chromium + chromedriver + 한글 폰트
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver fonts-noto-cjk locales && \
    rm -rf /var/lib/apt/lists/*

# 로케일(한글)
RUN sed -i 's/# ko_KR.UTF-8 UTF-8/ko_KR.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen
ENV LANG=ko_KR.UTF-8 LC_ALL=ko_KR.UTF-8

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render가 PORT 환경변수를 넘겨줍니다. 기본 10000도 허용.
CMD gunicorn app:app \
  --bind 0.0.0.0:${PORT:-10000} \
  --worker-class gthread \
  --workers 2 \
  --threads 2 \
  --timeout 90 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --max-requests 50 \
  --max-requests-jitter 20
