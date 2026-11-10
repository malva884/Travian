# Travian Agent - Deploy Coolify / Docker
# Dashboard Streamlit (porta 8501) + agente Playwright/Chromium headless.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS=true

WORKDIR /app

# Dipendenze Python + browser Chromium con le librerie di sistema necessarie.
# libatomic1 va installato PRIMA: il driver node di playwright non parte senza
# (libatomic.so.1 mancante su python:*-slim). --with-deps installa il resto.
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Directory persistenti (montare un volume su /app/state su Coolify)
RUN mkdir -p /app/state /app/debug

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/healthz', timeout=5)" || exit 1

CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
