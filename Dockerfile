FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps for Selenium/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Runtime dirs (will be mounted as volumes in compose)
RUN mkdir -p /app/database /app/exports

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8000"]
