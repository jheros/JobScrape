#!/usr/bin/env bash
# Scheduled collection, in two modes.
#
#   schedule.sh daily    last 24 hours    Monday-Saturday
#   schedule.sh weekly   last 2 months    Sunday
#
# Both run the whole pipeline — discover, validate, fetch, dedupe — straight
# into Postgres. There is no export: the database IS the deliverable.
#
# Crontab, 07:00 GMT+7:
#
#   CRON_TZ=Asia/Bangkok
#   0 7 * * 1-6  /srv/jobscrape/scripts/schedule.sh daily  >> /srv/jobscrape/data/cron.log 2>&1
#   0 7 * * 0    /srv/jobscrape/scripts/schedule.sh weekly >> /srv/jobscrape/data/cron.log 2>&1
#
# CRON_TZ is honoured by Vixie cron (Debian/Ubuntu) and cronie (RHEL). If your
# cron does not support it, the same schedule in UTC is `0 0 * * 1-6` and
# `0 0 * * 0` — 07:00 GMT+7 is 00:00 UTC. Getting this wrong shifts every run by
# seven hours and nothing will tell you.
#
# WHY MONDAY-SATURDAY AND NOT EVERY DAY. Both jobs are wanted at 07:00, so
# running the daily seven days a week would start it at the same instant as the
# weekly, every Sunday. Two concurrent fetches are the one failure this project
# has no defence against: they read-modify-write the same board_health and
# zero_yield counters with no row locking, interleave closures with upserts so
# postings flap closed and open, and double the request rate against the same
# hosts — and 429s cause under-collection, which is a correctness problem, not a
# courtesy one. The weekly's 2-month window is a strict superset of the daily's
# 24 hours, so Sunday loses nothing by skipping the daily.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

MODE="${1:-}"
case "$MODE" in
  daily|weekly) ;;
  *) echo "usage: $(basename "$0") {daily|weekly}" >&2; exit 2 ;;
esac

# Belt and braces on top of the Mon-Sat/Sunday split above: a weekly sweep that
# overruns 24 hours would still meet Monday's daily. `flock -n` makes the second
# one exit here instead of corrupting board state. It ships with util-linux on
# any Linux server; on a host without it, the split alone is the guard.
LOCK="${TMPDIR:-/tmp}/jobscrape-schedule.lock"
if command -v flock > /dev/null 2>&1 && [ -z "${_JOBSCRAPE_LOCKED:-}" ]; then
  # Re-run this script under the lock. `-E 99` gives contention its own exit
  # code so it is distinguishable from the pipeline itself failing with 1 —
  # without it, "someone else is running" and "the fetch crashed" look the same
  # to cron, and only one of them deserves an alert.
  #
  # NOT `exec flock ...`: exec replaces this shell, so nothing after it can run
  # and the contention branch below would be unreachable.
  export _JOBSCRAPE_LOCKED=1
  if flock -n -E 99 "$LOCK" "$0" "$@"; then
    exit 0
  else
    rc=$?
    if [ "$rc" -eq 99 ]; then
      echo "another jobscrape run holds $LOCK — skipping this $MODE run"
      exit 0
    fi
    exit "$rc"
  fi
fi

# Date arithmetic through Python, not `date -d`: the latter is GNU coreutils and
# is not what BSD/macOS ships, and this script should behave the same wherever
# it is deployed.
days_ago() { $PYTHON -c "import datetime,sys;print((datetime.date.today()-datetime.timedelta(days=int(sys.argv[1]))).isoformat())" "$1"; }

if [ "$MODE" = "daily" ]; then
  # "Last 24 hours" with a calendar-date flag. `--since` takes YYYY-MM-DD and no
  # time of day, so asking for today's date at 07:00 would miss everything
  # posted between 07:00 and midnight yesterday. Yesterday is the honest
  # translation, and it overlaps the previous run rather than leaving a gap.
  SINCE="$(days_ago 1)"
  VECTORS=(--vector ct --vector github)   # cheap, and independent of the CC index
  VALIDATE_LIMIT=1500
else
  SINCE="$(days_ago 60)"
  VECTORS=()                              # every vector, including Common Crawl
  VALIDATE_LIMIT=6000
fi

echo "=== jobscrape $MODE run $(date -u '+%Y-%m-%dT%H:%M:%SZ') (since $SINCE) ==="

# Discovery finds boards; validation is what makes them reachable. Neither pays
# off in the same run — a board discovered now is fetched by the next one — which
# is why they come first and are capped rather than skipped.
echo "--- discover ---"
$PYTHON -m jobscrape discover "${VECTORS[@]+"${VECTORS[@]}"}"

echo "--- validate (up to $VALIDATE_LIMIT unchecked boards) ---"
$PYTHON -m jobscrape validate --limit "$VALIDATE_LIMIT" --concurrency 6

# Note on cost: a NARROW window is nearly as cheap as no window at all, because
# `skippable_ids` skips any previously-rejected posting whose date falls outside
# it. The 2-month window is the expensive one — it re-downloads every posting
# rejected on a date basis in the last 60 days. That is the trade the weekly
# exists to make, and it is also what catches the ~1% of postings that carry no
# date at all, which any window silently drops.
echo "--- fetch (since $SINCE) ---"
$PYTHON -m jobscrape fetch --since "$SINCE"

# Aggregator rows are only collapsed against an ATS row when something
# re-clusters them; a fetch does that per-company, so cross-source pairs found
# by two different boards need this pass.
echo "--- dedupe ---"
$PYTHON -m jobscrape dedupe --backfill

$PYTHON -m jobscrape stats
echo "=== $MODE run finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
