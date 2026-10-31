# Open Shelf: the FastAPI service plus its browser client.
#
#     docker compose up --build         # with PostgreSQL, the usual way
#     docker build -t open-shelf .      # the image on its own
#
# The same image runs in production. tools/start.py migrates, makes sure
# there is data, and then serves on $PORT -- which a host assigns, so the
# process must not pick its own.

FROM python:3.12-slim

# OpenCV's wheels link against these. Without them `import cv2` fails at run
# time with an unhelpful shared-object error rather than at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first and on their own layer: application code changes on every
# commit, this file does not, so the slow OpenCV install stays cached.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a normal user. Nothing here needs root, and a web process that can
# rewrite its own source is a liability.
RUN useradd --create-home --uid 10001 shopkeeper \
    && mkdir -p /app/data \
    && chown -R shopkeeper:shopkeeper /app
USER shopkeeper

ENV PORT=8000
EXPOSE 8000

# The health endpoint reports the site it can actually see -- book count,
# account count, whether the database answered -- so an unhealthy container
# is one that cannot read its own data, not merely one that failed to bind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" | grep -q '"database":"ok"'

CMD ["python", "tools/start.py"]
