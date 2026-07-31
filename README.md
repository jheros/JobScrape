# US Software Job Collector

Collects **software job postings in the United States** for a date range you specify,
capturing as much per-job detail as each source exposes: direct link, apply link,
full description, company, location, salary, department, employment type and dates.

Coverage comes from going straight to the applicant tracking systems (ATS) that
companies post through, rather than to a single aggregator. Company boards are
discovered automatically — from the Common Crawl URL index, TLS certificate transparency
logs, and curated job lists — so the list of employers grows itself instead of being
hand-maintained.

**→ [How it works end to end](docs/ARCHITECTURE.md)** — the full process flow, the data
model, and why each piece is shaped the way it is.

---

## Read this first: what "during a period" can and cannot mean

**Every job API returns only currently-open postings.** A job posted inside your
window that has since been filled or closed is gone, and no source can retrieve it
retroactively. Concretely:

| Requested window | What you get |
| --- | --- |
| Last 1–30 days | Excellent. Most postings are still open and carry an exact publish date. |
| 1–3 months back | Good. Long-lived postings remain; filled roles have dropped off. |
| Older than ~4 months | Sparse. Only postings that never closed. The tool warns you. |

To get complete coverage of a period, you must be collecting **during** it. Every run
writes into a **PostgreSQL** archive keyed on `(source, job id)`, so re-running daily
builds a permanent record going forward. Nothing is ever overwritten with worse data — a
later shallow pass will not erase a description an earlier pass captured, and a company
posting the same role twice (or a job reaching the archive through both its own ATS board
and an aggregator) is collapsed to one canonical row rather than shown to you twice — see
[deduplication](docs/ARCHITECTURE.md#deduplication).

---

## Install

```bash
pip install -e .            # core
pip install -e ".[excel]"   # + .xlsx export
pip install -e ".[dev]"     # + tests
```

Requires Python 3.10+ and a running **PostgreSQL** server (17+ recommended; any recent
version works). The only hard non-database dependency is `httpx`.

```bash
cp .env.example .env               # optional; set JOBSCRAPE_CONTACT at minimum
createdb jobscrape                 # the archive
createdb jobscrape_test            # a SEPARATE database for the test suite
python -m alembic upgrade head     # create the schema
```

Migrating an existing SQLite archive from before this project moved to Postgres:

```bash
python -m jobscrape migrate-from-sqlite --dry-run   # preview row counts first
python -m jobscrape migrate-from-sqlite
python -m jobscrape dedupe --backfill --sample 5    # collapse reposts/cross-source dupes
```

## Quick start — the dashboard

For day-to-day use this is the whole interface:

```bash
./scripts/sweep.sh                 # one-time: discover -> validate -> fetch everything
python -m jobscrape dashboard      # then open http://127.0.0.1:8000
```

The dashboard drives and monitors the pipeline, and is where you triage jobs:

| Page | What it does |
| --- | --- |
| **Overview** | Headline numbers, growth chart, and the single highest-leverage next action |
| **Jobs** | Your daily queue. Filter, search, and set a status — rows leave the view once triaged, so you review each job once |
| **Runs** | Start/stop any stage, with the live log streaming as it runs, plus history |
| **Coverage** | The discovery → validation funnel per platform, and the unchecked backlog |
| **Sources** | Per-source yield and **why postings were dropped** (role / region / date) |

Every job carries a status — `new`, `saved`, `applied`, `interviewing`, `rejected`,
`dismissed` — stored in its own table, so a re-fetch can never overwrite the record of
where you applied.

### Running it several times a day

A first sweep is slow because every posting's description has to be downloaded. Repeat
sweeps are much cheaper: a posting already stored, **or already evaluated and rejected**,
skips its detail request entirely. Measured on live Ashby boards, skipping stored
postings alone cut requests 48% and halved the wall time; remembering rejected postings
closes most of the remaining gap, since on those boards 158 of 312 postings were being
re-downloaded every run only to be filtered out again.

Closed postings are detected too: when a board is fetched **successfully** and a posting
is no longer listed, it is marked closed and drops out of your queue. A board fetch that
*fails* marks nothing closed — otherwise one network error would wipe a company's listing.

## CLI

```bash
# Everything in one command: discover boards, validate, fetch, export.
python -m jobscrape run --since 2026-06-01 --until 2026-07-28 --format csv --format md
```

Or run the stages separately — each is resumable and safe to interrupt:

```bash
python -m jobscrape discover                      # harvest company slugs
python -m jobscrape validate                      # probe them, discard junk
python -m jobscrape fetch --since 2026-06-01      # pull postings
python -m jobscrape export --format csv           # write the file
python -m jobscrape stats                         # see what you have
python -m jobscrape sources                       # which sources are ready
```

## Sources

All verified working against live endpoints. The **21 ATS board sources** need no API key
and carry the bulk of the volume, and three large employers who run their own careers APIs
(Amazon, Apple, Uber) are covered directly because no ATS adapter can see them.

| Source | Key | Detail available |
| --- | --- | --- |
| **Greenhouse** | none | Full description, exact publish date, departments, offices |
| **Lever** | none | Full description (intro + sections), country, workplace type |
| **Ashby** | none | Full description, publish date, department, workplace type |
| **SmartRecruiters** | none | Structured city/region/country + remote & hybrid flags |
| **Workable** | none | Description, requirements, benefits, structured location |
| **Workday** | none | Full description, exact `startDate`, large US enterprises |
| **Recruitee** | none | Description + requirements, dates, remote/hybrid/on-site flags |
| **Pinpoint** | none | Native `workplace_type`, salary range, date via page JSON-LD |
| **Teamtailor** | none | JSON Feed + embedded schema.org JobPosting |
| **Personio** | none | XML feed: every posting with full description in one request |
| **BambooHR** | none | Description + date per posting, `isRemote` flag |
| **Breezy HR** | none | Structured location, salary text, description via JSON-LD |
| **JazzHR** | none | HTML board, but postings carry complete schema.org data |
| **Oracle Cloud** | none | Oracle Recruiting Cloud (Fusion) — large US enterprise & public sector |
| **UKG Pro** | none | Formerly UltiPro; listing already carries the description |
| **iCIMS** | none | Sitemap-driven, JSON-LD detail; both slug and vanity-host boards |
| **ADP** | none | Workforce Now career centres — US mid-market and enterprise |
| **Rippling** | none | Public REST board; common among US startups |
| **Jobvite** | none | Server-rendered listing, JSON-LD detail |
| **Cornerstone** | none | JWT + *regional* API host read from the career page |
| **Paycom** | none | Session JWT from the portal page; schema.org salary in detail |
| **RemoteOK** | none | Remote roles. *ToS requires attribution — see below.* |
| **Arbeitnow** | none | Mostly EU; low US yield, but free |
| **Hacker News** | none | "Who is hiring?" threads — startups no aggregator lists |
| **Himalayas** | none | ~96k indexed remote jobs; states hiring-region restrictions |
| **Jobicy** | none | Remote roles; `jobGeo` gives the hiring region |
| **We Work Remotely** | none | RSS category feeds; region field gives eligibility |
| **Remotive** | none | Remote roles. *Asks not to be redistributed wholesale* |
| **JSearch** | free | **LinkedIn + Indeed + Glassdoor + ZipRecruiter, aggregated legally** — [register](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) |
| **USAJobs** | free | Federal jobs — [register](https://developer.usajobs.gov/apirequest/) |
| **Adzuna** | free | Long-tail SMB — [register](https://developer.adzuna.com/) |
| **Jooble** | free | Aggregator breadth — [register](https://jooble.org/api/about) |
| **Amazon** | none | amazon.jobs — ~1,200 US software postings, full qualifications |
| **Apple** | none | jobs.apple.com — ~3,500 US postings behind a CSRF bootstrap |
| **Uber** | none | ~220 US postings. **Titles and locations only — no descriptions** |

**JSearch is the compliant route to the big-three job boards.** Rather than scraping
LinkedIn/Indeed (which violates their terms and is usually blocked), JSearch licenses and
republishes those listings through its own API. Its free tier is small — about 200
requests/month — so `JSEARCH_MAX_REQUESTS` caps per-run spend and responses cache for a
week.

Sources without a configured key are skipped with a note. They never fail a run.

### Restricted sources — opt-in, and read this

These are **off by default** and only load with `--include-restricted`: **LinkedIn**,
**Indeed**, and **Avature** (enterprise career portals).

- **Scraping these violates their Terms of Service.**
- LinkedIn and Indeed run aggressive anti-bot defenses. Those adapters *will* break and
  will often return nothing — that is the expected outcome, not a bug you can fix. In
  testing LinkedIn returned a heavily throttled 10 results and Indeed returned HTTP 403.
- Avature does currently work (verified: 30 postings, all dated). It is here because
  enterprise portals' terms prohibit automated collection and its pagination is
  undocumented, not because it is broken. Note its schema.org blocks usually omit
  location, so those rows survive only thanks to the permissive location filter.
- All three are isolated in `jobscrape/sources/restricted/` and imported lazily, so
  their failure can never affect the reliable board sources.

For Indeed there is a supported alternative: their official
[Publisher API](https://www.indeed.com/publisher). Set `INDEED_PUBLISHER_ID` and the
adapter uses that instead of scraping.

### Platforms that could not be supported

Investigated and found unusable, so no adapter was shipped rather than one that silently
returns nothing:

- **JS-rendered with no reachable data endpoint** (a plain HTTP fetch returns an empty
  shell; these would need a headless browser per board, which does not scale to
  thousands of companies): Zoho Recruit, ClearCompany, Paylocity, Taleo,
  SAP SuccessFactors.
- **Blocked outright, re-measured 2026-07-30 with TLS fingerprinting
  (`curl_cffi`) as well as plain HTTP** — impersonation was tried because it is the
  usual answer here, and it did not work, so no dependency was added:
  - **ZipRecruiter** — plain HTTP is refused by Cloudflare's WAF; impersonation gets
    past that and is then refused by ZipRecruiter's own anti-automation layer
    (`forbidden cf-waf` becomes `forbidden aa`). Seven browser profiles, same result.
    Its supported path is a keyed partner API.
  - **Glassdoor** — 403 at the edge on every profile.
  - **Indeed's mobile-app API** — blocked on the **header name** `indeed-api-key`
    itself: an empty value, a bogus value and the real key all 403, while that same key
    under any other header name reaches a normal API error and no key at all returns a
    clean 401 asking for one. The API demands a key and the edge blocks the only header
    that carries it. Not a fingerprinting problem, so impersonation cannot help.
  - **Meta** and **Tesla** — need a stealth headless browser, and Tesla a residential
    proxy on top; `ats-scrapers` records httpx, httpcloak, curl_cffi *and* Browserbase
    all failing against Tesla's Akamai.
- **Not job boards**: Employ (parent of JazzHR/Lever/Jobvite — already covered *via*
  those three), Google Hire (discontinued 2020), SignalHire (contact lookup), CareerArc
  (social distribution), Freshteam (sunset), "Homegrown (proprietary systems)".
- **HR suites / recruiting CRMs** whose boards are per-customer or behind auth: Bullhorn,
  JobDiva, Ceipal, Crelate, Recruit CRM, RecruiterFlow, Manatal, iSmartRecruit,
  BrightMove, CATS, HiBob, Factorial, PeopleForce, Lark, Bright Pattern, Paycor.

*(iCIMS, ADP, UKG, Oracle Recruiting, Cornerstone and Paycom were on this list in earlier
revisions and are now supported. Each was dropped after a guess at the endpoint rather
than a reading of the page — Cornerstone's API host, for instance, is regional and
per-tenant, and Paycom's session token is in the portal page's own HTML, not in a
minified bundle as previously recorded.)*

### RemoteOK attribution

RemoteOK's API terms require crediting them with a backlink. Markdown exports include
the notice automatically. If you publish data that includes RemoteOK rows, keep it:

> Job data from RemoteOK (<https://remoteok.com>)

## How discovery works

"All software jobs" is really a company-discovery problem — the job APIs are easy once
you know which boards exist.

`discover` runs **three independent vectors**, selectable with `--vector`:

1. **`cc` — the [Common Crawl](https://commoncrawl.org) URL index.** Reads every ATS
   board host (`job-boards.greenhouse.io/*`, `*.lever.co/*`, `*.myworkdayjobs.com/*`, …)
   and recovers each company's slug from the path. Highest volume by far. Completed
   index pages are checkpointed to `data/cc_progress.json`, so a multi-hour sweep
   resumes rather than restarting.
2. **`ct` — certificate transparency.** Every platform that gives each customer its own
   subdomain must obtain a TLS certificate for it, and those are published to public
   logs. Querying certspotter / hackertarget / crt.sh therefore enumerates customers the
   crawler never happened to visit — it found **84 Teamtailor boards against the 6**
   Common Crawl had. Works for the 9 subdomain-per-customer platforms.
3. **`github` — curated job lists.** Community new-grad/internship repos link directly to
   thousands of postings. Small but high-quality, and it surfaces Workday tenant triples,
   which are otherwise hard to discover.

Vectors 2 and 3 do **not** depend on Common Crawl, which matters: the index periodically
rate-limits and refuses connections outright. When that happens `discover` warns, falls
back to the bundled seeds, and the other two vectors carry on.

Then:

- `validate` probes each slug once and records it `live` / `empty` / `dead`. The index is
  full of numeric ids and junk paths; this is where they are discarded. Observed live
  rates: Ashby 86%, SmartRecruiters 77%, Greenhouse 80%.
- `fetch` pulls only from boards marked `live`.

Both stages checkpoint to Postgres — stop and re-run and they pick up where they left off.

```bash
python -m jobscrape discover                     # all three vectors
python -m jobscrape discover --vector ct         # just certificate transparency
python -m jobscrape discover --collections 1 --max-pages 1   # fast sample
```

**Validation is the step people skip, and it is the one that matters.** Discovering
8,000 companies does nothing until they are probed: this project sat at 405 live boards
out of 8,075 discovered (5%) until the backlog was worked through, which alone took
Greenhouse from 87 live boards to 1,343.

## Useful options

```bash
# Restrict to particular sources
python -m jobscrape fetch --sources greenhouse --sources lever

# Cap work while testing
python -m jobscrape fetch --companies 50 --max-results 200

# Keep non-software roles, or non-US locations
python -m jobscrape fetch --all-roles --worldwide

# Six role categories are excluded by default; re-admit any of them
python -m jobscrape fetch --include-roles security --include-roles systems

# Query the archive on export
python -m jobscrape export --state CA --search kubernetes --format xlsx
python -m jobscrape export --since 2026-07-01 --company stripe --include-html
```

## Output

The `jobscrape` Postgres database is the archive (`$DATABASE_URL`). Exports land in
`data/exports/` as CSV, JSONL, XLSX or Markdown, and never include suppressed duplicates.
Columns:

```text
job_url, title, company, location_raw, city, state, country, workplace_type,
seniority, is_remote, posted_at, employment_type, department, salary_min,
salary_max, salary_currency, salary_raw, skills, source, company_slug,
apply_url, updated_at, description_text[, description_html]
```

### The nine fields every job carries

**title, source, location, mode, seniority, salary, skills, description, link.**
Measured across open, non-duplicate postings:

| Field | Coverage |
| --- | --- |
| title, source, link, location, description | 100% |
| skills | 79% overall — **87%** of full (>1500 char) postings |
| salary | 63% |
| seniority | 56% |
| mode | 61% |
| *all nine at once* | *15%* |

**Nothing is guessed to fill a gap, and nothing is dropped for having one.** A blank means
the employer did not state it, which is itself worth knowing — most postings simply never
publish a salary or a work mode, and inventing one would make the column mean two
different things. The Overview page shows this table live, and:

```bash
# Only the fully-specified rows (~15% of the archive)
python -m jobscrape export --complete-only --format csv
```

**`seniority`** — `intern` / `entry` / `mid` / `senior` / `staff` / `principal` / `lead` /
`manager` / `director` / `executive` / `unknown`, read from the title by a precedence
ladder. First match wins, which is what makes "Senior Staff Engineer" staff and "Principal
Engineering Manager" a manager.

**`workplace_type`** — `remote` / `hybrid` / `onsite` / `unknown`, from a four-tier
cascade: the ATS's own field, then a structured boolean, then wording in the first 1,500
characters, then a final permissive sweep that marks a posting remote if *any* remote word
appears anywhere in it. That sweep runs last and only fills blanks, so a posting that
explicitly says hybrid or onsite keeps that answer.

The sweep is intentionally loose, to surface as many remote roles as possible. It matches
literally, with no negation guard, so a posting saying *"100% onsite (no hybrid or remote
options)"* will read as remote — about 24% of what it adds. The word `virtual` is the
largest single source of error (mostly "virtual interviews"); removing it from
`REMOTE_MENTION` in `jobscrape/models.py` is a one-line change if the remote count ever
looks inflated.

**`skills`** — a comma-separated list from a closed 133-term vocabulary of languages,
frameworks, clouds, datastores and practices. Every pattern is word-boundary anchored,
which is load-bearing rather than tidy: unanchored, `rust` matched 1,677 of 4,000 real
descriptions instead of 305 (*trustworthiness*, *thrust*) and `scala` 1,543 instead of 137
(*scalable*, *escalation*) — both outranking `java`. The skills gap is mostly structural:
Adzuna and Jooble serve ~500-character snippets, and a snippet rarely names a stack.

`workplace_type` is `remote` / `hybrid` / `onsite` / `unknown`. **Nothing is filtered on
it** — every job is collected regardless — so you can slice the dataset yourself:

```bash
# just the remote ones
psql jobscrape -c "SELECT title, company, job_url FROM jobs WHERE workplace_type='remote' AND duplicate_of IS NULL"
```

A source's own field is trusted first (Lever, Ashby, SmartRecruiters, Workable, Workday,
Recruitee and Pinpoint all publish one), then a structured boolean, then wording in the
title/location. `unknown` is the honest answer for a bare city with no workplace wording
and is the largest bucket — it is not a failure, and guessing "onsite" there would be
inventing data.

`description_html` is omitted unless you pass `--include-html`, since it dominates
file size. Salary is parsed out of the description text when a source has no
structured field, and validated against realistic pay bands so that funding
announcements and discounts are not mistaken for compensation.

## Filtering

Two classifiers decide what is kept. Both are deliberately conservative — an ambiguous
row is dropped rather than allowed to pollute the dataset.

- **Software role**: an **allowlist** of ~100 title patterns covering backend / frontend /
  full stack / platform / SRE / DevOps / cloud / ML / AI / LLM / data / mobile, seniority
  variants (Staff, Principal, Member of Technical Staff, New Grad), legacy stacks
  (Mainframe, COBOL, ETL) and language-named roles. A title has to **name** one of these.

  Bare `Engineer` or `Developer` is deliberately **not** enough. It used to be, with the
  blocklist expected to hold back every other profession that shares the word — but a
  blocklist has to anticipate every discipline in advance, and each sweep surfaced ones
  nobody had listed. Measured against the archive, that one rule admitted 5,035 of 22,839
  open postings, four fifths of them hardware ("Design Verification Engineer",
  "FPGA Engineer") or generic ("General Engineer", "Chief Engineer"). The failure mode is
  now a missed real title rather than an unbounded stream of unrelated ones, and the fix
  for that is one line in `_SOFTWARE_TERMS`.

  The blocklist is still checked first and still matters, for titles that name something
  real but wrong: "Sales Engineer", "Solutions Engineer", "Enterprise Architect",
  "Mechanical Engineer", "Business Developer", "Technical Recruiter", "CNC Programmer".

  Both lists allow up to two words between a qualifier and the role noun, so "iOS
  Frameworks Engineer" is kept and "Mechanical Reliability Engineer" is rejected. The
  accept side runs that gap over qualifiers no middle word can make non-software
  (`software`, `iOS`, `machine learning`, `cloud`), and the block side over physical
  disciplines no middle word can make software (`mechanical`, `silicon`, `VLSI`, `optical`).
  Words where the middle of the title decides — `design`, `systems`, `data`, `validation` —
  are on neither list and keep requiring adjacency. One exemption outranks the block side:
  a title saying **"Software Engineer" outright** is kept regardless, which is what
  separates "Optical Software Engineer" from "Optical Design Engineer".

  A department alone never rescues a title. `Engineering` is the department nearly every
  engineering board files postings under, so it is no evidence that an engineer is a
  *software* engineer. It confirms only genuinely ambiguous words — `Scientist`,
  `Architect`, `Tech Lead`, `Intern` — where the competing reading is a research biologist
  or a building architect rather than a hardware engineer.
- **US eligibility**: a *can't-rule-it-out* test rather than a guarantee. It accepts
  unless the listing gives positive evidence it cannot hire in the US. So a bare
  `Remote`, a `Worldwide` scope, a **missing** location, and anything unrecognized are
  all **kept** — an absent field is not evidence. A US signal anywhere in the string
  wins, so "New York / London" survives.

  Only two things are rejected: a named non-US place (`London`, `Bangalore, India`) with
  no countervailing signal, and a remote role scoped to another region
  (`Remote - EMEA`, `EU residents only`) which by definition cannot hire a US candidate.

  The trade-off is deliberate and worth knowing: a genuinely non-US job whose listing
  omits its location will come through. That is the cost of not discarding real jobs
  over a blank field.

- **QA and management are excluded**, by preference rather than because they are not
  software roles. Two flags reverse them independently:

  | Excluded | Covers | Re-admit with |
  | --- | --- | --- |
  | `qa` | QA / quality assurance, SDET, software engineer in test, tester, **test engineer**, **test automation**, **automation engineer** | `--include-roles qa` |
  | `management` | whatever the seniority ladder calls `manager`, `director` or `executive` | `--include-roles management` |
  | `network` | network / networking engineer, network architect, administrator, specialist | `--include-roles network` |
  | `systems` | systems engineer / administrator / architect / analyst, sysadmin | `--include-roles systems` |
  | `security` | security engineer / analyst / architect, **application & product security**, cyber security, InfoSec, SecOps, penetration testing | `--include-roles security` |
  | `analyst` | data analyst, **analytics engineer** | `--include-roles analyst` |
  | `firmware` | firmware and embedded — Firmware Engineer, Embedded Software Engineer, Embedded Developer | `--include-roles firmware` |
  | `early_career` | intern, internship, co-op, apprentice, trainee, new grad, university/college grad | `--include-roles early_career` |
  | `flight` | flight software, flight control/test/systems, avionics — embedded software on a vehicle | `--include-roles flight` |

  `--include-roles` is repeatable. `--include-qa` and `--include-management` remain as
  shorthand for the first two.

  Every exclusion pattern is spelled to match an accept pattern that names the same
  titles. Without that the flag half-works — it catches a spelling the accept list does
  not name, so nothing restores it and no error says why. `early_career` needed
  "data science" adding for exactly this reason, and it is word-anchored throughout
  because a bare `intern` substring also matches "internal" and "international".

  Note `flight` is a *preference*: "Flight Software Engineer" is genuine C++ work,
  excluded as embedded on the same reasoning as `firmware`. "Flight Engineer" is a
  different job entirely — cockpit crew — and is in the blocklist, not this table.

  The category word does not have to sit next to the role noun — up to two words may
  come between, so "Systems **Integration** Engineer" and "Lead Systems **Operations**
  Engineer" are caught, not just the adjacent form. That bound matters: unbounded, a
  category word at one end of a title would bind to a role noun at the other, turning a
  targeted rule into a keyword ban.

  **A software head noun survives a trailing category.** "Senior Software Engineer —
  Distributed Systems" and "Staff Software Engineer, Security" are software roles that name
  a team, and they stay. **Distributed systems and systems programming** are also kept
  outright as software disciplines; embedded systems is not, since it is firmware.

  Management is read from `jobscrape/seniority.py` rather than a second set of regexes,
  which is what gets the hard cases right: "Tech Lead", "Staff Engineer" and "Principal
  Engineer" are individual contributors and stay, while "Principal Engineering Manager"
  is a manager despite the IC word. "Chief Engineer" is also correctly read as a senior
  IC rather than C-suite — it leaves the archive anyway, for the separate reason that it
  names no software discipline, which is the right outcome for the ~69 aerospace and
  defence postings that carried it.

  These exclusion categories are **not** made redundant by the allowlist, which is worth
  saying because it looks like they should be. `_SOFTWARE_TERMS` names "systems engineer",
  "network engineer", "security engineer", "qa engineer" and "engineering manager"
  explicitly, so each would pass the allowlist on its own — the accept patterns are kept
  precisely so `--include-roles` can restore them, and this layer is what removes them by
  default. Each exclusion pattern is spelled to match its accept pattern; otherwise the
  flag silently half-works, which is how `--include-roles network` came to restore
  "Network Engineer" but not "Networking Engineer".

  The QA list deliberately takes the broad reading, which also removes hardware test roles
  ("Flight Test Engineer", "Propulsion Test Engineer") and a few software titles that merely
  contain the word, such as "Data and Automation Engineer - Python".

Override with `--all-roles` and `--worldwide`.

### Removing roles already collected

The archive is written once per posting and never re-evaluated, so a filter change only
affects future fetches. To apply it retroactively:

```bash
python -m jobscrape purge --dry-run --sample 10   # look first
python -m jobscrape purge
python -m jobscrape dedupe --backfill             # re-cluster anything released
```

This re-applies the *current role filter* to every stored row and deletes what no longer
passes — role only, since region and date are per-run choices rather than archive-wide
rules. Purged postings are recorded in `posting_seen` so the next sweep skips them instead
of downloading them again, and your triage notes in `job_status` are untouched.

It is the only destructive command in the project. Reversing it means re-fetching, and
`--include-qa` alone will *not* bring back postings already turned away — those are recorded
as rejected and skipped, so pair it with `--refetch-details`.

### Why runs are faster than they look

Ashby, SmartRecruiters and Workday need a second request per posting to get the
description and exact date. Rather than fetch all of them and filter afterwards, a
cheap prefilter runs against the listing data first and skips detail requests for
postings that cannot pass. On a representative board this cut 116 requests to 34 —
a 71% reduction — with byte-identical output.

The prefilter is deliberately more permissive than the real filters: anything
uncertain passes through, because a wasted request is far cheaper than a silently
lost job. `tests/test_prefilter.py` asserts that invariant across every
title × location × department combination.

## Scheduling

Because the archive accumulates, a daily run is what eventually gives complete period
coverage — it captures postings before they close. `scripts/daily.sh` does this: it
fetches a 3-day lookback (so a missed run self-heals) and re-runs discovery weekly
rather than daily, since the set of boards changes slowly.

```cron
0 6 * * * /path/to/project/scripts/daily.sh >> /path/to/project/data/cron.log 2>&1
```

## Politeness and rate limiting

All traffic goes through one client that enforces per-host concurrency limits, a delay
between requests, exponential backoff with jitter on 429/5xx, and an on-disk response
cache so re-runs are nearly free. Set `JOBSCRAPE_CONTACT` so your requests identify
you.

Every company on a given ATS shares one hostname, so a wide sweep concentrates all of
its traffic on a single endpoint. Two things follow from that:

- **Sensitive hosts get stricter limits.** `config.HOST_LIMITS` sets lower concurrency
  and longer delays for `jobs.ashbyhq.com` and friends. An explicit `.env` setting is
  never loosened by these overrides, only tightened.
- **A 429 slows the whole host, not just the one request.** Backing off a single
  request is useless when the next board immediately re-trips the limit, so the host's
  delay is increased for the remainder of the run (up to 8s).

This matters for correctness, not just courtesy: a board lost to rate limiting is a
board whose jobs never reach your dataset. Being slower is strictly better than
silently under-collecting. Shared ATS hosts also get a larger retry budget than the
default for the same reason.

The disk cache means an interrupted sweep costs little to resume — re-running mostly
replays cached responses. Use `--no-cache` only when you specifically want fresh data.

## Tests

```bash
python -m pytest tests/ -q
```

Covers the classifiers, date normalization (including Lever's epoch-milliseconds),
HTML flattening, salary parsing, deduplication (fingerprinting and cluster resolution),
staleness scoring, liveness probing, SSRF blocking, and database preservation behavior — against a real Postgres database
(`$JOBSCRAPE_TEST_DATABASE_URL`), not SQLite, so what passes is what production runs. No
network access required.

## Layout

```text
jobscrape/
  cli.py            discover / validate / fetch / export / stats / sources / run /
                     migrate-from-sqlite / dedupe
  http.py           shared async client: per-host rate limits, retries, cache
  db.py             PostgreSQL schema (via SQLAlchemy Core), upserts, company registry,
                     board_health backoff, grace-window closures
  fingerprint.py    role_fingerprint / role_key / content_hash identity hashes
  dedupe.py         two-tier duplicate cluster resolution + backfill
  staleness.py      is this posting a real, current opening?
  liveness.py       probe a posting's own page; close it on positive evidence
  safehttp.py       SSRF guard for probing third-party URLs
  migrate.py        one-time SQLite -> Postgres data migration
  observability.py  structured JSON logging, /health dependency checks
  models.py         Job record, date/salary/HTML parsing, workplace classifier, company
                     slug derivation
  filters.py        software-role and US-eligibility classifiers + prefilter
  export.py         CSV / JSONL / XLSX / Markdown
  discovery/
    commoncrawl.py  harvest ATS slugs from the crawl index
    seeds.py        verified boards, used when the index is unavailable
    validate.py     probe slugs, mark live / empty / dead
  sources/
    jsonld.py       shared schema.org JobPosting reader for HTML boards
    <platform>.py   one adapter per platform (19 board + 11 search)
    restricted/     opt-in only: linkedin, indeed, avature
alembic/
  versions/         schema migrations (the initial revision mirrors db.py's DDL)
```

## Credits

Everything below was reimplemented in Python against this project's own `Job` model and
`Fetcher` — nothing is vendored — and every borrowed claim was re-measured against live
endpoints before being adopted. Several were not adopted; those are recorded too, because
a measured negative is worth as much here as a feature.

**[freehire](https://github.com/strelov1/freehire)** (MIT) — the repost/cross-source
deduplication design: a company/title/description identity hash, suppression via a pointer
rather than deletion, and a grace period before closing a posting. Extended here with a
two-tier fingerprint/key split to handle aggregator description truncation, which the
original single-hash design didn't need to. Its staleness scoring shaped
`jobscrape/staleness.py`.

**[ats-scrapers](https://github.com/kalil0321/ats-scrapers)** (MIT) — the largest single
contribution:
- The **Workday 2,000 reporting cap** and the facet-subdivision fix for it. This was a
  live bug here: every employer at or above 2,000 postings was silently truncated.
  NVIDIA now yields 2,584 instead of 2,000.
- The **Cornerstone and Paycom** bootstrap flows, which corrected an earlier wrong call
  that both were unreachable.
- Its **MIT-licensed tenant lists** — 49,567 slugs across the 21 platforms we can fetch,
  imported via `jobscrape import-tenants` (see [Discovery](#how-discovery-works)).
- Endpoint shapes for Amazon, Apple and Uber.

**[JobSpy](https://github.com/speedyapply/JobSpy)** (MIT) — the Indeed mobile-app GraphQL
endpoint, its app key and attribute filter keys, plus the ZipRecruiter and Glassdoor
request shapes. All three of those routes are currently blocked (see
[Platforms that could not be supported](#platforms-that-could-not-be-supported)); the
Indeed implementation ships behind `INDEED_ALLOW_APP_API=1` so it is ready if the route
reopens.

**[JobSentinel](https://github.com/cboyd0319/JobSentinel)** (MIT) — the scam-signal
regexes behind `jobscrape/scam.py`. **Three of its nine were adopted.** All nine were run
against 44,493 real descriptions from this archive first; six produced 670 matches and not
one was a scam — they fire on fintech and messaging companies describing their own product
(Coinbase, Block, Fiserv), on recruiter boilerplate ("will NOT pay a placement fee"), and
hardest of all on the employers who publish anti-fraud warnings. The three kept matched
zero of those 44,493, and the reasoning is written up in `jobscrape/scam.py`.

**[job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator)** — read for
code patterns only. Its code is MIT but its `data/` directory is **CC BY-NC 4.0**, so its
tenant lists, salary lookup and locations file are deliberately **not** used. The
MIT-licensed ats-scrapers lists are used instead.
