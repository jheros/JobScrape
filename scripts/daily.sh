#!/usr/bin/env bash
# Daily collection run, intended for cron.
#
# Because these APIs only expose currently-open postings, running this daily is
# what makes complete coverage of any future period possible: each run appends
# to the SQLite archive, so postings are captured before they close.
#
# Example crontab entry (06:00 daily):
#   0 6 * * * /path/to/project/scripts/daily.sh >> /path/to/project/data/cron.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

# A 3-day lookback absorbs any run that was missed or interrupted.
SINCE="$($PYTHON -c 'import datetime;print((datetime.date.today()-datetime.timedelta(days=3)).isoformat())')"

echo "=== jobscrape daily run $(date -u '+%Y-%m-%dT%H:%M:%SZ') (since $SINCE) ==="

# Discovery is the slow part and the set of boards changes slowly, so the full
# Common Crawl sweep runs weekly. The other two vectors are cheap and
# independent of that index, so they run every day.
if [ "$(date +%u)" = "7" ]; then
  echo "--- weekly: full discovery ---"
  $PYTHON -m jobscrape discover
else
  echo "--- daily: certificate transparency + curated lists ---"
  $PYTHON -m jobscrape discover --vector ct --vector github
fi

# Chip away at the unvalidated backlog. Discovering companies is worthless until
# they are probed, and this is capped so a daily run stays bounded — leftovers
# are picked up by subsequent runs because validate only looks at 'unchecked'.
echo "--- validating up to 1500 new boards ---"
$PYTHON -m jobscrape validate --limit 1500 --concurrency 6

$PYTHON -m jobscrape fetch --since "$SINCE"
$PYTHON -m jobscrape export --since "$SINCE" --format csv
$PYTHON -m jobscrape stats
