# Runs the dashboard (jobscrape/dashboard/app.py) with its in-process
# scheduler — see docs/DOCKER.md for how this is actually deployed.
#
# No secrets baked in: `.env` is a bind mount at runtime (docker-compose.yml),
# never copied into the image. See .dockerignore.
FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary ships its own compiled libpq, so no build-essential/libpq-dev
# needed here — this stays a plain slim image.

# Chromium + Xvfb for `jobright-fetch` (jobscrape/sources/jobright/scraper.py):
# a real (if virtually-displayed) Chrome via Selenium/undetected-chromedriver,
# not the more easily fingerprinted --headless=new. --no-install-recommends
# keeps this from pulling in a full desktop's worth of dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY jobscrape ./jobscrape
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# `.[excel]`: the dashboard's "Export" stage writes xlsx as well as csv
# (jobscrape/dashboard/runner.py STAGES["export"]) — openpyxl is required for
# that, not just a nice-to-have. `.[jobright]` pulls in Selenium,
# undetected-chromedriver, and pyvirtualdisplay for the Chromium above.
RUN pip install --no-cache-dir '.[excel,jobright]'

# Overridden by --host/--port in docker-compose.yml's command; the code's own
# default (127.0.0.1:8000) is deliberately restrictive and is NOT what this
# image runs with in production — see docs/DOCKER.md for why that's a
# conscious choice for this deployment, not an oversight.
EXPOSE 8000

ENTRYPOINT ["python", "-m", "jobscrape"]
CMD ["dashboard"]
