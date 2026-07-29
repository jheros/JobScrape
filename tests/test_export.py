"""Tests for the export writers."""

from __future__ import annotations

import csv
import json

import pytest

from jobscrape import db, export
from jobscrape.models import Job


@pytest.fixture()
def populated(tmp_path):
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            Job(source="greenhouse", source_job_id="1", title="Software Engineer",
                company="Acme", job_url="https://example.com/1",
                location_raw="Austin, TX", city="Austin", state="TX",
                posted_at="2026-06-15", description_html="<p>Build things</p>",
                salary_min=120_000, salary_max=180_000, salary_currency="USD"),
            Job(source="remoteok", source_job_id="2", title="Backend Engineer",
                company="Globex", job_url="https://example.com/2",
                location_raw="Remote - US", is_remote=True, posted_at="2026-06-20"),
        ])
        yield conn, tmp_path


def test_csv_roundtrip(populated) -> None:
    conn, tmp_path = populated
    rows = db.query_jobs(conn)
    target = export.to_csv(rows, tmp_path / "out.csv")
    parsed = list(csv.DictReader(target.open(encoding="utf-8")))
    assert len(parsed) == 2
    assert all(r["job_url"].startswith("http") for r in parsed)
    # HTML is excluded by default because it dominates file size.
    assert "description_html" not in parsed[0]
    assert "description_text" in parsed[0]


def test_csv_can_include_html(populated) -> None:
    conn, tmp_path = populated
    rows = db.query_jobs(conn)
    target = export.to_csv(rows, tmp_path / "out.csv", include_html=True)
    parsed = list(csv.DictReader(target.open(encoding="utf-8")))
    assert "description_html" in parsed[0]


def test_jsonl(populated) -> None:
    conn, tmp_path = populated
    target = export.to_jsonl(db.query_jobs(conn), tmp_path / "out.jsonl")
    records = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["title"]
    assert isinstance(records[0]["is_remote"], (bool, type(None)))


def test_markdown_includes_remoteok_attribution(populated) -> None:
    """RemoteOK's terms require crediting them wherever their rows appear."""
    conn, tmp_path = populated
    target = export.to_markdown(db.query_jobs(conn), tmp_path / "out.md")
    text = target.read_text()
    assert "remoteok.com" in text
    assert "| Posted | Title |" in text


def test_markdown_escapes_pipes(tmp_path) -> None:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            Job(source="t", source_job_id="1", title="Engineer | Platform",
                company="A|B", job_url="https://x/1"),
        ])
        target = export.to_markdown(db.query_jobs(conn), tmp_path / "out.md")
    row = [ln for ln in target.read_text().splitlines() if "Engineer" in ln][0]
    # A literal pipe would break the table; it must be escaped.
    assert r"Engineer \| Platform" in row


def test_excel(populated) -> None:
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    conn, tmp_path = populated
    target = export.to_excel(db.query_jobs(conn), tmp_path / "out.xlsx")
    sheet = load_workbook(target).active
    assert sheet.max_row == 3  # header + 2 jobs
    assert [c.value for c in sheet[1]][0] == "job_url"


def test_excel_truncates_oversized_cells(tmp_path) -> None:
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            Job(source="t", source_job_id="1", title="SWE", company="A",
                job_url="https://x/1", description_text="x" * 50_000),
        ])
        target = export.to_excel(db.query_jobs(conn), tmp_path / "out.xlsx")
    sheet = load_workbook(target).active
    cells = [c.value for c in sheet[2] if isinstance(c.value, str)]
    assert all(len(v) <= 32_000 for v in cells)


def test_unknown_format_is_skipped(populated) -> None:
    conn, tmp_path = populated
    written = export.export(db.query_jobs(conn), ["csv", "bogus"], tmp_path / "out")
    assert len(written) == 1
