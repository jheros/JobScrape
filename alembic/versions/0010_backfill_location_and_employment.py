"""Re-derive `city`, `state`, `country` and `employment_type` from stored data.

Coverage before this ran, across 33,101 open non-duplicate rows: city 53%,
state 58%, country 37%, employment_type 40%. Five separate causes, all fixed in
code; this applies those fixes to what is already stored, with no refetching.

* `employment_type` held **158 distinct raw strings** because every adapter
  passed its ATS's own vocabulary through untouched — seven spellings of
  "full time" alone covered 11,676 rows. Now one controlled vocabulary.
* `country` held three spellings of the United States at once ("US" 11,074,
  "USA" 998, "United States of America" 1,068), and was NULL on 20,689 rows of
  which 13,400 are resolvable from evidence already present.
* `parse_us_location` could not resolve a bare city name, and the commonest
  unresolved locations in the archive are exactly that: "San Francisco" (1,257),
  "Cupertino" (350), "Seattle" (133), "Austin" (118).
* City and state only filled when BOTH were blank, so 1,835 rows with a state
  never got a city.
* Workday stored the listing's UI summary — literally "2 Locations" — as the
  whole location whenever a detail request failed. 1,118 rows, of which 779
  still hold the real place in `raw_json` and are repaired here.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-01
"""

from __future__ import annotations

import json
import re

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

BATCH = 500

_PLACEHOLDER = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)

FIELDS = ("city", "state", "country", "employment_type")


def _payload(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _coverage(conn) -> dict[str, int]:
    row = conn.execute(
        sa.text(
            "SELECT count(*) AS total, "
            + ", ".join(
                f"count(NULLIF({f}, '')) AS {f}" for f in FIELDS
            )
            + " FROM jobs WHERE closed_at IS NULL AND duplicate_of IS NULL"
        )
    ).mappings().one()
    return dict(row)


def upgrade() -> None:
    from jobscrape.filters import parse_us_location
    from jobscrape.models import (
        classify_employment_type,
        clean_location,
        infer_country,
        normalize_country,
    )

    conn = op.get_bind()
    before = _coverage(conn)

    rows = conn.execute(
        sa.text(
            "SELECT source, source_job_id, title, location_raw, description_text, "
            "       city, state, country, employment_type, raw_json "
            "FROM jobs"
        )
    ).mappings().all()

    updates = []
    repaired_locations = 0
    for row in rows:
        location = row["location_raw"]
        payload = _payload(row["raw_json"])

        # Workday's placeholder: rebuild the location itself before deriving
        # anything from it, otherwise every field below stays blank.
        if row["source"] == "workday" and location and _PLACEHOLDER.match(location):
            primary = payload.get("location")
            extra = [p for p in (payload.get("additionalLocations") or [])
                     if isinstance(p, str) and p.strip()]
            places = [p.strip() for p in
                      ([primary] if isinstance(primary, str) else []) + extra
                      if isinstance(p, str) and p.strip()]
            if places:
                location = clean_location("; ".join(dict.fromkeys(places)))
                repaired_locations += 1

        city, state = row["city"], row["state"]
        if not city or not state:
            parsed_city, parsed_state = parse_us_location(location)
            city = city or parsed_city
            state = state or parsed_state

        country = normalize_country(row["country"]) or infer_country(state, location)

        employment = classify_employment_type(
            row["employment_type"], row["title"], row["description_text"]
        )

        changed = (
            location != row["location_raw"]
            or city != row["city"]
            or state != row["state"]
            or country != row["country"]
            or employment != row["employment_type"]
        )
        if changed:
            updates.append({
                "loc": location, "city": city, "state": state,
                "country": country, "employment": employment,
                "source": row["source"], "job_id": row["source_job_id"],
            })

    query = sa.text(
        "UPDATE jobs SET location_raw = :loc, city = :city, state = :state, "
        "country = :country, employment_type = :employment "
        "WHERE source = :source AND source_job_id = :job_id"
    )
    for start in range(0, len(updates), BATCH):
        conn.execute(query, updates[start:start + BATCH])

    after = _coverage(conn)
    print(f"changed {len(updates):,} of {len(rows):,} stored rows")
    print(f"repaired {repaired_locations:,} Workday placeholder locations")
    total = after["total"]
    for field in FIELDS:
        was, now = before[field], after[field]
        print(f"    {field:16} {was:>7,} -> {now:>7,}   "
              f"({was * 100 // total}% -> {now * 100 // total}%)")


def downgrade() -> None:
    """No-op: the previous per-row values were not retained.

    A refetch recomputes all four from the current classifiers regardless, so
    there is nothing here a rollback could meaningfully restore.
    """
