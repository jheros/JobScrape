"""Tests for date normalization, HTML flattening, and database upserts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobscrape import db
from jobscrape.models import Job, html_to_text, to_utc_iso


def test_epoch_milliseconds_are_detected() -> None:
    """Lever sends epoch ms; seconds and ms must not be confused."""
    ms = to_utc_iso(1784569799619)
    assert ms is not None and ms.startswith("2026-")
    sec = to_utc_iso(1784569799)
    assert sec is not None and sec.startswith("2026-")


def test_date_shapes() -> None:
    assert to_utc_iso("2026-05-24").startswith("2026-05-24")
    assert to_utc_iso("2026-07-27T11:17:30-04:00").startswith("2026-07-27")
    assert to_utc_iso("2026-06-24T10:00:11.853Z").startswith("2026-06-24")
    assert to_utc_iso(None) is None
    assert to_utc_iso("not a date") is None


def test_timezone_is_normalized_to_utc() -> None:
    value = to_utc_iso("2026-07-27T23:00:00-04:00")
    assert value == datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc).isoformat()


def test_html_to_text_handles_double_encoding() -> None:
    html = "&lt;p&gt;Hello&lt;/p&gt;&lt;p&gt;World&lt;/p&gt;"
    text = html_to_text(html)
    assert "Hello" in text and "World" in text and "<" not in text


def test_html_to_text_strips_scripts() -> None:
    assert "alert" not in html_to_text("<p>Hi</p><script>alert(1)</script>")


def test_job_derives_text_from_html() -> None:
    job = Job(
        source="t", source_job_id="1", title="SWE", company="Acme",
        job_url="https://x/1", description_html="<p>Build <b>things</b></p>",
    )
    assert job.description_text == "Build things"


def _job(job_id: str, **kw: object) -> Job:
    base = dict(
        source="test", source_job_id=job_id, title="Software Engineer",
        company="Acme", job_url=f"https://example.com/{job_id}",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_upsert_deduplicates(tmp_path) -> None:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        inserted, updated = db.upsert_jobs(conn, [_job("1"), _job("2")])
        assert (inserted, updated) == (2, 0)

        inserted, updated = db.upsert_jobs(conn, [_job("1"), _job("3")])
        assert (inserted, updated) == (1, 1)
        assert db.stats(conn)["total_jobs"] == 3


def test_upsert_preserves_description_against_null(tmp_path) -> None:
    """A later shallow pass must not erase a description we already captured."""
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("1", description_html="<p>Full details</p>")])
        db.upsert_jobs(conn, [_job("1", description_html=None)])
        row = conn.execute("SELECT description_text FROM jobs").fetchone()
        assert row["description_text"] == "Full details"


def test_upsert_preserves_company_and_title_against_blank(tmp_path) -> None:
    """Rippling/JazzHR/Paycom only know the real company from a per-posting
    detail call; iCIMS/Jobvite have no listing payload at all, so title is
    detail-only too. An already-known posting's shallow refresh sends `""`
    for whichever of these it can't re-derive without a detail request --
    same shape as description above, but `company`/`title` are required
    fields (never a real `None`), so this exercises the NULLIF branch of
    the upsert instead of a plain COALESCE."""
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("1", company="Acme Corp", title="Backend Engineer")])
        db.upsert_jobs(conn, [_job("1", company="", title="")])
        row = conn.execute("SELECT company, title FROM jobs").fetchone()
        assert row["company"] == "Acme Corp"
        assert row["title"] == "Backend Engineer"

        # A real update (not a blank shallow refresh) must still win.
        db.upsert_jobs(conn, [_job("1", company="Acme Corp (renamed)", title="Backend Engineer II")])
        row = conn.execute("SELECT company, title FROM jobs").fetchone()
        assert row["company"] == "Acme Corp (renamed)"
        assert row["title"] == "Backend Engineer II"


def test_upsert_preserves_employment_type_country_location_department_is_remote(tmp_path) -> None:
    """Same failure, four more fields wide: all detail-only for at least one
    adapter (Rippling/Paycom for employment_type/country, iCIMS/Jobvite for
    location_raw/department/is_remote), all genuinely Optional so a plain
    COALESCE (not NULLIF) is what protects them."""
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job(
            "1", employment_type="full_time", country="US",
            location_raw="Austin, TX", department="Engineering", is_remote=True,
        )])
        db.upsert_jobs(conn, [_job(
            "1", employment_type=None, country=None,
            location_raw=None, department=None, is_remote=None,
        )])
        row = conn.execute(
            "SELECT employment_type, country, location_raw, department, is_remote "
            "FROM jobs"
        ).fetchone()
        assert row["employment_type"] == "full_time"
        assert row["country"] == "US"
        assert row["location_raw"] == "Austin, TX"
        assert row["department"] == "Engineering"
        assert row["is_remote"] is True


def test_first_seen_is_stable_across_reruns(tmp_path) -> None:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("1")])
        first = conn.execute("SELECT first_seen_at FROM jobs").fetchone()["first_seen_at"]
        db.upsert_jobs(conn, [_job("1", title="Senior Software Engineer")])
        row = conn.execute("SELECT first_seen_at, title FROM jobs").fetchone()
        assert row["first_seen_at"] == first
        assert row["title"] == "Senior Software Engineer"


def test_company_registry(tmp_path) -> None:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        assert db.add_companies(conn, "greenhouse", ["stripe", "figma"]) == 2
        assert db.add_companies(conn, "greenhouse", ["stripe"]) == 0  # idempotent
        db.mark_company(conn, "greenhouse", "stripe", "live", 536)
        live = db.get_companies(conn, ats="greenhouse", status="live")
        assert len(live) == 1 and live[0]["job_count"] == 536


def test_query_filters(tmp_path) -> None:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            _job("1", posted_at="2026-06-15", state="TX"),
            _job("2", posted_at="2026-01-01", state="CA"),
        ])
        assert len(db.query_jobs(conn, since="2026-06-01")) == 1
        assert len(db.query_jobs(conn, state="TX")) == 1
        assert len(db.query_jobs(conn)) == 2


def test_seven_digit_fractional_seconds() -> None:
    """.NET-style timestamps carry 7 fractional digits; fromisoformat takes 3 or 6.

    Jooble sends these. Before this was handled every Jooble job parsed as
    undated and was silently dropped by any --since/--until window — 400 real
    jobs reduced to zero.
    """
    value = to_utc_iso("2026-07-29T07:40:09.7530000")
    assert value is not None and value.startswith("2026-07-29T07:40:09")


def test_four_digit_fractional_seconds() -> None:
    """USAJobs sends 4 digits (".0000"), which fromisoformat also rejects.

    Padding matters as much as truncating: this cost every USAJobs row until
    the fraction was normalized to exactly 6 digits.
    """
    value = to_utc_iso("2026-07-23T00:00:00.0000")
    assert value is not None and value.startswith("2026-07-23")


@pytest.mark.parametrize(
    "raw",
    [
        "2026-07-29T07:40:09.5",             # 1 digit
        "2026-07-29T07:40:09.75",            # 2
        "2026-07-29T07:40:09.753",           # 3  (natively accepted)
        "2026-07-29T07:40:09.0000",          # 4  (USAJobs)
        "2026-07-29T07:40:09.75300",         # 5
        "2026-07-29T07:40:09.753000",        # 6  (natively accepted)
        "2026-07-29T07:40:09.7530000",       # 7  (Jooble / .NET)
        "2026-07-29T07:40:09.75300000",      # 8
        "2026-07-29T07:40:09.123456789",     # 9
        "2026-07-29T07:40:09",               # none
    ],
)
def test_any_fractional_precision_parses(raw: str) -> None:
    """Real sources emit 1-9 digits; fromisoformat natively accepts only 3 or 6."""
    assert to_utc_iso(raw) is not None


def test_fractional_truncation_keeps_the_offset() -> None:
    """Truncating the fraction must not damage the timezone that follows it."""
    value = to_utc_iso("2026-07-29T07:40:09.1234567+02:00")
    assert value == "2026-07-29T05:40:09.123456+00:00"


def test_clean_location_strips_coordinate_tails() -> None:
    """Some ATS exports append lat/long to the location string."""
    from jobscrape.models import clean_location

    assert clean_location("Gaston, SC 29053 | 33.8316 | -81.1126") == "Gaston, SC 29053"
    assert clean_location("Remote | 40.7 | -74.0") == "Remote"
    assert clean_location("Reno, NV | 39") == "Reno, NV"


def test_clean_location_requires_a_numeric_tail() -> None:
    """A pipe alone is not a coordinate — real place names use them too."""
    from jobscrape.models import clean_location

    assert clean_location("Berlin | Germany") == "Berlin | Germany"
    assert clean_location("District 9") == "District 9"
    assert clean_location("Austin, TX") == "Austin, TX"
    assert clean_location(None) is None


def test_clean_location_caps_a_semicolon_joined_state_enumeration() -> None:
    """A posting open to every US state as a separate remote location lists
    each one individually -- measured up to 1,893 characters / 52 entries
    on one real Workday posting. Cap it to the first few plus a count
    rather than storing the entire enumeration."""
    from jobscrape.models import clean_location

    states = [f"Remote - {n}, USA" for n in range(1, 51)]
    result = clean_location("; ".join(states))

    assert result == (
        "Remote - 1, USA; Remote - 2, USA; Remote - 3, USA; "
        "Remote - 4, USA; Remote - 5, USA; +45 more"
    )


def test_clean_location_caps_regardless_of_where_the_join_happened() -> None:
    """One real Greenhouse posting's own `location.name` field carried a
    "; "-joined 50-state enumeration directly -- no adapter-side join
    involved at all. The cap has to catch this on the final string, not
    rely on every adapter to join responsibly."""
    from jobscrape.models import clean_location

    raw = "; ".join(f"{s} - Remote" for s in
                     ["Arizona", "Arkansas", "Florida", "Georgia", "Idaho", "Indiana"])
    result = clean_location(raw)

    assert result == "Arizona - Remote; Arkansas - Remote; Florida - Remote; " \
        "Georgia - Remote; Idaho - Remote; +1 more"


def test_clean_location_prefers_semicolon_over_comma_when_both_cross_the_cap() -> None:
    """"; " separates distinct places, ", " separates one place's own parts
    -- capping on "; " when a value has both keeps each shown place intact
    ("Austin, TX, US") instead of slicing one apart mid-name."""
    from jobscrape.models import clean_location

    places = [f"City{n}, ST, US" for n in range(1, 7)]  # 6 places, 12 commas total
    result = clean_location("; ".join(places))

    assert result == "City1, ST, US; City2, ST, US; City3, ST, US; " \
        "City4, ST, US; City5, ST, US; +1 more"


def test_clean_location_falls_back_to_comma_when_semicolon_alone_is_under_the_cap() -> None:
    """A single place's own name can be long enough to read as many segments
    on ", " even with no "; " anywhere -- e.g. a poorly-joined office list."""
    from jobscrape.models import clean_location

    result = clean_location(", ".join(f"Office{n}" for n in range(1, 8)))

    assert result == "Office1, Office2, Office3, Office4, Office5, +2 more"


def test_clean_location_under_the_cap_is_untouched() -> None:
    from jobscrape.models import clean_location

    assert clean_location("Austin, TX; Remote") == "Austin, TX; Remote"


def test_clean_location_never_reports_a_negative_more_count() -> None:
    """The cap decision is made from the PRE-dedup segment count, but the
    "+N more" count itself came from the POST-dedup list -- a string with
    enough repeated segments to cross the cap, but few enough distinct ones
    to land back under it once deduped, used to produce a garbled negative
    count like "NY; +-4 more"."""
    from jobscrape.models import clean_location

    assert clean_location("NY; NY; NY; NY; NY; NY") == "NY"
    result = clean_location(
        "Austin, TX; Austin, TX; Austin, TX; Austin, TX; Austin, TX; Remote"
    )
    assert result == "Austin, TX; Remote"
    assert clean_location("Austin, TX, US") == "Austin, TX, US"


def test_job_cleans_its_location_on_construction() -> None:
    from jobscrape.models import Job

    job = Job(source="neogov", source_job_id="1", title="Engineer", company="City",
              job_url="https://x/1", location_raw="Gaston, SC 29053 | 33.8316 | -81.1126")
    assert job.location_raw == "Gaston, SC 29053"


@pytest.mark.parametrize(
    "bad", ["Remote", "remote", "REMOTE", "NA", "TRUE", "worldwide", "ww", "-",
            "-11; -10; -9.5; -9; -8; -7; -6; -5; -4"],
)
def test_a_placeholder_city_is_blanked_not_stored(bad: str) -> None:
    """A workplace word, a "no restriction" sentinel, or a plain garbage
    value has independently shown up as `city` from Indeed, SmartRecruiters,
    Workday, Ashby, BambooHR, Adzuna, Breezy, and Himalayas — never a real
    place, in any of them. Caught once here rather than per-adapter."""
    from jobscrape.models import Job

    job = Job(source="test", source_job_id="1", title="Engineer", company="Acme",
              job_url="https://x/1", city=bad, state=bad)
    assert job.city is None
    assert job.state is None


def test_a_real_city_and_state_survive_construction() -> None:
    from jobscrape.models import Job

    job = Job(source="test", source_job_id="1", title="Engineer", company="Acme",
              job_url="https://x/1", city="Austin", state="TX")
    assert job.city == "Austin"
    assert job.state == "TX"


def test_html_entities_in_plain_text_fields_are_unescaped() -> None:
    """Himalayas ships titles like literal "&#x28;Senior&#x29; Full-Stack
    Developer" and Jobicy ships `department` the same way — not HTML (no
    tags), just an upstream serializer that entity-encodes everything. Every
    plain-text field should come out clean, not just the two sources caught
    so far."""
    from jobscrape.models import Job

    job = Job(
        source="test", source_job_id="1",
        title="&#x28;Senior&#x29; Full-Stack Developer - Full Remote",
        company="Johnson &amp; Johnson", job_url="https://x/1",
        department="Sales &amp; Marketing", industry="Tech &amp; Finance",
    )
    assert job.title == "(Senior) Full-Stack Developer - Full Remote"
    assert job.company == "Johnson & Johnson"
    assert job.department == "Sales & Marketing"
    assert job.industry == "Tech & Finance"


def test_description_html_is_not_unescaped_at_construction() -> None:
    """`description_html` stays raw markup — `html_to_text` unescapes and
    strips it together later. Unescaping it here first could turn a
    description's own deliberately-escaped "&lt;script&gt;" into what looks
    like a real tag by the time it gets there."""
    from jobscrape.models import Job

    job = Job(source="test", source_job_id="1", title="Engineer", company="Acme",
              job_url="https://x/1",
              description_html="<p>Don't write &lt;script&gt; tags.</p>")
    assert job.description_html == "<p>Don't write &lt;script&gt; tags.</p>"


def test_a_non_latin_city_name_is_not_mistaken_for_no_letters() -> None:
    """The "no letters at all" placeholder check must be Unicode-aware — an
    ASCII-only `[a-zA-Z]` test would wrongly blank a real Hebrew, Cyrillic,
    or CJK place name as if it were a run of digits/punctuation."""
    from jobscrape.models import Job

    job = Job(source="test", source_job_id="1", title="Engineer", company="Acme",
              job_url="https://x/1", city="בני ברק", state="Казань")
    assert job.city == "בני ברק"
    assert job.state == "Казань"
