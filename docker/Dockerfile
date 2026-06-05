FROM python:3.11-slim

WORKDIR /app

# ── System deps ──────────────────────────────────────────────────────────────
# tini — small init that reaps zombies, gives clean signal handling for the
#        long-running asyncio loop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ──────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
# The `app/` package contains: bot.py, db.py, draft.py, scoring.py, countries.py.
COPY app/ ./app/

# Heartbeat file dir (mounted as a volume in compose; mkdir here so the path
# also works outside docker-compose).
RUN mkdir -p /app/state

# ── Runtime ──────────────────────────────────────────────────────────────────
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.bot"]
