# Dockerfile – Immobilien-Sniper
# Build:   docker build -t immobilien-sniper .
# Run:     docker run -p 8000:8000 -v $(pwd)/data:/app/data immobilien-sniper

FROM python:3.12-slim

# System-Dependencies für Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxss1 \
    libxtst6 xdg-utils libxkbcommon0 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright-Browser installieren
RUN playwright install chromium
RUN playwright install-deps chromium

# App-Code
COPY . .

# Datenbank-Verzeichnis (persistentes Volume)
RUN mkdir -p /app/data
ENV DATABASE_URL="sqlite:////app/data/immobilien_sniper.db"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
