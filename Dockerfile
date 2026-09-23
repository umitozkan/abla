FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Europe/Istanbul

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 emine \
    && useradd --uid 10001 --gid emine --no-create-home --shell /usr/sbin/nologin emine \
    && mkdir -p /data/receipts \
    && chmod 700 /data /data/receipts \
    && chown -R emine:emine /data

COPY --chown=emine:emine . .
USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
