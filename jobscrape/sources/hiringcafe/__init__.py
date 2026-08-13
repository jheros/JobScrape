"""hiring.cafe postings, pulled from the site's own internal Next.js data API.

Not a documented public API and not a partnership -- hiringcafe.com serves its
own frontend's search results from `/_next/data/{buildId}/index.json`, the
same class of undocumented-but-stable endpoint `sources/jsearch.py` and
`sources/restricted/` already live with. No key, no login, no ToS on file.

Modeled as a separate service, the same shape as `sources/jobright/`, rather
than a registered `BoardSource`/`GlobalSource` or an entry in
`restricted_sources()` -- own module tree, own CLI subcommand
(`hiringcafe-fetch`/`hiringcafe-ingest`), own dashboard card. See
`scraper.py` for the buildId-discovery and pagination mechanics.
"""
