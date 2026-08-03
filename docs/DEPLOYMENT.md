# Deploying the scheduled collector

For running `scripts/schedule.sh` on a server — the website's host, or anything else with
a cron daemon and network access. The dashboard is not part of this: it binds to localhost
and has no authentication on any of its mutating routes, so it stays a local tool.

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

## Schedule

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
