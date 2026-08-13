# Sarmaya OS API.
#
# Kept deliberately plain: a single stage, the platform's own Python base, and
# no build tooling beyond what psycopg2-binary needs. Nothing here is specific
# to one host, so the same image runs on Render, Fly, Railway or a VM.
FROM python:3.12-slim

# Python housekeeping: no .pyc files to bloat the layer, unbuffered output so
# logs appear in the platform's log viewer as they happen rather than when the
# buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code change does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a non-root user. The app never needs to write to its own image —
# uploads go to a mounted path or object storage — so there is no reason to
# hand a web process the ability to rewrite its own code.
RUN useradd --create-home --uid 10001 sarmaya \
    && mkdir -p /app/uploads \
    && chown -R sarmaya:sarmaya /app
USER sarmaya

# The platform tells us the port. 8000 is only the local default.
ENV PORT=8000
EXPOSE 8000

# Migrations run as a separate release step, not here: two instances starting
# at once would both try to migrate, and a failed migration should stop the
# deploy rather than crash-loop a container. See the deployment runbook.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
