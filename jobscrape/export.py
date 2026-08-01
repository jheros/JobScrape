"""Export collected jobs to CSV, JSONL, Excel, or Markdown."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config
from .sources.remoteok import ATTRIBUTION as REMOTEOK_ATTRIBUTION

from . import skills as skills_mod

log = logging.getLogger("jobscrape.export")

# Ordered for human reading: identity and link first, bulk text last.
COLUMNS = [
    "job_url", "title", "company", "location_raw", "city", "state", "country",
    "workplace_type", "seniority", "is_remote", "posted_at",
    "employment_type", "department",
    "salary_min", "salary_max", "salary_currency", "salary_raw",
    "skills",
    "source", "company_slug", "apply_url", "updated_at",
    "description_text",
]

#: The nine fields a job is expected to carry. Used by ``--complete-only``,
#: which keeps a row only when every one of them is populated.
#:
#: Grouped rather than flat because several are satisfied by any of a set:
#: a location may arrive as a raw string or as structured city/state, and a
#: link may be the posting or the apply URL. Requiring one exact column would
#: reject rows that do carry the information.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("title",),
    "source": ("source",),
    "location": ("location_raw", "city", "state"),
    "mode": ("workplace_type",),
    "seniority": ("seniority",),
    "salary": ("salary_min", "salary_max"),
    "skills": ("skills",),
    "description": ("description_text",),
    "link": ("job_url", "apply_url"),
}

#: Placeholders that mean "we could not determine this", not a real value.
#: Without this ``--complete-only`` would accept a row whose mode is the string
#: "unknown" and whose seniority is likewise — which is exactly the row the flag
#: exists to exclude.
_EMPTY_VALUES = {"", "unknown"}


def _is_populated(row: Any, column: str) -> bool:
    value = row[column] if column in row.keys() else None
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in _EMPTY_VALUES:
        return False
    return True


def is_complete(row: Any) -> bool:
    """Whether a row carries all nine expected fields."""
    return all(
        any(_is_populated(row, column) for column in columns)
        for columns in REQUIRED_FIELDS.values()
    )


def missing_fields(row: Any) -> list[str]:
    """Which of the nine are absent — for reporting, not filtering."""
    return [
        name for name, columns in REQUIRED_FIELDS.items()
        if not any(_is_populated(row, column) for column in columns)
    ]


def _rows_to_dicts(
    rows: Iterable[dict[str, Any]], columns: Sequence[str]
) -> list[dict[str, Any]]:
    cols = list(columns)
    out = []
    for row in rows:
        record = {c: row[c] if c in row.keys() else None for c in cols}
        if "is_remote" in record and record["is_remote"] is not None:
            record["is_remote"] = bool(record["is_remote"])
        out.append(record)
    return out


def _stamp(path: Path | None, suffix: str) -> Path:
    if path:
        return path
    config.ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return config.EXPORT_DIR / f"jobs-{ts}.{suffix}"


def to_csv(
    rows: Iterable[dict[str, Any]], path: Path | None = None
) -> Path:
    target = _stamp(path, "csv")
    records = _rows_to_dicts(rows, COLUMNS)
    fields = list(records[0].keys()) if records else list(COLUMNS)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return target


def to_jsonl(
    rows: Iterable[dict[str, Any]], path: Path | None = None
) -> Path:
    target = _stamp(path, "jsonl")
    with target.open("w", encoding="utf-8") as handle:
        for record in _rows_to_dicts(rows, COLUMNS):
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return target


#: Excel's hard per-cell limit is 32,767 characters.
_EXCEL_CELL_LIMIT = 32_000


def to_excel(
    rows: Iterable[dict[str, Any]], path: Path | None = None
) -> Path:
    """Write .xlsx using openpyxl directly.

    Deliberately avoids pandas: it pulls in numpy, which is a common source of
    binary-incompatibility breakage, and offers nothing we need for a flat
    table dump.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError(
            "Excel export needs openpyxl — install with: pip install openpyxl"
        ) from exc

    target = _stamp(path, "xlsx")
    records = _rows_to_dicts(rows, COLUMNS)
    fields = list(records[0].keys()) if records else list(COLUMNS)

    workbook = Workbook(write_only=False)
    sheet = workbook.active
    sheet.title = "Jobs"
    sheet.append(fields)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for record in records:
        sheet.append([_excel_value(record.get(f)) for f in fields])

    sheet.freeze_panes = "A2"
    if records:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(fields))}{len(records) + 1}"
    # Rough but useful column widths; description columns stay narrow.
    for index, field in enumerate(fields, start=1):
        width = 60 if field in ("job_url", "apply_url", "title") else 18
        if field == "skills":
            width = 44
        if field.startswith("description"):
            width = 80
        sheet.column_dimensions[get_column_letter(index)].width = width

    workbook.save(target)
    return target


def _excel_value(value: Any) -> Any:
    if isinstance(value, str):
        # Strip control characters openpyxl refuses, then clamp the length.
        cleaned = "".join(ch for ch in value if ch >= " " or ch in "\t\n")
        return cleaned[:_EXCEL_CELL_LIMIT]
    if isinstance(value, bool):
        return value
    if isinstance(value, datetime) and value.tzinfo is not None:
        # openpyxl refuses timezone-aware datetimes outright; Postgres returns
        # everything as UTC-aware, so converting to UTC and dropping tzinfo
        # loses no information, just the (always-UTC) label.
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def to_markdown(rows: Iterable[dict[str, Any]], path: Path | None = None) -> Path:
    """A compact link-first report — closest thing to 'just the job links'."""
    target = _stamp(path, "md")
    records = list(rows)
    lines = [
        "# US Software Jobs",
        "",
        f"{len(records)} postings · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "",
        "| Posted | Title | Company | Location | Mode | Level | Salary | Skills | Source | Link |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        posted = row["posted_at"]
        posted = posted.strftime("%Y-%m-%d") if isinstance(posted, datetime) else str(posted or "")[:10]
        title = str(row["title"] or "").replace("|", "\\|")[:80]
        company = str(row["company"] or "").replace("|", "\\|")[:40]
        location = str(row["location_raw"] or "").replace("|", "\\|")[:40]
        mode = _md_cell(row, "workplace_type")
        level = _md_cell(row, "seniority")
        salary = _md_salary(row)
        # Only the first few skills — a posting can name twenty, and a table
        # cell that wide makes the whole report unreadable.
        skills = ", ".join(skills_mod.parse_field(
            row["skills"] if "skills" in row.keys() else "")[:5]) or "—"
        lines.append(
            f"| {posted} | {title} | {company} | {location} | {mode} | {level} | "
            f"{salary} | {skills} | {row['source']} | [apply]({row['job_url']}) |"
        )
    if any(row["source"] == "remoteok" for row in records):
        lines += ["", f"_{REMOTEOK_ATTRIBUTION}_"]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _md_cell(row: Any, column: str) -> str:
    """A markdown cell for a value that is often absent or "unknown"."""
    value = row[column] if column in row.keys() else None
    text = str(value or "").strip()
    return "—" if not text or text.lower() == "unknown" else text


def _md_salary(row: Any) -> str:
    """Render a pay range, tolerating either end being absent.

    A one-sided range is real and not rare: migration 0003 cleared 13 salary
    floors that were stored as a literal 0 while keeping their genuine
    ceilings, and some sources publish only an upper bound. Formatting assumed
    both ends were present and raised on those rows.
    """
    low = row["salary_min"] if "salary_min" in row.keys() else None
    high = row["salary_max"] if "salary_max" in row.keys() else None
    if low is None and high is None:
        return "—"
    if low is None:
        return f"up to ${high:,.0f}"
    if high is None:
        return f"${low:,.0f}+"
    if high == low:
        return f"${low:,.0f}"
    return f"${low:,.0f}–${high:,.0f}"


WRITERS = {"csv": to_csv, "jsonl": to_jsonl, "xlsx": to_excel, "md": to_markdown}


def export(
    rows: list[dict[str, Any]],
    formats: list[str],
    path: Path | None = None,
) -> list[Path]:
    written: list[Path] = []
    for fmt in formats:
        writer = WRITERS.get(fmt)
        if not writer:
            log.warning("Unknown export format %r, skipping", fmt)
            continue
        target = path.with_suffix(f".{fmt}") if path else None
        if fmt == "md":
            written.append(writer(rows, target))
        else:
            written.append(writer(rows, target))
    return written
