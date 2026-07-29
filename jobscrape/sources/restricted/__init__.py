"""Opt-in scrapers for sites whose Terms of Service prohibit scraping.

These are OFF by default and only load when ``--include-restricted`` is passed.

Read before enabling:

* Scraping LinkedIn and Indeed violates their Terms of Service.
* Both deploy aggressive anti-bot defenses. These adapters *will* break, and
  may return nothing at all on any given day. That is expected, not a bug.
* Requests may be rate-limited, challenged, or blocked by IP.

They are deliberately isolated in this subpackage and imported lazily so that a
failure here can never affect the reliable ATS sources.
"""
