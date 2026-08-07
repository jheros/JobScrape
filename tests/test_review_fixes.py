"""Regressions found by a full-project review.

Each test here corresponds to a defect that shipped and was not caught by the
existing suite. The comments record *why* each was invisible, since that is the
part worth not repeating.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jobscrape import db
from jobscrape.models import Job
from jobscrape.sources.base import shallow_refresh


# --- raw_json was being wiped on every shallow refresh ---------------------
#
# `raw` is in `PRESERVED_ON_REFRESH` and `raw_json` is in `upsert_jobs`'s
# COALESCE preserve set, so the intent was always to keep the payload a deep
# pass captured. But `to_row()` serialised an empty dict to the *string* "{}",
# which is not NULL, so COALESCE happily took it and overwrote good data.
#
# Invisible because every existing test asserted on the columns a shallow
# refresh is *known* to preserve (description, salary, dates). Nothing looked at
# raw_json, and nothing downstream reads it — so the loss was silent until a
# backfill needed the payload and found "{}". 771 rows in the archive, 13% of
# Ashby's, and it is why Oracle's native WorkplaceType could not be backfilled.


def test_empty_raw_serialises_to_null_not_a_string() -> None:
    empty = Job(source="t", source_job_id="1", title="X", company="A", job_url="u")
    assert empty.to_row()["raw_json"] is None, (
        'an empty payload must be NULL so the upsert\'s COALESCE preserves the '
        'stored one; "{}" is truthy and overwrites it'
    )


def test_a_real_payload_still_serialises() -> None:
    job = Job(source="t", source_job_id="1", title="X", company="A", job_url="u",
              raw={"WorkplaceType": "Hybrid"})
    assert job.to_row()["raw_json"] == '{"WorkplaceType": "Hybrid"}'


def test_shallow_refresh_keeps_the_stored_payload(_isolated_db) -> None:
    """The end-to-end case: a repeat sweep must not erase what it captured.

    This is what a second pass over any board actually does — adapters emit
    `raw={} if not with_details`, and `shallow_refresh` blanks it besides.
    """
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="oracle", source_job_id="19247", title="Principal Platform Engineer",
        company="acme", job_url="https://x/1",
        raw={"WorkplaceType": "Hybrid", "PrimaryLocation": "Norcross, GA"},
    )])
    stored = conn.execute(
        "SELECT raw_json FROM jobs WHERE source_job_id = ?", ("19247",)
    ).fetchone()["raw_json"]
    assert stored and stored.get("WorkplaceType") == "Hybrid"

    db.upsert_jobs(conn, [shallow_refresh(Job(
        source="oracle", source_job_id="19247", title="Principal Platform Engineer",
        company="acme", job_url="https://x/1",
    ))])
    after = conn.execute(
        "SELECT raw_json FROM jobs WHERE source_job_id = ?", ("19247",)
    ).fetchone()["raw_json"]
    assert after and after.get("WorkplaceType") == "Hybrid", (
        "a shallow refresh erased the stored payload"
    )


def test_a_genuinely_new_payload_still_replaces_the_old_one() -> None:
    """The guard must not freeze the column — only blank must be preserved."""
    job = Job(source="t", source_job_id="1", title="X", company="A", job_url="u",
              raw={"new": True})
    assert job.to_row()["raw_json"] == '{"new": true}'


# --- the log-consumer task could be garbage-collected ----------------------


def test_run_manager_holds_a_reference_to_its_consumer_tasks() -> None:
    """asyncio keeps only a WEAK reference to a running task.

    `_consume` streams a run's output into `run_events` and marks the run
    finished. Unreferenced, the garbage collector was free to destroy it
    mid-flight — the live log would stop dead and the run would sit at
    "running" forever, with no error anywhere to explain it.

    `runner.py` had no tests at all, which is why this survived.
    """
    from jobscrape.dashboard.runner import RunManager

    manager = RunManager()
    assert hasattr(manager, "_consumers"), "no strong reference held"

    async def scenario() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def fake_consume() -> None:
            started.set()
            await finish.wait()

        task = asyncio.create_task(fake_consume())
        manager._consumers.add(task)
        task.add_done_callback(manager._consumers.discard)

        await started.wait()
        assert task in manager._consumers, "task must be held while running"
        finish.set()
        await task
        # ...and released once done, so the set cannot grow without bound
        # across a long-lived dashboard session.
        await asyncio.sleep(0)
        assert task not in manager._consumers, "finished task must be discarded"

    asyncio.run(scenario())


# --- detail results must stay aligned with the jobs they belong to ---------


def test_gather_limited_preserves_length_and_order() -> None:
    """Nine adapters `zip()` a request list against `gather_limited`'s results.

    If that ever returned a different length or order, each posting would get
    another posting's description — plausible-looking, entirely wrong data.
    The `zip(..., strict=True)` at those call sites turns such a change into a
    loud error; this asserts the contract they rely on.
    """
    from jobscrape.http import gather_limited

    async def ok(value: int) -> int:
        await asyncio.sleep(0.001 * (5 - value))   # finish out of order
        return value

    async def boom() -> int:
        raise RuntimeError("this one fails")

    results = asyncio.run(gather_limited([ok(i) for i in range(5)], limit=2))
    assert results == [0, 1, 2, 3, 4], "order must follow the input, not completion"

    mixed = asyncio.run(gather_limited([ok(0), boom(), ok(2)], limit=2))
    assert mixed == [0, None, 2], "a failure must hold its position, not shift the rest"


@pytest.mark.parametrize(
    "module",
    ["adp", "bamboohr", "breezy", "jazzhr", "oracle", "paycom", "pinpoint",
     "rippling", "workday"],
)
def test_detail_zips_are_strict(module: str) -> None:
    """A silent mis-pairing is the worst outcome here, so it must raise."""
    import inspect
    import importlib

    source = inspect.getsource(importlib.import_module(f"jobscrape.sources.{module}"))
    for line in source.splitlines():
        if "zip(" in line and "strict" not in line:
            raise AssertionError(f"{module}: unguarded zip -> {line.strip()}")


# --- the ?-placeholder shim -------------------------------------------------


def test_the_qmark_shim_translates_positionally() -> None:
    from jobscrape.db import _translate_sql

    assert _translate_sql("SELECT * FROM jobs WHERE a = ? AND b = ?") == (
        "SELECT * FROM jobs WHERE a = :p0 AND b = :p1"
    )


def test_the_qmark_trap_is_documented() -> None:
    """Every `?` becomes a parameter, including inside a literal.

    That is a real constraint on anyone writing a query here, and the error it
    produces ("A value is required for bind parameter 'p0'") names nothing that
    would lead you to the cause. The docs next to the shim are the only warning,
    so this asserts they exist.
    """
    import pathlib

    text = pathlib.Path("jobscrape/db.py").read_text()
    header = text[: text.index("_QMARK_RE = re.compile")]
    assert "jsonb_exists" in header, "the JSONB workaround is not documented"
    assert "literal" in header, "the string-literal trap is not documented"


# --- "unknown" was overwriting a definite workplace verdict -----------------
#
# `workplace_type` is derived at construction from the description, the source's
# own workplace field and the structured remote flag. A shallow refresh strips
# all three, so it recomputed "unknown" from nothing — and unlike the other
# derived columns, nothing stopped that being written over a good answer.
#
# Invisible because every existing test on shallow refresh asserted the columns
# it is *known* to preserve, and `workplace_type` is not one of them: it is
# recomputed, not carried. Migration 0006 fixed the affected rows once and a
# later sweep ate them again, which is what a data bug looks like from outside.
# 178 Workday postings stored `remoteType: Hybrid` in their payload and read
# `unknown`.


def test_a_shallow_refresh_cannot_erase_a_workplace_verdict(_isolated_db) -> None:
    conn = _isolated_db
    deep = Job(
        source="workday", source_job_id="42", title="Staff Engineer",
        company="acme", job_url="https://x/42", location_raw="Austin, TX",
        workplace_hint="Hybrid",
        description_text="You will work a hybrid schedule, 3 days in the office.",
    )
    assert deep.workplace_type == "hybrid", "precondition: the deep pass classifies it"
    db.upsert_jobs(conn, [deep])

    shallow = shallow_refresh(Job(
        source="workday", source_job_id="42", title="Staff Engineer",
        company="acme", job_url="https://x/42", location_raw="Austin, TX",
    ))
    assert shallow.workplace_type == "unknown", "precondition: a shallow pass knows nothing"
    db.upsert_jobs(conn, [shallow])

    stored = conn.execute(
        "SELECT workplace_type FROM jobs WHERE source_job_id = ?", ("42",)
    ).fetchone()["workplace_type"]
    assert stored == "hybrid", "a shallow refresh erased the stored workplace verdict"


def test_a_better_informed_pass_still_wins(_isolated_db) -> None:
    """The guard must not freeze the column — only "unknown" is refused."""
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="workday", source_job_id="43", title="Engineer", company="acme",
        job_url="https://x/43", workplace_hint="Onsite",
    )])
    db.upsert_jobs(conn, [Job(
        source="workday", source_job_id="43", title="Engineer", company="acme",
        job_url="https://x/43", workplace_hint="Remote",
    )])
    stored = conn.execute(
        "SELECT workplace_type FROM jobs WHERE source_job_id = ?", ("43",)
    ).fetchone()["workplace_type"]
    assert stored == "remote", "a definite new verdict must replace the old one"


# --- a source's own workplace field, mapped by value not by substring -------


def test_the_workday_negation_is_not_read_as_remote() -> None:
    """Workday ships "Remote work is not available".

    It contains the word, so substring matching set `is_remote=True` — the
    strongest signal the classifier has — asserting the exact opposite of what
    the field says. Both the adapter and the classifier had this bug.
    """
    from jobscrape.models import classify_hint, classify_workplace

    assert classify_hint("Remote work is not available") == "onsite"
    assert classify_workplace("Engineer", None, "Remote work is not available") == "onsite"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Remote work is not available", "onsite"),
        ("Onsite", "onsite"), ("On Site", "onsite"),
        ("Onsite Only", "onsite"), ("Onsite Customer", "onsite"),
        ("Hybrid", "hybrid"), ("Remote/Hybrid", "hybrid"),
        ("Office Tech-Flexible", "hybrid"), ("Office - Flexible", "hybrid"),
        ("Fully Remote or Hybrid if Local", "hybrid"),
        ("Remote", "remote"), ("Remote Home-Based", "remote"),
        ("Remote Customer-Based", "remote"),
    ],
)
def test_every_observed_workday_remote_type(value: str, expected: str) -> None:
    """The full set of values seen in the archive, so a new one is noticed."""
    from jobscrape.models import classify_hint

    assert classify_hint(value) == expected


def test_the_workday_adapter_does_not_flag_the_negation_remote() -> None:
    """The adapter sets `is_remote` from the same mapping, not from a substring.

    Asserted against the real adapter source rather than a re-implementation of
    its expression, because the bug was that the two disagreed.
    """
    import inspect

    from jobscrape.sources import workday

    source = inspect.getsource(workday)
    assert '"remote" in str(remote_type' not in source, (
        "the adapter is substring-matching remoteType again"
    )
    assert "classify_hint(remote_type)" in source, (
        "the adapter must reuse the classifier's value mapping"
    )


# --- anchored phrases beat the permissive sweep -----------------------------


def test_an_anchored_hybrid_phrase_outranks_a_stray_remote() -> None:
    """Why the anchored tier sits above the sweep.

    The sweep can only ever answer "remote", so before this tier existed a
    posting stating a hybrid schedule read as remote if the word appeared once
    anywhere in it — and it usually does, in a benefits list or an EEO block.
    """
    from jobscrape.models import classify_workplace

    posting = ("Staff Engineer. We are a remote-friendly employer. "
               "This role works 3 days in the office each week.")
    assert classify_workplace("Staff Engineer", None, None, full_text=posting) == "hybrid"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("This is a fully remote position.", "remote"),
        ("We are 100% remote.", "remote"),
        ("A remote-first company.", "remote"),
        ("You will spend 2 days in the office.", "hybrid"),
        ("We run a hybrid schedule.", "hybrid"),
        ("This role is on-site only.", "onsite"),
        ("Candidates must be on-site in Austin.", "onsite"),
        ("An in-office position based in our office.", "onsite"),
    ],
)
def test_anchored_phrases_read_the_whole_posting(phrase: str, expected: str) -> None:
    """Deliberately placed past the 1,500-character window the wording tier reads.

    A work arrangement is often stated at the very end of a posting, after the
    responsibilities and the benefits.
    """
    from jobscrape.models import classify_workplace

    padded = ("Engineering at our company. " * 90) + phrase
    assert len(padded) > 1500
    assert classify_workplace("Engineer", None, None, full_text=padded) == expected


# --- the permissive sweep, minus one word ----------------------------------


def test_a_virtual_interview_is_not_a_remote_job() -> None:
    """211 of 3,063 remote postings rested on this word alone.

    "virtual" was always the weakest term in `REMOTE_MENTION` and the comment
    beside it named removing it as the first thing to try.
    """
    from jobscrape.models import REMOTE_MENTION, classify_workplace

    assert "virtual" not in REMOTE_MENTION
    posting = "Engineer. We conduct virtual interviews for all candidates."
    assert classify_workplace("Engineer", None, None, full_text=posting) == "unknown"


def test_the_permissive_sweep_is_otherwise_unchanged() -> None:
    """The rule as asked for: a mention of remote anywhere means remote."""
    from jobscrape.models import classify_workplace

    posting = "Engineer in Austin. Our team collaborates with remote colleagues."
    assert classify_workplace("Engineer", None, None, full_text=posting) == "remote"


# --- a False remote flag is weak evidence, not proof ------------------------
#
# This began as two tests against Apple's `homeOffice` field, whose adapter has
# since been deleted along with Amazon's — both companies are on
# `filters.BLOCKED_COMPANIES` now, so the adapters existed only to fetch
# postings the filter would immediately discard.
#
# The INVARIANT they guarded is not Apple-specific and is worth keeping: a
# structured `is_remote=False` must not end the classification, because plenty
# of sources ship that field as a vestigial constant. All 993 stored Apple rows
# read False; a live fetch found 2 of 417 that were True. So `False` is fed to
# `classify_workplace` as evidence, and any positive wording still wins.


def test_a_true_remote_flag_is_conclusive() -> None:
    from jobscrape.models import classify_workplace

    assert classify_workplace("Engineer", True, None) == "remote"


def test_a_false_remote_flag_does_not_end_the_classification() -> None:
    """Tier 2 returns early only on a True. A False falls through to the
    wording tiers, which is what lets a hybrid schedule stated in the body
    override a source's vestigial flag."""
    from jobscrape.models import classify_workplace

    hybrid = "Engineer. This role follows a hybrid schedule, 3 days in the office."
    assert classify_workplace(hybrid, False, None) == "hybrid"

    remote = "Engineer. This is a fully remote position."
    assert classify_workplace(remote, False, None) == "remote"

    # With nothing else to go on, the False stands.
    assert classify_workplace("Engineer in Austin", False, None) == "onsite"


# --- an oversized payload killed a fetch run -------------------------------
#
# `to_row()` capped the payload by slicing the SERIALISED string at 400,000
# characters. That cuts JSON mid-token, and `raw_json` is a `jsonb` column, so
# Postgres refused the statement:
#
#     psycopg2.errors.InvalidTextRepresentation:
#     invalid input syntax for type json
#
# Not a lost row — a lost *batch*, and the exception propagated out of the
# sweep. One Ashby posting carrying a base64 blob ended a 5-hour fetch.
#
# Invisible because the cap only fires above 400 KB and no fixture was near it:
# every existing raw_json test used a payload of a few dozen bytes.


@pytest.mark.parametrize(
    "raw",
    [
        {"remoteType": "Hybrid", "blob": "A" * 500_000},
        {"blob": "A" * 500_000},
        {"nested": {"deep": ["x" * 100] * 50_000}},
        ["x" * 500_000],
        "A" * 500_000,
    ],
)
def test_an_oversized_payload_still_serialises_to_valid_json(raw: Any) -> None:
    import json

    from jobscrape.models import RAW_JSON_MAX, encode_raw_json

    stored = encode_raw_json(raw)
    assert stored is not None
    assert len(stored) <= RAW_JSON_MAX
    json.loads(stored)  # the assertion: Postgres would accept this


def test_the_cap_is_applied_to_the_data_not_the_string() -> None:
    """The distinction the bug turned on, stated as a test.

    Slicing the serialised form is what produced invalid JSON, so this asserts
    the output is not a prefix of the naive encoding.
    """
    import json

    from jobscrape.models import RAW_JSON_MAX, encode_raw_json

    raw = {"id": "keep-me", "blob": "A" * 500_000}
    naive = json.dumps(raw)[:RAW_JSON_MAX]
    with pytest.raises(json.JSONDecodeError):
        json.loads(naive)
    assert encode_raw_json(raw) != naive


def test_a_reduced_payload_keeps_the_fields_the_backfills_read() -> None:
    """Storing nothing would be safe and would also lose the workplace fields.

    A posting big enough to trip the cap is no less likely to carry a native
    workplace value than any other, and those scalars are cheap to keep.
    """
    import json

    from jobscrape.models import encode_raw_json

    stored = json.loads(encode_raw_json({
        "remoteType": "Hybrid", "homeOffice": True, "WorkplaceType": "Remote",
        "blob": "A" * 500_000, "nested": {"k": ["v"] * 50_000},
    }))
    assert stored["remoteType"] == "Hybrid"
    assert stored["homeOffice"] is True
    assert stored["WorkplaceType"] == "Remote"
    assert stored["_truncated"] is True, "a reduced payload must say so"
    assert "blob" not in stored and "nested" not in stored


def test_an_ordinary_payload_is_untouched() -> None:
    """The cap must not change the common case."""
    import json

    from jobscrape.models import encode_raw_json

    raw = {"id": "1", "remoteType": "Hybrid", "title": "Engineer"}
    assert json.loads(encode_raw_json(raw)) == raw


# --- employment_type held 158 spellings of four facts -----------------------
#
# Every adapter passed its ATS's own vocabulary through untouched, so the column
# mixed "FullTime", "Full-time", "full_time", "full-time", "Full Time",
# "Full-Time" and "Full time" — seven spellings of one fact across 11,676 rows.
# Filtering on it required knowing every ATS's casing convention.
#
# The classifier follows freehire's `leverEmploymentType` (MIT) in using keyword
# CONTAINMENT rather than exact match, which is what lets one shared function
# absorb all 158 spellings without a lookup table.


@pytest.mark.parametrize(
    "raw",
    ["FullTime", "Full-time", "full_time", "full-time", "Full Time", "Full-Time",
     "Full time", "Fulltime", "permanent, full-time", "fulltime_permanent",
     "Full Time Employee", "Permanent - Full Time", "Regular", "Permanent",
     "Employee", "Full-Time/ Salary", "Full-Time  (FT)"],
)
def test_every_full_time_spelling_collapses(raw: str) -> None:
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(raw) == "full_time"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Software Engineering (Full-Time)", "full_time"),
        ("Software Developer (Full-Time)", "full_time"),
        ("DevOps (Full-Time)", "full_time"),
        ("Database Engineer (Full-Time)", "full_time"),
    ],
)
def test_a_category_label_in_the_commitment_field_still_resolves(
    raw: str, expected: str
) -> None:
    """156 Lever rows carry a job category where the schedule should be.

    The adapter reads the right field (`categories.commitment`) — some employers
    simply type a category into it on Lever's own platform. Containment reads
    the schedule out of the label anyway; exact matching would have needed 158
    table entries and still missed the next one.
    """
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Contract", "contract"), ("Contractor", "contract"),
        ("Temporary", "contract"), ("fulltime_fixed_term", "contract"),
        ("part_time", "part_time"), ("Part-time", "part_time"),
        ("Internship", "internship"), ("Intern", "internship"),
    ],
)
def test_the_other_vocabularies_resolve(raw: str, expected: str) -> None:
    assert __import__(
        "jobscrape.models", fromlist=["classify_employment_type"]
    ).classify_employment_type(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The more specific fact wins when a value names two.
        ("Full-Time Contractor", "contract"),
        ("Part-Time Internship", "internship"),
        ("Summer Intern (Part Time)", "internship"),
    ],
)
def test_the_more_specific_arrangement_wins(raw: str, expected: str) -> None:
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Remote", "Software & Database Engineers", "Systems & Network Engineers",
     "Engineering", "", None],
)
def test_a_value_naming_no_schedule_abstains(raw: str | None) -> None:
    """A wrong facet is worse than a missing one.

    Someone filtering on `full_time` loses real matches either way, but only a
    wrong value actively misleads them.
    """
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(raw) is None


def test_the_description_decides_only_when_the_raw_value_could_not() -> None:
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(
        None, "Engineer", "This is a full-time position with benefits."
    ) == "full_time"
    # A raw value naming a category, not a schedule, hands over to the prose.
    assert classify_employment_type(
        "Software & Database Engineers", "Engineer", "This is a contract role."
    ) == "contract"
    # ...but a raw value that DID resolve is never second-guessed.
    assert classify_employment_type(
        "Contract", "Engineer", "This is a full-time position."
    ) == "contract"


def test_a_bare_word_in_prose_is_not_an_arrangement() -> None:
    """Anchored phrases, same reasoning as `ANCHORED_WORKPLACE`.

    "we contract with vendors" and "internal tools" are not employment types.
    """
    from jobscrape.models import classify_employment_type

    assert classify_employment_type(None, "Engineer", "We contract with vendors.") is None
    assert classify_employment_type(None, "Engineer", "Build internal tools.") is None


# --- country was stored in three spellings at once --------------------------


@pytest.mark.parametrize(
    "raw", ["US", "us", "USA", "usa", "United States", "United States of America"],
)
def test_every_us_spelling_collapses(raw: str) -> None:
    """11,074 "US", 998 "USA" and 1,068 "United States of America" coexisted.

    Workday caused most of it: it stored the free-text `country.descriptor`
    while the ISO code sat in `alpha2Code` in the same payload.
    """
    from jobscrape.models import normalize_country

    assert normalize_country(raw) == "US"


def test_an_unknown_country_name_is_kept_not_dropped() -> None:
    """Losing a spelling this table does not know would be worse than storing it."""
    from jobscrape.models import normalize_country

    assert normalize_country("Ruritania") == "Ruritania"
    assert normalize_country(None) is None
    assert normalize_country("  ") is None


@pytest.mark.parametrize("raw", ["worldwide", "Worldwide", "anywhere", "global"])
def test_a_no_restriction_sentinel_is_not_kept_as_an_unknown_country(raw: str) -> None:
    """Breezy's own `country.id` is literally `"worldwide"` for a fully open
    role — unlike "Ruritania" above, this isn't an unrecognized *spelling* of
    a real country, it's a source's way of saying there's no restriction at
    all, which a blank `country` already means."""
    from jobscrape.models import normalize_country

    assert normalize_country(raw) is None


@pytest.mark.parametrize(
    "state,location,expected",
    [
        ("CA", None, "US"),                      # a resolved state implies the US
        (None, "Austin, TX, USA", "US"),         # named outright
        (None, "United States", "US"),
        (None, "Remote", None),                  # says nothing
        (None, "Worldwide", None),
        (None, "Berlin, Germany", None),
        (None, None, None),
        # SmartRecruiters copies Spain's province codes straight into `state`,
        # which collide exactly with real US abbreviations — "MD" (Madrid),
        # "CT" (Cataluña), "AR" (Aragón). An unverified `state` is not
        # trustworthy here; the location text naming Spain outright is.
        ("MD", "Madrid, MD, Spain", None),
        ("CT", "Barcelona, CT, Spain", None),
        ("AR", "Zaragoza, AR, Spain", None),
        # A real US state is unaffected by the same guard when nothing in the
        # text contradicts it.
        ("MD", "Bethesda, MD, USA", "US"),
        # Both a US and a non-US place named: the US signal still wins, same
        # as `is_us_eligible`'s own "US mention anywhere wins" rule.
        ("MD", "Madrid, Spain or Bethesda, MD, United States", "US"),
        # "New Mexico" is a real US state whose full name literally contains
        # the country name "Mexico" as a substring -- found live in the
        # archive (a Workday multi-state remote listing naming "New Mexico"
        # among many other real US states, all correctly abbreviated). Must
        # not be misread the same way the Spain collision is.
        ("NM", "Remote - New Mexico", "US"),
    ],
)
def test_country_is_inferred_only_from_evidence(
    state: str | None, location: str | None, expected: str | None
) -> None:
    """20,689 rows had no country and defaulting them all to "US" was tempting.

    ~7,600 of them say only "Remote", "Worldwide", or nothing. Stamping those
    "US" would be a guess wearing the same clothes as a fact, so they stay None
    — the same reason `workplace_type` has an "unknown".
    """
    from jobscrape.models import infer_country

    assert infer_country(state, location) == expected


# --- bare city names were the commonest unresolved location -----------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("San Francisco", ("San Francisco", "CA")),
        ("Cupertino", ("Cupertino", "CA")),
        ("Seattle", ("Seattle", "WA")),
        ("Austin", ("Austin", "TX")),
        ("Chicago", ("Chicago", "IL")),
        ("San Francisco Office", ("San Francisco", "CA")),
        ("Remote - Austin", ("Austin", "TX")),
        ("NYC", ("New York", "NY")),
    ],
)
def test_a_bare_hub_city_resolves_its_state(
    location: str, expected: tuple[str, str]
) -> None:
    """The parser required a state to be named; these name only a city.

    Measured, they were the top unresolved shapes in the archive: "San
    Francisco" (1,257 rows), "Cupertino" (350), "Seattle" (133), "Austin" (118).
    """
    from jobscrape.filters import parse_us_location

    assert parse_us_location(location) == expected


@pytest.mark.parametrize(
    "location",
    ["Portland", "San Francisco Bay Area", "Silicon Valley", "Bay Area",
     "United States", "Worldwide", "Berlin", "2 Locations", "Remote"],
)
def test_an_ambiguous_or_non_city_resolves_nothing(location: str) -> None:
    """Curated, not a geocoder — the doctrine every dictionary here follows.

    There is a Portland in Oregon and in Maine, so it implies no state. A metro
    area is not a city. Matching on the whole cleaned string rather than by
    containment is what keeps "San Francisco Bay Area" from becoming the city.
    """
    from jobscrape.filters import parse_us_location

    assert parse_us_location(location) == (None, None)


def test_the_existing_state_parsing_is_unchanged() -> None:
    from jobscrape.filters import parse_us_location

    assert parse_us_location("Austin, TX") == ("Austin", "TX")
    assert parse_us_location("Boston, Massachusetts") == ("Boston", "MA")
    assert parse_us_location(None) == (None, None)


def test_city_and_state_fill_independently() -> None:
    """Filling required BOTH to be blank, so one supplied value blocked the other.

    1,835 rows carried a state with no city for exactly this reason.
    """
    from jobscrape.filters import apply_filters

    job = Job(source="t", source_job_id="1", title="Backend Engineer",
              company="Acme", job_url="https://x/1",
              location_raw="Austin, TX", state="TX")
    assert job.city is None
    kept = apply_filters([job])
    assert kept and kept[0].city == "Austin" and kept[0].state == "TX"


# --- Workday stored a UI summary where a place belonged ---------------------


def test_the_n_locations_placeholder_never_becomes_a_location() -> None:
    """1,118 postings had "2 Locations" as their entire location_raw.

    `locationsText` is what Workday's UI shows for a multi-office posting. It
    was the fallback whenever a detail request failed, and it defeats every
    downstream parser while looking like data.
    """
    from jobscrape.sources.workday import _location

    assert _location({}, {"locationsText": "2 Locations"}) is None
    assert _location({}, {"locationsText": "5 Locations"}) is None
    # A real place in the same field is still a usable fallback.
    assert _location({}, {"locationsText": "Austin, TX"}) == "Austin, TX"


def test_the_extra_offices_are_joined_rather_than_summarised() -> None:
    """The real data was one field away the whole time."""
    from jobscrape.sources.workday import _location

    detail = {"location": "Hampton, VA, US", "additionalLocations": ["Omaha, NE, US"]}
    assert _location(detail, {"locationsText": "2 Locations"}) == (
        "Hampton, VA, US; Omaha, NE, US"
    )


def test_a_remote_posting_open_to_every_us_state_is_summarised_not_dumped() -> None:
    """Real Workday data: a posting open to any of the 50 states lists each
    one as its own `additionalLocations` entry -- measured at 1,893
    characters / 52 entries for one real posting. `_location` must cap it
    via `join_locations` rather than store the entire enumeration."""
    from jobscrape.sources.workday import _location

    states = [f"Remote - {n}, USA" for n in range(1, 51)]
    detail = {"location": "Austin, Texas, US", "additionalLocations": states}
    result = _location(detail, {"locationsText": "51 Locations"})

    assert result.startswith("Austin, Texas, US; Remote - 1, USA")
    assert result.endswith("+46 more")
    assert len(result) < 200


def test_workday_prefers_the_iso_code_over_the_descriptor() -> None:
    """Both are in the same payload; only one is a country code."""
    from jobscrape.sources.workday import _country

    assert _country({
        "jobRequisitionLocation": {
            "country": {"alpha2Code": "US", "descriptor": "United States of America"}
        }
    }) == "US"
    # No code published — the name beats storing nothing.
    assert _country({"country": {"descriptor": "United States of America"}}) == (
        "United States of America"
    )
    assert _country({}) is None


# --- structured location parsed, then discarded -----------------------------


def test_bamboohr_and_ukg_keep_the_parts_they_already_parse() -> None:
    """Both read city/state/country and used them only to build a joined string.

    The parts were then re-derived from the string that had just been built out
    of them — when they were right there, structured and unambiguous.
    """
    from jobscrape.sources.bamboohr import _location_parts as bamboo_parts
    from jobscrape.sources.ukg import _location_parts as ukg_parts

    assert bamboo_parts({"location": {"city": "Austin", "state": "TX", "country": "US"}}) == (
        ("Austin", "TX", "US")
    )
    assert bamboo_parts({}) == (None, None, None)

    assert ukg_parts({"Locations": [{"Address": {
        "City": {"Name": "Austin"}, "State": {"Code": "TX"}, "Country": {"Code": "US"},
    }}]}) == ("Austin", "TX", "US")
    assert ukg_parts({}) == (None, None, None)


# --- description_html is no longer stored -----------------------------------


def test_the_html_never_reaches_the_database() -> None:
    """The dataclass field survives; only persistence went.

    `__post_init__` derives `description_text` from the HTML in memory, before
    `to_row()` runs, so dropping the column loses no text.
    """
    job = Job(source="t", source_job_id="1", title="Engineer", company="Acme",
              job_url="https://x/1", description_html="<p>Build things</p>")
    assert job.description_text == "Build things"
    assert "description_html" not in job.to_row()
    assert "description_html" not in db.JOB_COLUMNS


# --- a probe exception was being written as the verdict "dead" --------------


def test_a_probe_error_leaves_the_board_unchecked(_isolated_db) -> None:
    """An exception is not evidence about whether a board exists.

    `probe` does network and parsing work, so a timeout, an unexpected payload
    shape or an adapter bug all landed in the same `except` and were written as
    `dead`. `dead` boards are never selected for a fetch again — `_fetch_boards`
    asks for `status="live"` — so a transient error retired a real employer
    until someone ran `validate --recheck`. It was logged at debug, i.e.
    invisible by default.
    """
    import asyncio

    from jobscrape.discovery import validate as validate_mod
    from jobscrape.sources import BOARD_SOURCES

    conn = _isolated_db
    db.add_companies(conn, "greenhouse", ["boom"])

    class Exploding:
        ats = "greenhouse"

        async def probe(self, fetcher, slug):
            raise RuntimeError("transient parse failure")

    original = BOARD_SOURCES["greenhouse"]
    BOARD_SOURCES["greenhouse"] = Exploding()
    try:
        counts = asyncio.run(validate_mod.validate_ats(
            conn, fetcher=None, ats="greenhouse", limit=10,
        ))
    finally:
        BOARD_SOURCES["greenhouse"] = original
    assert counts.get("dead", 0) == 0, "an error must not be recorded as a verdict"
    assert counts.get("errored", 0) == 1

    row = conn.execute(
        "SELECT status FROM companies WHERE ats = ? AND slug = ?",
        ("greenhouse", "boom"),
    ).fetchone()
    assert row["status"] == "unchecked", "the board must stay eligible for a retry"


# --- an empty board is not a dead board -------------------------------------


@pytest.mark.parametrize("module", ["adp", "oracle", "ukg", "workday"])
def test_an_empty_board_probes_as_empty_not_dead(module: str) -> None:
    """Four adapters returned `None` from a probe of a board with no openings.

    `BoardSource.probe` maps `None` to "dead" and `[]` to "empty", and every one
    of these already returns `None` earlier for a genuinely failed first page —
    so reaching the end means the API answered. Measured before the fix: Workday
    had 0 empty boards out of 83 while every other ATS ran 7-48%.
    """
    import importlib
    import inspect

    source = inspect.getsource(
        importlib.import_module(f"jobscrape.sources.{module}")
    )
    assert "[] if with_details else None" not in source, (
        f"{module}: an empty board still probes as dead"
    )
    assert "None if not with_details else []" not in source, (
        f"{module}: an empty board still probes as dead"
    )


# --- the host semaphore leaked on cancellation ------------------------------


def test_a_cancelled_request_returns_its_host_permit() -> None:
    """`acquire` takes a permit and *then* awaits the politeness delay.

    The caller's `finally` is only installed once `acquire` returns, so a
    cancellation in that window leaked the permit for the lifetime of the
    Fetcher. Several hosts in `config.HOST_LIMITS` are limited to ONE concurrent
    request, so a single cancelled task deadlocked that host for the rest of the
    sweep — reachable from the dashboard's stop button and from any `gather`
    whose sibling raises.
    """
    import asyncio

    from jobscrape.http import _HostLimiter

    async def scenario() -> str:
        limiter = _HostLimiter(concurrency=1, delay=3.0)
        # Prime `_last` so the next acquire actually has to wait.
        (await limiter.acquire("example.com")).release()

        task = asyncio.create_task(limiter.acquire("example.com"))
        await asyncio.sleep(0.05)          # let it reach the sleep
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        try:
            (await asyncio.wait_for(limiter.acquire("example.com"), timeout=6)).release()
            return "recovered"
        except asyncio.TimeoutError:
            return "deadlocked"

    assert asyncio.run(scenario()) == "recovered"


# --- closures could exceed PostgreSQL's bind-parameter ceiling --------------


def test_closing_a_huge_result_set_does_not_exceed_the_bind_limit(
    _isolated_db,
) -> None:
    """`NOT IN (?, ?, …)` bound one parameter per seen id, unbatched.

    `seen_ids` is the RAW crawl result, not the filtered `kept`, and Amazon runs
    11 query terms against a 10,000-per-term cap. Past 65,535 psycopg2 raises,
    the exception escapes `cmd_fetch`'s source loop, `finish_run` never runs and
    the `runs` row is stranded as "running" — which then blocks the dashboard
    from starting another fetch.

    Chunking is not available here: `NOT IN (A ∪ B)` is not `NOT IN A` then
    `NOT IN B`, so a batched version would close everything in the other batch.
    """
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="amazon", source_job_id="keep-me", title="Backend Engineer",
        company="Amazon", job_url="https://x/1",
    )])
    huge = {f"id-{i}" for i in range(70_000)} | {"keep-me"}
    closed = db.mark_board_closures(conn, "amazon", None, huge)

    assert closed == 0, "a posting still listed must not be closed"
    still_open = conn.execute(
        "SELECT closed_at FROM jobs WHERE source_job_id = ?", ("keep-me",)
    ).fetchone()
    assert still_open["closed_at"] is None


# --- purge recorded a display name where a slug belonged --------------------


def test_purge_records_the_company_slug_not_its_name(_isolated_db) -> None:
    """`record_rejected`'s third argument is `company_slug`, and `skippable_ids`
    reads it back against `companies.slug`.

    Writing "Anduril Industries" where `anduril` belongs means the lookup can
    never match, so every posting `purge` deleted was re-downloaded and
    re-rejected on the next sweep — while the log claimed they were "recorded as
    rejected so they are not re-fetched". 8,524 rows in the archive were written
    that way.
    """
    conn = _isolated_db
    db.record_rejected(conn, "greenhouse", "anduril", [("job-1", "2026-07-01")])
    skippable = db.skippable_ids(conn, "greenhouse", "anduril")
    assert "job-1" in skippable, (
        "a posting recorded under its slug must be skippable by that slug"
    )


# --- a link that is not http(s) is not a link -------------------------------


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
     "//evil.example.com/x", "ftp://host/file", "  javascript:x  "],
)
def test_a_dangerous_url_scheme_is_discarded(url: str) -> None:
    """Both URL fields are raw third-party strings rendered into an `href`.

    A posting carrying `javascript:fetch('/api/jobs/bulk', …)` would run in the
    dashboard's origin on click — and that origin has no authentication on any
    of its mutating routes.
    """
    job = Job(source="t", source_job_id="1", title="Engineer", company="A",
              job_url=url, apply_url=url)
    assert job.job_url == ""
    assert job.apply_url == ""


def test_ordinary_links_are_untouched() -> None:
    job = Job(source="t", source_job_id="1", title="Engineer", company="A",
              job_url="https://boards.greenhouse.io/acme/jobs/1",
              apply_url="http://acme.com/apply?id=1")
    assert job.job_url == "https://boards.greenhouse.io/acme/jobs/1"
    assert job.apply_url == "http://acme.com/apply?id=1"


# --- the dashboard could assemble more than its allowlist claimed -----------


def test_only_allowlisted_cli_flags_reach_the_subprocess() -> None:
    """`STAGES` allowlists the stage; `extra` was appended to argv verbatim.

    Every allowlisted flag is a value-less boolean, and that is the property
    that matters: a flag taking a value can redirect where the process writes.
    `--output` on the export stage was an arbitrary file write and `--db`
    repointed the archive — reachable with no credential, because these routes
    take form bodies and a cross-origin form POST needs no preflight.
    """
    from jobscrape.dashboard.runner import SAFE_EXTRA_FLAGS, safe_extra

    assert safe_extra(["--output", "/Users/me/.zshrc"]) == []
    assert safe_extra(["--db", "postgres://evil/x"]) == []
    assert safe_extra(["--recheck", "--output", "/tmp/x"]) == ["--recheck"]
    assert safe_extra(None) == []
    for flag in SAFE_EXTRA_FLAGS:
        assert not flag.startswith("--output") and not flag.startswith("--db")


# --- neither app should publish its route table ------------------------------


def test_neither_app_serves_an_openapi_schema() -> None:
    """`docs_url=None, redoc_url=None` removes Swagger and ReDoc but NOT the
    schema — FastAPI keeps serving /openapi.json unless `openapi_url` is also
    None. On the public webhook that handed an anonymous caller the route table,
    including that the secret is accepted as a path segment."""
    from jobscrape.dashboard.app import app as dashboard_app
    from jobscrape.webhook import app as webhook_app

    assert webhook_app.openapi_url is None
    assert dashboard_app.openapi_url is None


# --- the docs drifted because nothing checked them --------------------------


def test_the_docs_name_every_excluded_role_category() -> None:
    """ARCHITECTURE.md said "six categories" and then listed seven, at a point
    when there were nine.

    Prose counts drift silently every time the registry grows — `early_career`
    and `flight` were added without it. Asserting the *names* rather than a
    number means the docs fail loudly when a category is added and quietly pass
    when one is renamed in both places.
    """
    import pathlib

    from jobscrape.filters import EXCLUDED_ROLE_NAMES

    text = pathlib.Path("docs/ARCHITECTURE.md").read_text()
    section = text.split("Excluded by preference", 1)[1][:600]
    missing = [name for name in EXCLUDED_ROLE_NAMES if f"`{name}`" not in section]
    assert not missing, (
        f"docs/ARCHITECTURE.md does not name these role categories: {missing}"
    )


def test_the_docs_state_the_real_vocabulary_size() -> None:
    """Both docs quote a term count, and it was 133 for a long time after the
    vocabulary was no longer 133."""
    import pathlib

    from jobscrape.skills import VOCABULARY

    size = len(VOCABULARY)
    for name in ("README.md", "docs/ARCHITECTURE.md"):
        text = pathlib.Path(name).read_text()
        assert f"{size}-term" in text, (
            f"{name} does not state the current vocabulary size ({size} terms)"
        )
