# Deploying the scheduled collector

Scheduling now lives **inside the dashboard process** (`jobscrape/dashboard/scheduler.py`):
add, edit, enable/disable and force-cancel schedules from the dashboard's `/schedule` page,
and edit `.env` values (API keys, `APIFY_TOKEN`, tuning) from `/settings` — no terminal
needed for either. The dashboard still binds to localhost and has no authentication on any
of its mutating routes, so it stays a local tool even though it can now edit secrets.

The catch: a schedule only fires while the dashboard process is running. `scripts/schedule.sh`
+ crontab (below) survives an unattended reboot for free; the in-process scheduler does not,
unless the dashboard itself runs as a persistent service — see **Run the dashboard as a
service** below, which is the recommended path if you want "set it and forget it" scheduling.
`scripts/schedule.sh` and the crontab lines are kept working as a fallback for anyone who'd
rather not run the dashboard continuously, but don't run both at once: two schedulers with no
shared lock reintroduces the exact double-run race `flock` exists to prevent.

## Install

```bash
git clone <this repo> /srv/jobscrape && cd /srv/jobscrape
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

The script honours `$PYTHON`, so point cron at the venv rather than activating it:

```cron
PYTHON=/srv/jobscrape/.venv/bin/python
```

## Configure

Copy `.env.example` to `.env` and fill in what this host actually needs. Only the first is
required:

| key | why |
| --- | --- |
| `DATABASE_URL` | the Postgres the website reads. `postgresql+psycopg2://user:pass@host/jobscrape` |
| `JOBSCRAPE_CONTACT` | an email address sent with every request, so a board operator can reach you rather than just blocking you |
| `JOOBLE_API_KEY` | Jooble — the highest keep-rate source in the archive, 83% |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna |
| `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | federal postings |
| `APIFY_TOKEN` | Indeed and LinkedIn postings via `apify-pull` (below). Get one at apify.com → Settings → Integrations. This is the only key `apify-pull` needs — no endpoint, no public route, nothing else to deploy |
| `APIFY_WEBHOOK_SECRET` | only if this host ALSO runs `jobscrape webhook` — a separate, opt-in push route. Not needed for `apify-pull` |

A missing key is a **skip with a message, never an error** — the run continues without
that source. So a first deployment with only `DATABASE_URL` works, and each key you add
widens coverage.

Then bring the schema up to date:

```bash
alembic upgrade head
```

## Run the dashboard as a service (recommended, for in-dashboard scheduling)

`Type=simple`, not `oneshot`: this process stays running, and its own scheduler fires
on the times configured at `/schedule`.

```ini
# /etc/systemd/system/jobscrape-dashboard.service
[Unit]
Description=jobscrape dashboard

[Service]
Type=simple
WorkingDirectory=/srv/jobscrape
Environment=PYTHON=/srv/jobscrape/.venv/bin/python
ExecStart=/srv/jobscrape/.venv/bin/python -m jobscrape dashboard
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable with `systemctl enable --now jobscrape-dashboard.service`. `Restart=on-failure` is
also what makes the Settings page's "Restart dashboard" button (after an env edit that needs
one) actually come back on its own — without a supervisor, that button just stops the
process and you start it again by hand.

## Schedule (fallback: pure cron, no dashboard required)

Only needed if you're not running the dashboard as a service above — otherwise configure
schedules at `/schedule` instead, and skip this section.

```cron
CRON_TZ=Asia/Bangkok
PYTHON=/srv/jobscrape/.venv/bin/python
0 7 * * 1-6  /srv/jobscrape/scripts/schedule.sh daily  >> /srv/jobscrape/data/cron.log 2>&1
0 7 * * 0    /srv/jobscrape/scripts/schedule.sh weekly >> /srv/jobscrape/data/cron.log 2>&1
```

See the README's **Scheduling** section for why the daily is Monday–Saturday rather than
every day, and for the UTC equivalents if your cron ignores `CRON_TZ`.

The daily leg also runs `apify-pull --last 3` — the last three succeeded runs of your
configured Indeed and LinkedIn actors, upserted into this same database. It is a no-op
without `APIFY_TOKEN`: the step logs a line and exits 0 rather than failing the schedule,
so it is safe to leave in place before the key exists and before the actors have ever run.
Once both are true, postings start arriving the next time this fires — no separate command
needed, and nothing to add anywhere else.

`data/cron.log` grows without bound; give it a logrotate rule:

```
/srv/jobscrape/data/cron.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

### systemd timers instead

Strictly better than cron if the host is ever off or asleep, because `Persistent=true`
runs a missed job on the next boot. Two units per schedule:

```ini
# /etc/systemd/system/jobscrape-daily.service
[Service]
Type=oneshot
WorkingDirectory=/srv/jobscrape
Environment=PYTHON=/srv/jobscrape/.venv/bin/python
ExecStart=/srv/jobscrape/scripts/schedule.sh daily

# /etc/systemd/system/jobscrape-daily.timer
[Timer]
OnCalendar=Mon..Sat *-*-* 07:00:00 Asia/Bangkok
Persistent=true
[Install]
WantedBy=timers.target
```

The weekly is the same with `OnCalendar=Sun *-*-* 07:00:00 Asia/Bangkok` and
`schedule.sh weekly`. Enable with `systemctl enable --now jobscrape-daily.timer`.

## Monitoring

The first run takes hours; later ones are far cheaper, because postings already stored and
postings already evaluated-and-rejected skip their detail request.

- **`jobscrape stats`** — printed at the end of every scheduled run, so the log tail is
  the report.
- **`/health`** on the dashboard, if you run one: it checks Postgres, the **age of the
  last successful run**, and disk headroom, returning 503 when any of those is unhealthy.
  That is the endpoint to point an uptime monitor at — a schedule that silently stopped
  looks exactly like one that is running fine until you check.
- **The `runs` table** — every fetch opens a row and closes it, including on a crash
  (`status='failed'` with the error text). A row stuck at `running` means the process was
  killed hard enough to skip its own bookkeeping.

```sql
SELECT started_at, ended_at, status, error FROM runs ORDER BY id DESC LIMIT 10;
```

## What the website reads

Nothing in the pipeline writes to the website. It reads the `jobs` table directly; the
columns and their meanings are in the README's schema section. Two filters matter for any
query behind a job listing:

```sql
WHERE closed_at IS NULL       -- still open
  AND duplicate_of IS NULL    -- not suppressed as a repost or cross-source copy
```

## Sending jobs to other systems (Endpoints)

Another application can receive jobs from this one over HTTP. Add it on the dashboard's
**Endpoints** page (`/endpoints`, under Admin in the sidebar — Settings links to it too):

- **Name** — unique; shown in run logs.
- **URL** — must be `https://`. Plain `http://` is accepted only for `localhost`,
  `127.0.0.1` or a literal private IP (e.g. `http://192.168.1.20:3000/import`). URLs with a
  username or password in them are refused.
- **Token** — sent as `Authorization: Bearer <token>`. It is write-only: the page shows
  only "Token set", and no page, API response or log ever contains it. When editing, leave
  it blank to keep the current one.

**Set `JOBSCRAPE_ADMIN_PASSCODE` before using this.** With it blank every visitor is an
admin, so anyone who can reach the dashboard could add an endpoint and receive your jobs.

Row actions: **Test connection** posts `{"rows": []}` once and reports the HTTP status
(it changes nothing). **Send now** starts an `endpoint-sync` run immediately; it sends
pending changes to every enabled endpoint. **Resend everything** clears that endpoint's
place and re-sends all open jobs. **Edit**, **on/off** and **Delete** do what they say.

### When jobs are sent

With the dashboard running as a service, an `endpoint-sync` run starts automatically after
every stage finishes (a schedule firing, a Runs-page click, "Run now"), whenever at least
one endpoint is enabled. This is independent of `RENDER_DATABASE_URL`, and it never
follows a `render-sync` or another `endpoint-sync`.

Without the dashboard's scheduler, run it from cron after your pipeline (endpoints are
still added once on the Endpoints page):

```cron
CRON_TZ=Asia/Bangkok
PYTHON=/srv/jobscrape/.venv/bin/python
30 * * * *  cd /srv/jobscrape && $PYTHON -m jobscrape endpoint-sync >> /srv/jobscrape/data/cron.log 2>&1
```

In Docker: `docker compose exec dashboard python -m jobscrape endpoint-sync`.

```
python -m jobscrape endpoint-sync [--endpoint NAME] [--dry-run] [--limit N]
```

`--dry-run` makes no requests and saves nothing. `--limit` still sends for real; it caps
rows per endpoint for that run. With no endpoint enabled the command logs a skip and exits
0. A second run while one is in progress also skips (a Postgres advisory lock). The run is
recorded on the Runs page as stage `endpoint-sync`, and as `error` if any endpoint failed.

### What is sent

Each endpoint keeps its own place: the `(changed_at, source, source_job_id)` of the last
row it accepted, where `changed_at` is the newest of `last_seen_at`, `closed_at` and
`duplicate_marked_at` — the same definition `render-sync` uses. One endpoint failing never
stops or delays the others; it just catches up on its next successful run.

- **First sync** (or after Resend everything): only open, non-duplicate jobs.
- **Later syncs**: every row that changed, including ones that closed or were marked as
  duplicates, so the receiver can close them.
- **Never sent**: jobs from `linkedin`, `indeed` or `avature`.

### Request

```
POST <url>
Authorization: Bearer <token>
Content-Type: application/json

{"rows": [ { ...one object per job... } ]}
```

At most 500 rows and 4 MiB of JSON per request. A single job larger than 4 MiB on its own
is logged and skipped. Each row has exactly these keys, with JobScrape's own column names:

`source`, `source_job_id`, `title`, `company`, `job_url`, `apply_url`,
`description_text`, `workplace_type`, `ai_workplace_type`, `city`, `state`, `country`,
`location_raw`, `seniority`, `ai_seniority`, `salary_min`, `salary_max`,
`salary_currency`, `employment_type`, `skills` (comma-separated text), `department`,
`industry`, `ai_requires_clearance`, `posted_at`, `closed_at` (ISO 8601 or null),
`duplicate_of` (id of the job this one duplicates, or null), `scam_signals`.

`(source, source_job_id)` is unique and is the key a receiver should upsert on.
Datetimes are ISO 8601 with offset, e.g. `2026-07-01T00:00:00+00:00`.

After the last batch, if the receiver reported any inserted, updated, reopened or closed
rows during the run, one more request carries the run's totals:

```json
{"rows": [], "announce": {"inserted": 12, "updated": 40, "reopened": 0, "closed": 3}}
```

### Response

Any 2xx is success; the place is saved after each one. The body should be:

```json
{"inserted": 0, "updated": 0, "reopened": 0, "closed": 0, "skipped": 0,
 "rejected": [{"index": 3, "externalId": "greenhouse:123", "message": "why"}]}
```

Missing keys count as 0. Rejected rows are not re-sent; the count and the first 20
messages go into the run's log.

Redirects are never followed. Network errors, `429` and `5xx` are retried three times
(after 2 s, 8 s and 30 s); any other status (`401`, `403`, `404`, a `3xx`...) stops that
endpoint for this run without moving its place. The timeout is 120 s per request. Each
endpoint's last sync time, status (`ok`/`error`) and message show on the Endpoints page.
