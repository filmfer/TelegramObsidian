FROM python:3.12-slim

# Install tini for clean shutdown + Tesseract (OCR images later)
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr libmagic1 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user for security
RUN useradd -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "bot"]
