# How job scraping actually works

Fetching jobs is split into 3 parts: **Custom**, **Apify**, **Jobright**. Each one
grabs jobs from a different kind of source, in a different way.

## Part 1: Custom

This is our own code, hitting sources directly — no outside service involved.

### ATS platforms (the main one)

Most companies post their jobs through an ATS (applicant tracking system) —
Greenhouse, Lever, Ashby, Workday, BambooHR, Personio, Recruitee, Pinpoint,
Teamtailor, SmartRecruiters, Workable, Breezy, Rippling, JazzHR, Jobvite, iCIMS,
Oracle, UKG, ADP, Cornerstone, Paycom. ~20 platforms, one adapter per platform.

For all of these, we just call the platform's own API directly. No browser, no
Selenium — plain HTTP request in, JSON back. Example, Greenhouse:

```
GET https://boards-api.greenhouse.io/v1/boards/{company-slug}/jobs?content=true
```

One request, and you get every open job at that company, description included.
That's the easy case. Some other platforms need two calls instead of one (a list
call, then a detail call per job), but it's the same idea everywhere — call the API,
get JSON, turn it into a job record.

But to call `boards-api.greenhouse.io/v1/boards/acme/jobs`, you first need to know
`acme` is a real Greenhouse customer. That's the part before fetching:

**Discover** — find companies that might be using one of these ATS platforms.
We don't have a master list, so we go looking for the slugs ourselves:

- **Common Crawl** — imagine someone else already crawled a huge chunk of the
  internet and saved every URL they ran into in one giant, searchable pile. That's
  Common Crawl — it's free and public, and we don't have to crawl anything
  ourselves, just search their pile. So we search it for anything that looks like
  `boards.greenhouse.io/something` or `something.lever.co`, one search per platform,
  and whatever `something` is, that's a company slug to go check out. (Workday's a
  bit different — its URLs pack three pieces of info together instead of one clean
  slug, so that one needs its own extra parsing.) This is by far our biggest source
  of candidates. Downside: that "giant pile" is itself split into a ton of chunks,
  so searching all of it takes hours, not seconds — we save our place as we go so a
  run can be stopped and picked back up later instead of starting over.
- **Certificate transparency logs** — every website with the padlock/HTTPS needs a
  security certificate to get that, and there's a rule (not up to the company) that
  every certificate ever issued, for any website, gets permanently logged somewhere
  public — think of it like a public receipt that can never be deleted. So if a
  company set up `acme.bamboohr.com`, getting that HTTPS certificate left a public
  receipt with `acme.bamboohr.com` written on it, forever, whether they wanted it
  logged or not. We just search those public receipts for each ATS's domain and
  read the company slug straight off them.
- **GitHub lists** — some people keep a running public list on GitHub of companies
  that are hiring — the kind of "companies hiring new grads" page that gets shared
  around and updated by hand. We just read a handful of those and pick out anything
  that looks like a job-board link. Way smaller than Common Crawl, but since a
  person curated it, almost everything on it is a real, currently-hiring company —
  so it's a small list that's unusually reliable.
- **Manually imported lists** — sometimes someone else has already gone and built a
  big list of company slugs for one platform and published it (we've used lists
  with tens of thousands of entries) — basically, someone already did this exact
  homework for us, so we just load their answer sheet instead of redoing the work.
  The annoying part is that everyone formats their list differently, not
  necessarily the way we need it — Workday's especially fussy about this, since we
  need three specific pieces of info per company and one of them is sometimes
  buried inside a URL in their list rather than sitting in its own column, so we
  have to dig it back out.

All of this just gives us *candidate* slugs — tens of thousands of them. Doesn't mean
they're real.

**Validate** — check whether a candidate slug is actually real. Just send one request
to that company's board API and see what comes back:

- Real jobs → **live**, worth fetching from.
- Real board, nothing posted right now → **empty**.
- Nothing there / 404 → **dead**, ignore it.

Only `live` companies ever get fetched for real.

### Other custom sources (not tied to one company)

A few sources aren't "per company" — you just query them directly: Adzuna, Jooble,
USAJobs, RemoteOK, and a few smaller ones. Same idea as the ATS platforms — call
their API with a search query instead of a company slug, get JSON back.

## Part 2: Apify

Indeed and LinkedIn don't work like the above — they actively block plain API
scraping. So instead of scraping them ourselves, we use **Apify**, a cloud scraping
service. Apify runs its own actor (bot) that does the actual scraping of Indeed/
LinkedIn on their infrastructure, and we just grab the results afterward — either it
pushes them to us automatically (a webhook, when its run finishes), or we manually
pull the last few runs (`apify-pull`).

## Part 3: Jobright

JobRight.ai has no public API, and you need to be logged in to see anything. So this
one's different again: a real Chrome browser, driven by Selenium
(`undetected-chromedriver`, so the site doesn't fingerprint it as a bot), logs into
your JobRight account exactly like a person would. Once it's logged in, instead of
clicking through the UI, it calls JobRight's own internal API directly using the
browser's session cookies — much faster than scraping the rendered page, and gets
more fields too.

## After any of the 3 bring jobs in

Doesn't matter which of the 3 parts a job came from — once fetched, every job goes
through the same pipeline:

1. **Clean it up** — normalize dates, figure out remote/hybrid/onsite, guess
   seniority from the title, parse salary out of the text if it wasn't structured,
   that kind of thing. Same rules for every source.
2. **Filter it** — is it a software role, is it US-eligible, is it in the date
   window, is it actually remote (if that's what you asked for)? Only what survives
   gets saved.
3. **Dedupe it** — the same job often shows up twice: once when a company reposts
   it, and once when it shows up on both LinkedIn/Indeed *and* the company's own
   board. Both get collapsed down to one canonical copy (never deleted, just marked
   as a duplicate).
4. **Close it when it disappears** — if a company's board stops listing a job we had
   before, we wait a bit (in case the board's just glitching) then mark it closed.

For way more detail on any of this — exact numbers, edge cases, every source's
quirks — see `docs/ARCHITECTURE.md`. This file is just the map.
