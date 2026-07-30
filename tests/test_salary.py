"""Tests for salary extraction.

Most sources expose no structured pay field, so the range is recovered from the
description. The risk is false positives — funding rounds, revenue figures and
discounts all look like dollar ranges — so rejection cases matter as much as
the matches.

``salary_min``/``salary_max`` are **always annual**. An hourly range is scaled
by ``HOURS_PER_YEAR`` on the way in, because there is no ``salary_period``
column to tell a reader which kind of number they are looking at.
"""

from __future__ import annotations

import pytest

from jobscrape.models import Job, parse_salary


@pytest.mark.parametrize(
    "text,low,high",
    [
        ("The salary range for this role is $120,000 - $180,000 per year.", 120_000, 180_000),
        ("Compensation: $91,300 — $184,900", 91_300, 184_900),
        ("Base pay range $120K–$180K", 120_000, 180_000),
        ("Expected range: $85,000 to $110,000", 85_000, 110_000),
        # Hourly ranges are annualised — see the module docstring.
        ("Hourly rate of $55.00 to $70.00 per hour", 55.0 * 2080, 70.0 * 2080),
        ("Pay scale $30 - $45 / hour", 30.0 * 2080, 45.0 * 2080),
    ],
)
def test_valid_ranges(text: str, low: float, high: float) -> None:
    got_low, got_high, currency, _ = parse_salary(text)
    assert (got_low, got_high) == (low, high)
    assert currency == "USD"


@pytest.mark.parametrize(
    "text",
    [
        "We raised $50,000,000 - $70,000,000 in Series C funding.",
        "Save $5 to $10 on your first order",
        "Our customers save $2 - $3 million annually",
        "No compensation information here",
        "",
    ],
)
def test_non_salary_amounts_rejected(text: str) -> None:
    low, high, _, _ = parse_salary(text)
    assert low is None and high is None


def test_none_input() -> None:
    assert parse_salary(None) == (None, None, None, None)


def test_reversed_range_rejected() -> None:
    low, _, _, _ = parse_salary("salary of $180,000 - $120,000")
    assert low is None


def test_job_backfills_salary_from_description() -> None:
    """A source with no salary field still gets a range from its description."""
    job = Job(
        source="t", source_job_id="1", title="Software Engineer", company="Acme",
        job_url="https://x/1",
        description_html="<p>Details.</p><p>The salary range is $150,000 - $200,000.</p>",
    )
    assert job.salary_min == 150_000
    assert job.salary_max == 200_000
    assert job.salary_currency == "USD"


def test_structured_salary_is_not_overwritten() -> None:
    """An explicit value from the API always wins over description scraping."""
    job = Job(
        source="t", source_job_id="1", title="SWE", company="Acme",
        job_url="https://x/1",
        salary_min=100_000, salary_max=140_000, salary_currency="USD",
        description_html="<p>The salary range is $999,000 - $1,000,000.</p>",
    )
    assert job.salary_min == 100_000
    assert job.salary_max == 140_000


def test_salary_found_late_in_long_description() -> None:
    """Pay ranges usually sit at the end of a posting, not the beginning."""
    body = "Responsibilities. " * 800 + "The base salary range is $130,000 - $170,000."
    job = Job(
        source="t", source_job_id="1", title="SWE", company="Acme",
        job_url="https://x/1", description_text=body,
    )
    assert job.salary_min == 130_000


def test_single_decimal_abbreviated_amounts_parse() -> None:
    """Regression: `\\d{2}` required exactly two decimals, so "$211.4K" matched
    nothing and the range was silently read as "no salary" — 429 rows in the
    archive carried a salary this dropped. Ashby publishes comp in this form."""
    assert parse_salary("$211.4K – $290.6K • Offers Equity")[:2] == (211_400, 290_600)
    assert parse_salary("$50.5K to $75.25K annually")[:2] == (50_500, 75_250)
    # The two-decimal and no-decimal forms must keep working.
    assert parse_salary("$213K – $251K")[:2] == (213_000, 251_000)
    assert parse_salary("$55.00 to $70.00 per hour")[:2] == (55.0 * 2080, 70.0 * 2080)


def test_single_decimal_does_not_defeat_the_plausibility_guard() -> None:
    """Accepting one decimal place must not let funding rounds through."""
    assert parse_salary("raised $1.5M - $2M in funding")[0] is None
    assert parse_salary("$1.5K - $2.5K per week")[0] is None


def test_hourly_ranges_are_annualised_but_the_text_is_kept_verbatim() -> None:
    """One column, one meaning — with the original still on record.

    488 of 29,898 salaried rows held a raw hourly rate in the same column as
    annual figures, across 11 sources. Nothing downstream could tell 22.0 from
    220000.0 apart, so every salary filter and sort compared the two and the
    hourly rows sorted to the bottom as if they paid nothing.
    """
    low, high, currency, matched = parse_salary("Pay range: $22.00 - $24.00 per hour")
    assert (low, high) == (22.0 * 2080, 24.0 * 2080)
    assert currency == "USD"
    # The conversion has to stay auditable.
    assert matched == "$22.00 - $24.00"


def test_the_plausibility_band_is_judged_before_scaling() -> None:
    """The bands describe a rate as written; scaling first would break them."""
    # $600/hour is outside the hourly band and must be rejected, not scaled.
    assert parse_salary("consulting at $600 - $800 per hour")[0] is None
    # An annual range must not be checked against the hourly band.
    assert parse_salary("salary of $120,000 - $180,000")[0] == 120_000


def test_an_annualised_hourly_range_is_not_re_scaled_by_the_annual_band() -> None:
    """$70/hr becomes $145,600 — which must not then look like a fresh match."""
    job = Job(source="t", source_job_id="1", title="SWE", company="Acme",
              job_url="https://x/1",
              description_text="The hourly rate is $55.00 to $70.00 per hour.")
    assert job.salary_min == 55.0 * 2080
    assert job.salary_max == 70.0 * 2080
