# Travian Agent - Deploy Coolify / Docker
# Dashboard Streamlit (porta 8501) + agente Playwright/Chromium headless.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS=true

WORKDIR /app

# Dipendenze Python + browser Chromium con le librerie di sistema necessarie.
# (playwright install --with-deps usa apt per installare le dipendenze OS)
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

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
