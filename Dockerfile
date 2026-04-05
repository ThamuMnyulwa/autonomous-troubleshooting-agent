FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./

RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache -r pyproject.toml

COPY agents/ agents/
COPY intake/ intake/
COPY integrations/ integrations/
COPY glue/ glue/
COPY prompts/ prompts/
COPY config.py db.py mcp_client.py queueing.py ./

# ── Intake API ───────────────────────────────────────────────────────────────
FROM base AS intake

EXPOSE 8080

CMD ["uvicorn", "intake.api:app", "--host", "0.0.0.0", "--port", "8080"]

# ── Worker Job ───────────────────────────────────────────────────────────────
FROM base AS worker

CMD ["python", "-m", "agents.worker"]
