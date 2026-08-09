"""`.env` read/write for the Settings page.

`write_env_file` is deliberately NOT the same parser `config._load_dotenv()`
uses to read — that one is read-only and lossy (quotes stripped, no comment
awareness). These tests are about the property that actually matters for a
file the user may also hand-edit: an update touches only the lines it changes,
and everything else survives untouched.
"""

from __future__ import annotations

import pytest

from jobscrape.dashboard import envfile


def test_read_env_file_parses_like_config(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "FOO=bar\n"
        'QUOTED="value with spaces"\n'
    )
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    assert envfile.read_env_file() == {"FOO": "bar", "QUOTED": "value with spaces"}


def test_read_env_file_missing_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / "nope.env")
    assert envfile.read_env_file() == {}


def test_write_env_file_preserves_comments_and_blank_lines(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# section header\n"
        "FOO=old\n"
        "\n"
        "BAR=unchanged\n"
    )
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    changed = envfile.write_env_file({"FOO": "new"})
    assert changed == ["FOO"]
    text = path.read_text()
    assert "# section header" in text
    assert "FOO=new" in text
    assert "BAR=unchanged" in text
    assert "FOO=old" not in text


def test_write_env_file_appends_new_keys_under_a_marker(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("FOO=bar\n")
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    envfile.write_env_file({"NEW_KEY": "value"})
    text = path.read_text()
    assert "# added via dashboard" in text
    assert "NEW_KEY=value" in text
    assert "FOO=bar" in text


def test_write_env_file_on_a_missing_file_creates_it(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    envfile.write_env_file({"FOO": "bar"})
    assert "FOO=bar" in path.read_text()


def test_write_env_file_with_no_updates_is_a_no_op(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / "nope.env")
    assert envfile.write_env_file({}) == []


# --- write_env_file rejects newline injection -------------------------------
#
# The dashboard has no login on any route (docs/DOCKER.md). An embedded `\n`
# in a submitted value turns one `f"{key}={value}"` line into two real lines
# once joined -- one field's value could otherwise write a second, entirely
# different `KEY=value` line into `.env`, bypassing app.py's `ALL_KEYS`
# allowlist (which only checks the submitted field NAME, never a value's
# contents). `DATABASE_URL`/`RENDER_DATABASE_URL` are themselves writable
# through this same form, so this is a real escalation path, not just
# malformed output.


def test_write_env_file_rejects_an_embedded_newline(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("FOO=bar\n")
    monkeypatch.setattr(envfile, "ENV_PATH", path)

    with pytest.raises(ValueError):
        envfile.write_env_file({
            "JOBSCRAPE_CONTACT": "me@example.com\nDATABASE_URL=postgresql://evil/db",
        })
    # Nothing written -- not even the lines that would have been fine alone.
    assert path.read_text() == "FOO=bar\n"


def test_write_env_file_rejects_an_embedded_carriage_return(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("FOO=bar\n")
    monkeypatch.setattr(envfile, "ENV_PATH", path)

    with pytest.raises(ValueError):
        envfile.write_env_file({"JOBSCRAPE_CONTACT": "a\rDATABASE_URL=evil"})
    assert path.read_text() == "FOO=bar\n"


def test_write_env_file_rejects_the_whole_batch_if_any_value_is_bad(tmp_path, monkeypatch) -> None:
    """All-or-nothing, not a partial write that silently drops the bad key --
    a partial success here would still be surprising and hard to notice."""
    path = tmp_path / ".env"
    path.write_text("FOO=bar\n")
    monkeypatch.setattr(envfile, "ENV_PATH", path)

    with pytest.raises(ValueError):
        envfile.write_env_file({
            "JOBSCRAPE_CONTACT": "fine@example.com",
            "USAJOBS_EMAIL": "bad\nDATABASE_URL=evil",
        })
    text = path.read_text()
    assert "JOBSCRAPE_CONTACT" not in text
    assert text == "FOO=bar\n"


def test_mask_keeps_last_four_characters() -> None:
    value = "apify_api_1234567890"
    masked = envfile.mask(value)
    assert masked.endswith(value[-4:])
    assert masked[:-4] == "•" * (len(value) - 4)
    assert envfile.mask("") == ""
    assert envfile.mask("ab") == "••"
