# Running the dashboard in Docker

An alternative to `docs/DEPLOYMENT.md`'s bare-metal path: the dashboard (with
its in-process scheduler — see `jobscrape/dashboard/scheduler.py`) runs
continuously inside a container, and Docker's own restart policy is the
supervisor, instead of systemd.

## This deployment has no authentication and no firewall, by explicit choice

The dashboard has no *required* login on any route — that's true everywhere
in this project, and is normally made safe by binding to `127.0.0.1` only.
**This compose file deliberately overrides that**, binding `0.0.0.0` on
whatever port you choose, with no firewall restricting who can reach it.
Without `JOBSCRAPE_ADMIN_PASSCODE` set (see `.env.example`), anyone who can
reach that port on this network can read API keys off `/settings`, start or
stop any pipeline stage, or trigger a real (non-dry-run) delete via Purge —
there is nothing standing in the way.

This is sound only if you've deliberately accepted that trade-off for a
network you trust (e.g. your own LAN, not the public internet). If you want
the login-free convenience back without the wide-open exposure, three
options, in increasing order of effort: set `JOBSCRAPE_ADMIN_PASSCODE` so
only whoever enters it at `/admin-login` can use Schedule/Runs/Coverage/
Settings (everyone else gets a read-only, blurred view — see
`.env.example`'s own comment); keep the container bound to `127.0.0.1` and
reach it over an SSH tunnel (`ssh -L 8088:localhost:8088 you@host`); or put a
reverse proxy with HTTP basic auth in front of it. The passcode option is a
lightweight visitor distinction, not real accounts — treat it the same as
any other shared secret in `.env`.

## Install

```bash
# Copy the repo to the server (this project has never been pushed to a git
# remote, so this is a direct transfer, not a clone):
rsync -az --exclude .git --exclude data --exclude .env --exclude __pycache__ \
    ./ user@host:/opt/jobscrape/
```

## Configure

Same env vars as `docs/DEPLOYMENT.md`'s **Configure** section — copy
`.env.example` to `/opt/jobscrape/.env` on the server and fill in what you
need. `DATABASE_URL` is the one difference worth calling out here: with
`network_mode: host` (below), the container reaches the server's own Postgres
at `localhost`, exactly as if the app were running directly on the host —
`postgresql+psycopg2://jobscrape:<password>@localhost/jobscrape`.

If the server's Postgres has no `jobscrape` role/database yet:

```bash
sudo -u postgres psql -c "CREATE ROLE jobscrape WITH LOGIN PASSWORD '<generated>';"
sudo -u postgres psql -c "CREATE DATABASE jobscrape OWNER jobscrape;"
```

Generate a real password for this — don't reuse anything you've typed
elsewhere (e.g. the server's own SSH password).

`jobright-fetch` also needs `JOBRIGHT_EMAIL`/`JOBRIGHT_PASSWORD`. Leave
`JOBRIGHT_CHROME_VERSION` **unset** here even if your own machine's `.env`
has one — it names a locally-installed Chrome's major version, and the
image installs its own Chromium via `apt`, whose version drifts over time
and won't generally match. Blank lets `undetected-chromedriver` auto-detect
whatever's actually in the container.

## Build and start

```bash
cd /opt/jobscrape
docker compose build
# On a genuinely fresh database, `alembic upgrade head` fails partway through:
# migration 0001 already replays the full current schema (db.SCHEMA), and most
# migrations after it (0002-0012) lack `IF NOT EXISTS` guards, so they collide
# with columns 0001 already created. Instead, let the app create the schema
# directly (its own ensure_schema() path, already idempotent), then stamp
# Alembic's version table without replaying history:
docker compose run --rm --entrypoint python dashboard -c \
    "from jobscrape import db; db.connect()"
docker compose run --rm --entrypoint alembic dashboard stamp head
docker compose up -d
```

`network_mode: host` in `docker-compose.yml` is why there's no `-p` port
mapping to reason about: whatever `--host`/`--port` the app binds to inside
the container is that same address on the host directly. It's also why the
container reaches Postgres at `localhost` with no extra network
configuration — this only works cleanly on a real Linux host, not Docker
Desktop's VM.

`.env` and `data/` are bind-mounted (see `docker-compose.yml`), not baked into
the image — `.env` specifically has to stay a real, writable file on the host,
since the Settings page (`jobscrape/dashboard/envfile.py`) edits it live and
those edits need to survive a container rebuild.

## Monitoring

```bash
docker compose ps                 # Up?
docker compose logs -f            # "Dashboard on http://0.0.0.0:8088", "Scheduler started"
docker compose restart            # simulates a crash — confirms the scheduler comes back alone
```

Same `/health` endpoint and `runs` table as the bare-metal deployment — see
`docs/DEPLOYMENT.md`'s **Monitoring** section, unchanged here.

## Updating

```bash
# from your machine:
rsync -az --exclude .git --exclude data --exclude .env --exclude __pycache__ \
    ./ user@host:/opt/jobscrape/
# on the server:
cd /opt/jobscrape && docker compose build && docker compose up -d
```

`docker compose up -d` recreates the container on a new image without
touching the bind-mounted `.env`/`data/` or the database.
