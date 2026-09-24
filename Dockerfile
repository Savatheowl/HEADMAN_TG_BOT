FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

RUN useradd -m appuser \
    && mkdir -p /app/data /app/downloads \
    && chown -R appuser:appuser /app

USER appuser
CMD ["python", "main.py"]