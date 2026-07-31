# Architecture & process flow

How the collector works end to end: what runs, in what order, and why each piece is
shaped the way it is.

The numbers quoted throughout are **measured**, not estimated — they come from runs
against live endpoints and are reproducible with the commands shown.

---

## The shape of it

```text
                    ┌──────────── ENTRY POINTS ────────────┐
                    │  CLI          dashboard      scripts  │
                    │  python -m    (FastAPI)      sweep.sh  │
                    │  jobscrape                   daily.sh  │
                    └──────────────────┬───────────────────┘
                                       │
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
   1. DISCOVER    2. VALIDATE      3. FETCH        4. EXPORT     5. TRIAGE
   find boards    probe them       get jobs        csv/xlsx      you decide
        │              │          (+ dedupe)           │              │
        └──────────────┴───────────────┴───────────────┴──────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────────┐
                        │      PostgreSQL archive       │
                        │ companies · jobs · job_status │
                        │ posting_seen · runs · events  │
                        │ schema managed by Alembic     │
                        └──────────────────────────────┘
```

Every stage is **checkpointed and resumable**. Stop any of them and re-run; none of them
restart from zero.

---

## Stage 1 — Discover: which companies exist

Fetching a job is easy. Knowing *which employers exist* is the hard part, and it is the
real determinant of coverage. Five independent inputs merge into the `companies` table.

| Input | Scale | Notes |
| --- | --- | --- |
| **Bundled seeds** | 174 boards | Verified live at build time. The floor when everything else is unavailable. |
| **Common Crawl** (`cc`) | ~7,600 per index page | 16 URL patterns. Highest volume by far. Completed pages are checkpointed to `data/cc_progress.json`. |
| **Certificate transparency** (`ct`) | 9 platforms | Every subdomain-per-customer board needs a TLS certificate, and certificates are public. Found **84 Teamtailor boards against Common Crawl's 6**. |
| **Curated GitHub lists** (`github`) | 5 repos | Small but high quality — every entry is a real hiring tech company, and it is the main source of Workday tenant triples. |
| **Published tenant lists** (`import-tenants`) | 49,567 slugs | Somebody else's discovery, already done. MIT-licensed only. Measured **77% live** on a 2,080-board sample — by far the best yield per unit of effort of any vector. |

```bash
python -m jobscrape discover                  # all three vectors
python -m jobscrape discover --vector ct      # just certificate transparency
```

**`ct` and `github` do not depend on Common Crawl.** That matters more than it sounds:
during development that index began refusing TLS handshakes entirely (IP rate-limiting
after heavy use). When that happens, discovery logs a warning, falls back to the bundled
seeds, and the other two vectors carry on rather than the run failing.

Slugs pass a junk filter (`_clean` in `discovery/commoncrawl.py`) rejecting numeric ids,
asset paths and hash-like strings, plus a vendor-subdomain list so `www.bamboohr.com` is
never probed as if it were a customer.

### Published tenant lists

```bash
python -m jobscrape import-tenants path/to/ats-companies --dry-run
python -m jobscrape import-tenants path/to/ats-companies
```

A separate command rather than a `discover` vector, because it reads a local directory
rather than the network and is run once per list refresh, not on every sweep.

**Normalisation is the whole job.** Each list publishes whatever identifier its own
scraper used, and those are not our slugs. Workday ships `23andme/23` where we need
`23andme|wd5|23` — and the `wd5` exists *only* in the URL column, so importing the slug
column verbatim produces boards that all 404. Six platforms need real parsing (Workday,
Cornerstone, Oracle, UKG, ADP, iCIMS) and three need lighter fixes (Paycom, Ashby,
Jobvite); the rest pass through. A row that cannot be normalised is skipped **and
counted** — a wrong slug costs a probe and lands as a permanently dead company, and
silence about how many were dropped would make a partial import look complete.

Imported as `unchecked`, so Stage 2 decides what is real; a re-import never resets a
board that has already been validated.

**Licensing is checked per source, not per repo.** `ats-scrapers` is MIT throughout
including its CSVs. `job-board-aggregator`'s code is MIT but its `data/` is CC BY-NC 4.0,
so its ~60k slugs are deliberately not used.

**Output:** rows in `companies` with `status='unchecked'`.

## Stage 2 — Validate: which of those are real

The step that is easy to skip and shouldn't be.

Each slug is probed **once** with a cheap listing-only call (`fetch_board(with_details=False)`)
and recorded as `live`, `empty` or `dead`. The Common Crawl index contains a great deal
of junk and many historical boards that no longer exist; this is where they are removed.

Observed live rates: Ashby 86%, Greenhouse 80%, SmartRecruiters 77%, Workday 54%.

> This produced the single largest gain in the project's history. Working through a
> backlog of unvalidated boards took the collection from **405 live boards to 3,727** —
> a 9.2× increase — with no new code at all. Discovering companies does nothing until
> they are probed.

Progress commits every 50 boards, so an interrupted run keeps everything it did.

## Stage 3 — Fetch: get the jobs

Two source families run in sequence: 19 per-company board sources, then 11 search-style
global sources.

### 3a. Board sources — the coverage backbone

For each live board, concurrently:

```text
 1. skippable_ids(source, slug, since, until)
      = stored job ids  ∪  postings already evaluated and rejected

 2. fetch_board(slug, prefilter, known_ids)
      · one-request sources  → the listing carries everything
      · two-request sources  → listing, then a detail call ONLY for ids
                               not already in known_ids
      returns None  = the board did not respond
      returns []    = the board exists and has nothing posted

 3. if None → stop here (or a board_health failure this severe repeats).
      Closures are NEVER inferred from a failed fetch. A board that keeps
      failing gets an exponential cooldown (board_health, see below) so a
      struggling endpoint isn't hit at full frequency forever.

 4. mark_board_closures(seen_ids)
      anything the board no longer lists is marked closed — but only once
      it has been unseen for CLOSURE_GRACE_HOURS (default 48h), not on the
      first absence. Boards paginate inconsistently under load; judging
      closure from one listing snapshot was measured to wrongly close
      thousands of still-open Workday postings.
      Done BEFORE filtering — a posting outside your date window is
      still open, and marking it closed would be wrong.

 5. apply_filters_counted()   → excluded roles (QA, management), then role,
                                 then region, then date
 6. record_rejected()         → so the next sweep skips them too
 7. upsert_jobs()             → COALESCE preserves already-captured detail;
                                 a re-upsert also reopens a closed posting
 8. dedupe.resolve_scoped()    → collapse reposts/cross-source copies for
                                 the board just fetched (see Deduplication)
```

Two optimisations live in that path, both measured rather than assumed:

**The prefilter** (step 2) drops obviously-irrelevant postings using listing data alone,
before spending a detail request. On a sample Ashby board: **116 → 34 requests, identical
output**. It is deliberately *more permissive* than the real filters — anything uncertain
passes through, because a wasted request costs one HTTP call while a wrongly skipped one
loses the job permanently. `tests/test_prefilter.py` asserts that invariant across every
title × location × department combination.

**`known_ids` plus rejected memory** (steps 1 and 6) skip the detail request entirely for
postings already handled. Measured on live Ashby boards: **48% fewer requests, 1.9×
faster**. The second half matters as much as the first — on those boards 158 of 312
postings were being re-downloaded on *every* sweep only to be filtered out again, because
they were fetched and then dropped rather than stored. `posting_seen` remembers them.

A rejected posting's date is stored too, so widening your date window re-examines it
rather than skipping it forever.

**Picking the cheapest endpoint a platform offers.** Two adapters were rewritten after
measuring what their providers actually support, and both changes were large:

- **Ashby** publishes a REST board (`api.ashbyhq.com/posting-api/job-board/{slug}`) that
  returns *every* posting with its description, publish date and compensation in **one
  request**. The GraphQL path needs one list call plus one detail call per posting for the
  same data. Measured on the same 116-posting board: **1 request / 1.9s vs 117 requests /
  82.4s** — identical output. GraphQL is kept as a fallback, which also disambiguates
  something the HTTP layer can't: `Fetcher` returns `None` for a 404 and a transient
  failure alike, so a REST miss asks GraphQL before concluding a board is dead.
- **Workday** has no "list everything" call, but an **empty** `searchText` enumerates the
  whole board where keyword search silently under-collects. Live: AIG 475 postings vs 28,
  3M 655 vs 41, NVIDIA 2,000 vs 1,694. After filtering, that is **AIG 15 → 37 real
  software roles, 3M 23 → 61, AES 8 → 14**. Keyword search can't be fixed by adding more
  terms — it misses whatever doesn't happen to match, so it under-collects rather than
  merely running slower. Keywords remain as a fallback for tenants that reject the empty
  search.

Stopping at Workday's reported `total` is a correctness requirement, not an optimisation:
it keeps serving full pages past the end of the result set (offset 2,000 on a
2,000-posting board returns 20 more rows), so pagination waiting for a short page to
signal the end would never terminate.

### 3b. Global sources — search APIs

Swept with 12 software search terms, paginated, deduped per source.

- **No key needed:** RemoteOK, Arbeitnow, Hacker News, Himalayas, Remotive, Jobicy,
  We Work Remotely
- **Free key needed:** USAJobs, Adzuna, Jooble, JSearch

A missing key is a **skip with an explanatory message**, never an error. The run
continues.

Pagination limits here are tunable, and the defaults were raised after checking that the
old ones genuinely bound rather than being theoretical ceilings — Himalayas was still
returning full pages at page 60 under a 50-page cap (**1,000 → 2,688 postings** per run
once lifted), and Jooble reports ~196,000 matches for a single term where 10 pages saw
200. See `JOBSCRAPE_HIMALAYAS_MAX_PAGES` / `JOOBLE_MAX_PAGES` in `config.py`.

Global sources also get the same closure treatment as boards, with one difference worth
knowing: `mark_board_closures` is called with `company_slug=None`, meaning "skip
per-company scoping — treat this source's whole result set as one very large board." That
is deliberately *not* the same as `company_slug IS NULL`, since slug derivation gives even
aggregator rows a slug.

**A source-wide close is only sound against a crawl that walked its whole result set.**
Every one of these adapters stops early and returns what it has when a page fails, a page
budget runs out, or `--max-results` caps it — so a rate-limited run is indistinguishable
from a genuinely shrunken catalogue unless the adapter says which it was. Closing against a
truncated "seen" set retires postings the run never reached, and the 48h grace hides that
until failures repeat, which is exactly when rate-limiting happens.

So each adapter returns a `JobBatch` carrying `complete`, and closure is skipped unless it
is true. The default is `False` and a plain list carries no flag at all, so an adapter that
forgets to say under-closes rather than deleting live jobs. The sampling sources
(`hackernews`, `jsearch`) report `complete=False` unconditionally — by construction they
read a slice, not a catalogue, so they can never be a basis for closing anything, which is
what the liveness probe exists to cover.

### 3c. Normalisation — every adapter converges here

`Job.__post_init__` in `models.py` does this once so no adapter repeats it:

- **Dates** → UTC ISO. Handles epoch seconds, epoch milliseconds, and fractional seconds
  of *any* precision. This is not hypothetical: Jooble sends 7 digits and USAJobs sends 4,
  while Python's `fromisoformat` natively accepts only 3 or 6. Before this was handled,
  both sources parsed as undated and every one of their jobs was silently dropped.
- **HTML** → plain text; double-unescaped (several sources double-encode), scripts stripped.
- **Salary** → parsed from description text where no structured field exists, validated
  against realistic annual and hourly bands so funding announcements and discounts are
  not mistaken for compensation.
- **`workplace_type`** → `remote` / `hybrid` / `onsite` / `unknown`, decided by a
  four-tier cascade in `classify_workplace`, first tier to answer wins:
  1. **The source's own field** (Lever/Ashby `workplaceType`, Workday `remoteType`,
     Oracle `WorkplaceType`, Pinpoint `workplace_type`). Highest trust — the employer said
     it. Oracle puts it on the *list* response, so it costs no extra request.
  2. **A structured boolean** (SmartRecruiters `location.remote`, Workable `telecommuting`,
     USAJobs `RemoteIndicator`).
  3. **Wording** in title + location + the first **1,500 characters** of the description.
     Hybrid is matched before remote, because "hybrid remote" and "remote-friendly" are
     hybrid. The 1,500 cap is load-bearing: widening it to 6,000 resolves ~24% more
     postings but roughly **half of those are wrong**, including inversions — "This is not
     a remote position" reads as remote, "On-site gym" as onsite.
  4. **A permissive remote sweep** over the *whole* posting: any word in
     `models.REMOTE_MENTION` makes it remote. Deliberately loose, and deliberately **last**
     — it only ever fills a blank, so an explicit hybrid or onsite verdict is never
     overwritten. Applied globally it would flip 33% of hybrid and 22% of onsite rows,
     since hybrid postings routinely say "2 days remote".
- **`seniority`** → `intern` / `entry` / `mid` / `senior` / `staff` / `principal` / `lead` /
  `manager` / `director` / `executive` / `unknown`, from a precedence ladder over the
  **title only** (`jobscrape/seniority.py`). First match wins, which is the whole design:
  5.8% of real titles name two levels, so "Senior Staff Engineer" is staff and "Principal
  Engineering Manager" is a manager. Classifies **56.0%** of open postings; the rest name
  no level and are honestly `unknown`.
- **`skills`** → a comma-separated list drawn from a closed 133-term vocabulary
  (`jobscrape/skills.py`), every pattern word-boundary anchored. That anchoring is not a
  refinement: without it `rust` matched 1,677 of 4,000 descriptions instead of 305
  (*trustworthiness*, *thrust*) and `scala` 1,543 instead of 137 (*scalable*,
  *escalation*) — both outranking `java`, which is how the problem was noticed.

### 3d. Filtering

- **Excluded by preference** — six categories held in a registry in `filters.py`:
  `qa`, `management`, `network`, `systems`, `security`, `analyst`, `firmware`. Distinct from the
  blocklist below, which means "not a software job at all": these *are* software jobs, and
  each is individually reversible with `--include-roles CATEGORY`. `is_excluded_role`
  returns the matching **category name**, so `purge` can report why a row went without
  re-testing it. The check runs in **both**
  `is_software_role` and `cheap_prefilter` — they are independent copies of the role
  decision, and the invariant test between them is one-directional, so an exclusion applied
  to only one would silently download every QA posting before discarding it.
- **Role** — an **allowlist** of ~100 title patterns: a title must *name* a software
  discipline. Bare `Engineer` / `Developer` is not enough, because the word is shared with
  every other engineering profession and no blocklist can enumerate them all — the rule
  that accepted it admitted 5,035 of 22,839 postings, four fifths hardware or generic. A
  blocklist still runs first, for titles that name something real but wrong ("Sales
  Engineer", "Solutions Engineer", "Mechanical Engineer"). A department confirms only
  genuinely ambiguous words (`Scientist`, `Architect`, `Intern`) — never a bare
  `Engineer`, since `Engineering` is the department almost every posting carries.
- **US eligibility** — a *can't-rule-it-out* test, not a guarantee. Bare `Remote`,
  missing locations and anything unrecognised are **accepted**; only a named non-US place
  or a region-locked remote scope (`Remote - EMEA`, `EU residents only`) is rejected.
- **Date window** — `--since` / `--until` against the normalised `posted_at`.

Counts for each rejection reason are recorded per source. That is what lets the dashboard
answer "why did this source yield nothing?" with a chart instead of a debugging session.

## Stage 4 — Export

`data/jobs.db` is the archive. Exports land in `data/exports/` as CSV, JSONL, XLSX
(written with openpyxl directly — pandas is deliberately not a dependency) or Markdown.

## Stage 5 — Triage (the dashboard)

```text
browser ──HTTP──> FastAPI ──subprocess──> python -m jobscrape <stage>
   ▲                 │                            │
   │                 │  stdout ──parsed──> run_events
   └────SSE──────────┘                            │
                                                  ▼
                                        the same CLI, unchanged
```

Stages run as **subprocesses of the CLI**, not in-process. That buys three things worth
more than the small overhead:

- No duplicated logic — the dashboard runs exactly what the terminal runs.
- A long sweep can genuinely be killed, including its concurrent workers (the whole
  process group is signalled).
- A crash in a stage cannot take the web server down with it.

The web layer can only launch names from a fixed allowlist, so it can never assemble an
arbitrary command line.

Five pages: **Overview** (headline numbers, the highest-leverage next action, and how many
duplicates have been collapsed), **Jobs** (the daily queue), **Runs** (start/stop, a live
streaming log, and a trend chart of jobs collected per run), **Coverage** (the
discovery→validation funnel), **Sources** (per-source yield and the filter funnel).

Charts are server-rendered inline SVG — no CDN, no chart library, works offline.

A `/health` endpoint reports real dependency state — Postgres connectivity and connection
pool usage, the age of the last successful run, disk headroom — not just "the process is
scheduled," returning a non-200 status when any check fails.

---

## Data model

**PostgreSQL**, with schema evolution managed by **Alembic** (`alembic/versions/`) rather
than hand-rolled `ALTER TABLE` migrations. `jobscrape/db.py` also applies the current
schema idempotently on every `connect()` — a zero-config safety net for local dev and the
test suite, harmless to run against a database Alembic has already migrated.

| Table | Holds | Why it is separate |
| --- | --- | --- |
| `companies` | discovered boards, their status, and `board_health` (`consecutive_failures`, `cooldown_until`) | The discovery/validation ledger, plus enough state to back off a struggling board instead of hitting it at full frequency forever |
| `jobs` | the postings | `closed_at` (NULL = open) marks closures, subject to a 48h grace period; `duplicate_of` suppresses reposts/cross-source copies (see Deduplication); `first_seen_at` powers "new since" |
| `job_status` | your triage and notes | **Separate on purpose** — a re-fetch rewrites `jobs`, and must never be able to erase where you applied. This survives a row becoming a duplicate too |
| `posting_seen` | postings evaluated and rejected | Stops the pipeline re-downloading the same rejects on every sweep |
| `runs` | run history and per-source stats (`stats` is `JSONB`) | Feeds the dashboard's process monitor and the per-run trend chart |
| `run_events` | streamed log lines | The live tail, and per-stage analytics |

Three columns are **derived at upsert** rather than set by an adapter — `evergreen_text`,
`scam_signals` and `skills`. All three are computed from `description_text`, which is
`COALESCE`-preserved, so each carries an explicit
`CASE WHEN excluded.description_text IS NOT NULL` guard. Without it the second sweep of
every board would recompute them from the NULL description a shallow refresh carries and
store the empty result over good data. `seniority` deliberately does *not* need this: it
derives from `title`, which is required and survives a shallow refresh intact.

`upsert_jobs` keys on `(source, source_job_id)`, preserves `first_seen_at`, and
`COALESCE`s the expensive fields (descriptions, dates, salary) so a later shallow pass
can never blank what an earlier deep pass captured. That guarantee is what makes the
incremental fast path safe. A re-upsert also clears `closed_at` unconditionally — a
posting the source is actively serving again is not closed, regardless of what an earlier
sweep concluded.

### The nine-field contract

Every job is expected to carry **title, source, location, mode, seniority, salary, skills,
description and link**. Measured across open, non-duplicate postings:

| Field | Coverage | Note |
| --- | --- | --- |
| title / source / link | 100% | required |
| location | 99.8% | |
| description | 99.8% | |
| skills | 78.5% overall, **87.1%** of full (>1500 char) postings | aggregator snippets are the gap |
| salary | 63.4% | most employers do not publish one |
| seniority | 56.0% | most titles do not name a level |
| mode | 61.4% | tier 4 lifted this from 51.1% |

**Nothing is inferred to close those gaps and nothing is dropped for having one.** A blank
means the employer did not state it, which is information in itself; guessing would make
the column mean two different things at once. What the pipeline does instead is make the
gaps visible — a field-coverage panel on the Overview page, and an `export --complete-only`
flag for when you want only the fully-specified rows (about 7% of the archive).

The skills gap is almost entirely structural rather than a failure of extraction: postings
under 600 characters — Adzuna and Jooble serve snippets, not full descriptions — yield a
skill 38% of the time, against 87.1% for postings over 1,500 characters.

## Is this posting real? (staleness)

A role that has been open eight months, that the employer lists fifteen concurrent copies
of, or that literally says "talent pool" is not something you can apply to today. Four
signals, and **two must converge** before a posting is called `likely_evergreen`:

| Signal | Fires when |
| --- | --- |
| `old` | `posted_at` is 90+ days ago |
| `mass_posted` | 5+ concurrent open copies of the same `(company_slug, role_fingerprint)` |
| `reposted` | `repost_count − mass_count ≥ 3` — historical churn only |
| `evergreen_text` | the description carries a standing-advert phrase |

That subtraction is not optional. Mass-posting is a *subset* of reposting, so counting both
raw would let one concurrent spray fire two signals and reach the verdict alone.

The **verdict is never stored** — it depends on the clock, so a stored copy is silently
wrong between backfills and a threshold change would need a migration. It is computed on
read, and filtering needs it in SQL too, so `staleness.py` generates both forms from the
same constants with a test that asserts they agree row-for-row.

The one thing that *is* stored is `evergreen_text`, because it is **evidence, not a
verdict**: whether a description contains "talent pool" depends only on the description.
Matching the phrases at read time with `ILIKE ANY` measured **52 seconds per query** —
leading wildcards cannot use an index — against **0.4s** reading the column.

The dictionary is deliberately multi-word ("talent pool", never a bare "pipeline", which
appears in every "data pipeline"). Current archive: 202 `likely_evergreen`, 11,485 `fresh`.

## Liveness: closing what the sweep cannot reach

Sampling sources (Hacker News threads, a quota-limited JSearch) never enumerate a
catalogue, so absence from a run means nothing and they can never close anything — their
postings would stay open forever. `python -m jobscrape liveness` probes the posting's own
page instead.

**Three verdicts, not two**, and that is the whole design:

| Verdict | Evidence | Effect on strikes |
| --- | --- | --- |
| `expired` | 404/410, a closed-posting phrase, a listing-page redirect, a body under 300 chars | **+1** |
| `live` | healthy 2xx, no death signal | **reset to 0** |
| `uncertain` | 5xx, 403, timeout, blocked address | **untouched** |

A 503 is not evidence a posting is alive, so it must not clear an accumulated strike — and
not evidence it is dead, so it must not add one. Collapsing that into a boolean is how a
site having a bad afternoon closes a catalogue. A posting closes at **two consecutive**
expired reads, and a reopen resets the counter so the grace survives it.

Two guards worth knowing. **URLs that outlive the vacancy are never probed**: a Hacker News
thread stays up whether or not the role was filled, so a probe could only ever answer
`live` — 221 of 496 candidates are skipped for this reason. And because the probed URL
comes from third-party data rather than an endpoint we chose, every probe goes through an
**SSRF guard** (`safehttp.py`) that resolves the host and rejects loopback, private,
link-local (`169.254.169.254`), and **CGNAT `100.64.0.0/10`** — which Python's
`ipaddress.is_private` does *not* cover and which AWS/GCP use for internal addressing.

Verified on the live archive: two consecutive runs, 9 dead postings closed on the second.

## Deduplication

One company posting the same role twice, or a job reaching the archive through both its
own ATS board and an aggregator, used to mean two (or hundreds of) rows in your review
queue for one real opening. Measured on the production archive: **6,367 of 44,328 open
jobs (14.4%)** were exactly this — the single largest cluster was one remote iOS role at
Quora, posted once per US city, 216 times.

Two identity hashes (`jobscrape/fingerprint.py`), each doing a different job:

- **`role_fingerprint`** — company + title + description. Strict: only collapses a true
  repost within the *same* source, since it requires the description to match exactly.
- **`role_key`** — company + title only, no description. Looser, and used *only* across
  sources, and only to suppress an **aggregator** copy in favor of a **direct ATS** copy —
  never to merge two ATS requisitions into each other. This asymmetry exists because
  aggregators hard-truncate descriptions (Adzuna caps at 500 characters against a
  Greenhouse posting's 4,000+), so `role_fingerprint` can never match across sources at
  all, while a title-only key applied *within* one ATS board was measured to wrongly merge
  hundreds of genuinely distinct requisitions.

`jobscrape/dedupe.py` runs both tiers and writes a `duplicate_of` pointer — never a
delete. A suppressed row keeps its data and its `job_status`, so a mis-cluster is
reversible (`dedupe.py`'s canonical-selection chains are also compressed to their true
root: a tier-1 canonical later reassigned by tier 2 would otherwise leave its tier-1
dependents pointing at a row that is itself a duplicate). Every read path — the dashboard,
exports, `stats` — filters `duplicate_of IS NULL`.

Runs scoped to one company after each board fetch (`resolve_scoped`, an index range scan);
`python -m jobscrape dedupe --backfill` recomputes across the whole archive, with
`--dry-run` and `--sample N` for reviewing what would collapse before committing.

## Migrating from the SQLite era

The archive was SQLite through most of this project's development. `python -m jobscrape
migrate-from-sqlite` copies every table into Postgres in dependency order, batched and
idempotent — safe to re-run after an interruption — and reports row counts per table so
the move is verifiable. It also computes `role_fingerprint`/`role_key` for every migrated
row by round-tripping each one through the real `Job` dataclass, so a migrated archive's
fingerprints are identical to what a live fetch would have produced.

## Cross-cutting: the HTTP layer

Every network call goes through a single `Fetcher`, so politeness lives in exactly one
place: per-host concurrency caps, a minimum interval between requests, exponential
backoff with jitter, and an on-disk cache keyed on method + URL + body + **response
type**.

Because every company on a given ATS shares one hostname, a wide sweep concentrates all
of its traffic on a single endpoint. So a 429 slows **the whole host** for the remainder
of the run — backing off only the individual request just lets the next board immediately
re-trip the limit. This is a correctness concern, not courtesy: a board lost to rate
limiting is a board whose jobs never reach your dataset. Adding it recovered Ashby from
35 boards to 66 on a fresh sweep, with zero 429s.

---

## Typical daily loop

```bash
./scripts/sweep.sh              # once: discover → validate → fetch everything
python -m jobscrape dashboard   # then work at http://127.0.0.1:8000
```

- **Overview** tells you the single highest-leverage next action.
- **Refresh Now** re-fetches. Repeat sweeps are much cheaper than the first, because
  postings already stored — or already evaluated and rejected — skip their detail request.
- **Jobs** shows only what you have not triaged, so you review each posting once.

## Running the numbers yourself

```bash
python -m pytest tests/ -q             # 1,218 tests against a real Postgres database
python -m jobscrape stats              # what the archive currently holds
python -m jobscrape sources            # every source and whether it is ready
python -m jobscrape dedupe --dry-run   # what a full dedup backfill would collapse
python -m jobscrape liveness --dry-run # probe postings the sweep can't reach
curl localhost:8000/health             # dependency checks: Postgres, last run, disk
```

The test suite needs Postgres reachable at `$JOBSCRAPE_TEST_DATABASE_URL` (a **separate**
database from `$DATABASE_URL` — see `tests/conftest.py`). Each test runs inside one
transaction that is rolled back at teardown, so nothing a test writes ever persists or
leaks into the next test.
