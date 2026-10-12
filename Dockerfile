# The web half of the bookstore: API + browser client.
#
#     docker build -t smart-bookstore .
#     docker run --rm -p 8000:8000 smart-bookstore
#     open http://localhost:8000/app/
#
# The Tkinter desktop client is deliberately NOT what this image runs. A GUI
# needs a display server, and shipping one inside a container to run a window
# nobody can see would be theatre. The desktop client runs with `python run.py`
# on a real machine; the container serves the same rules over HTTP.
#
# Face and voice login are likewise unavailable here: a container has no
# camera and no microphone. The app already degrades to a clear message when
# hardware is missing, which is exactly the behaviour you get in the image.

FROM python:3.12-slim AS base

# OpenCV's wheels are built against these; without them `import cv2` fails at
# run time with an unhelpful shared-object error rather than at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
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

# A shop with no books is a bad first impression, so the image ships with the
# demo shop already built: sixteen titles, six accounts covering every role
# and state in the specification, generated covers, and reviews arranged so
# the to-order and no-order lists are populated on first look.
#
# This happens at build time, into the image. Mounting your own directory over
# /app/data at run time replaces it wholesale and nothing here overwrites it:
#     docker run -p 8000:8000 -v "$PWD/data:/app/data" smart-bookstore
RUN python tools/seed.py --reset \
    && python -c "import json; print('seeded', len(json.load(open('data/books.json'))), 'books')"

# Run as a normal user: nothing here needs root, and a web process that can
# rewrite its own source is a liability.
RUN useradd --create-home --uid 10001 shopkeeper \
    && chown -R shopkeeper:shopkeeper /app
USER shopkeeper

EXPOSE 8000

# The health endpoint reports the shop it is actually serving (book count,
# account count, which face detector loaded), so an unhealthy container is
# one that cannot read its own data -- not merely one that failed to bind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "bookstore.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
