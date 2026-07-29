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

log = logging.getLogger("jobscrape.export")

# Ordered for human reading: identity and link first, bulk text last.
COLUMNS = [
    "job_url", "title", "company", "location_raw", "city", "state", "country",
    "workplace_type", "is_remote", "posted_at", "employment_type", "department",
    "salary_min", "salary_max", "salary_currency", "salary_raw",
    "source", "company_slug", "apply_url", "updated_at",
    "description_text", "description_html",
]


def _rows_to_dicts(
    rows: Iterable[dict[str, Any]], columns: Sequence[str], include_html: bool
) -> list[dict[str, Any]]:
    cols = [c for c in columns if include_html or c != "description_html"]
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
    rows: Iterable[dict[str, Any]], path: Path | None = None, include_html: bool = False
) -> Path:
    target = _stamp(path, "csv")
    records = _rows_to_dicts(rows, COLUMNS, include_html)
    fields = list(records[0].keys()) if records else [
        c for c in COLUMNS if include_html or c != "description_html"
    ]
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return target


def to_jsonl(
    rows: Iterable[dict[str, Any]], path: Path | None = None, include_html: bool = True
) -> Path:
    target = _stamp(path, "jsonl")
    with target.open("w", encoding="utf-8") as handle:
        for record in _rows_to_dicts(rows, COLUMNS, include_html):
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return target


#: Excel's hard per-cell limit is 32,767 characters.
_EXCEL_CELL_LIMIT = 32_000


def to_excel(
    rows: Iterable[dict[str, Any]], path: Path | None = None, include_html: bool = False
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
    records = _rows_to_dicts(rows, COLUMNS, include_html)
    fields = list(records[0].keys()) if records else [
        c for c in COLUMNS if include_html or c != "description_html"
    ]

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
        "| Posted | Title | Company | Location | Source | Link |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        posted = row["posted_at"]
        posted = posted.strftime("%Y-%m-%d") if isinstance(posted, datetime) else str(posted or "")[:10]
        title = str(row["title"] or "").replace("|", "\\|")[:80]
        company = str(row["company"] or "").replace("|", "\\|")[:40]
        location = str(row["location_raw"] or "").replace("|", "\\|")[:40]
        lines.append(
            f"| {posted} | {title} | {company} | {location} | {row['source']} | "
            f"[apply]({row['job_url']}) |"
        )
    if any(row["source"] == "remoteok" for row in records):
        lines += ["", f"_{REMOTEOK_ATTRIBUTION}_"]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


WRITERS = {"csv": to_csv, "jsonl": to_jsonl, "xlsx": to_excel, "md": to_markdown}


def export(
    rows: list[dict[str, Any]],
    formats: list[str],
    path: Path | None = None,
    include_html: bool = False,
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
            written.append(writer(rows, target, include_html))
    return written
