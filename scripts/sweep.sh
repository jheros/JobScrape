#!/usr/bin/env bash
# Full sweep: discover -> validate -> fetch every open job.
#
# Run this once to build the archive, then again whenever you want a complete
# refresh. Repeat sweeps are far cheaper than the first: postings already stored,
# and postings already evaluated and rejected, skip their detail request.
#
# Every stage is checkpointed — stop it at any time and re-run to continue.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

echo "=== full sweep $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="

echo "--- 1/3 discover ---"
$PYTHON -m jobscrape discover

# Discovered companies contribute nothing until they are probed, and this is
# usually where the largest gain is.
echo "--- 2/3 validate ---"
$PYTHON -m jobscrape validate --concurrency 6

# No date window: collect every currently-open posting.
echo "--- 3/3 fetch ---"
$PYTHON -m jobscrape fetch

$PYTHON -m jobscrape stats
echo "Done. Start the dashboard with: $PYTHON -m jobscrape dashboard"
