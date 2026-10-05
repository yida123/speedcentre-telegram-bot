FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --no-create-home speedbot \
    && mkdir -p /app/data && chown speedbot /app/data
COPY bot ./bot
USER speedbot
CMD ["python", "-m", "bot.main"]
