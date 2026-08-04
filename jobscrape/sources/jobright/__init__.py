"""JobRight postings, pushed in by an external scraper (JobScoutPro).

Like Apify, this is not a `Source` the fetch loop asks "what do you have?" —
it's handed a batch of already-scraped records (over stdin, by the
``jobright-ingest`` CLI stage) and asked to store them. See ``ingest.py``.
"""
