"""Read and write `.env` for the Settings page.

Deliberately a separate, small parser from `config._load_dotenv()` rather than
a shared one: that function is read-only and lossy by design (quotes stripped,
no comment awareness) and only ever needs to answer "what's the value of this
key right now" for `os.environ.setdefault`. Writing back has a different job —
don't disturb a file the user may also hand-edit — so it gets its own logic.

Only the two tiers `config.py`'s loading model actually supports are attempted
here: `write_env_file` updates the file on disk and the *current process's*
`os.environ`, which is enough for every subsequent subprocess `RunManager`
launches (they inherit `os.environ` at spawn time). It does NOT retroactively
change a module-level constant `config.py` already read at its own import —
that needs a dashboard restart, and the settings route is what tells the user
so, not this module.
"""

from __future__ import annotations

from pathlib import Path

from .. import config

ENV_PATH = config.PROJECT_ROOT / ".env"

#: Keys whose values should render masked (reveal-on-click) in the Settings UI.
SECRET_KEYS = frozenset({
    "APIFY_TOKEN", "APIFY_WEBHOOK_SECRET", "RAPIDAPI_KEY", "ADZUNA_APP_KEY",
    "JOOBLE_API_KEY", "USAJOBS_API_KEY", "JOBRIGHT_PASSWORD",
    "RENDER_DATABASE_URL",
})

#: The Settings page's grouping — trimmed to what a user actually needs to
#: set up front (identity, credentials, the blocklist) rather than every env
#: var `.env.example` documents. Tuning knobs with a working default
#: (DB pool sizing, politeness/performance, per-source pagination depth,
#: log format/level, `JSEARCH_MAX_REQUESTS`, `JOBSCRAPE_TEST_DATABASE_URL`,
#: `INDEED_PUBLISHER_ID` — unread by any source today) are deliberately not
#: here. This is purely a UI trim: every one of those still works exactly as
#: before via `.env`/the real environment, `config.py`'s own reads are
#: completely decoupled from what this list shows — see `read_env_file`'s
#: docstring. Add a key back here if it turns out to need routine adjustment
#: after all.
SECTIONS: list[tuple[str, list[str]]] = [
    ("Contact", ["JOBSCRAPE_CONTACT"]),
    ("Database", ["DATABASE_URL"]),
    ("API keys", [
        "RAPIDAPI_KEY", "USAJOBS_API_KEY", "USAJOBS_EMAIL",
        "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_API_KEY",
    ]),
    ("Blocked employers", ["JOBSCRAPE_BLOCKED_COMPANIES"]),
    ("Apify: Indeed and LinkedIn postings", [
        "APIFY_TOKEN", "APIFY_WEBHOOK_SECRET",
        "APIFY_INDEED_ACTOR", "APIFY_LINKEDIN_ACTOR", "APIFY_PAGE_SIZE",
    ]),
    ("JobRight (manual/local only — needs the jobright extra)", [
        "JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD", "JOBRIGHT_CHROME_VERSION",
    ]),
    ("Render sync", ["RENDER_DATABASE_URL"]),
]

#: Every key the Settings form is allowed to write — anything else in a
#: submitted form body is ignored, the same allowlist discipline `runner.py`
#: uses for CLI flags.
ALL_KEYS = frozenset(key for _, keys in SECTIONS for key in keys)

_MARKER = "# added via dashboard"


def read_env_file() -> dict[str, str]:
    """Every `KEY=value` currently in `.env`, in file order.

    Same parsing rules as `config._load_dotenv()` (blank/`#`-comment lines and
    anything without `=` skipped, split on the first `=` only, one layer of
    matching quotes stripped) — but this reads the FILE, not `os.environ`, so
    it reflects what's on disk even for a key a real environment variable is
    currently overriding.
    """
    if not ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def mask(value: str) -> str:
    """Last 4 characters visible, the rest replaced — never the empty string,
    so a blank field reads as "not set" rather than as a masked value."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * (len(value) - 4) + value[-4:]


def write_env_file(updates: dict[str, str]) -> list[str]:
    """Apply `updates` to `.env`, preserving every other line untouched.

    Only lines matching `KEY=...` for a key in `updates` are rewritten, in
    place, keeping the rest of that line's formatting; a key with no existing
    line is appended at the end under a marker comment. This does not attempt
    to preserve an inline comment trailing an edited value — that's a known,
    accepted limitation, not a bug.

    Returns the list of keys actually changed, so the caller can apply the
    same set to `os.environ` for the current process.

    Rejects (raises `ValueError`, writes nothing) any value containing `\n`
    or `\r`. The dashboard has no login on any route (`docs/DOCKER.md`), and
    a value with an embedded newline turns one `f"{key}={value}"` line into
    two real lines once joined — `JOBSCRAPE_CONTACT=x\nDATABASE_URL=evil`
    submitted as ONE field's value writes a second, completely different
    `KEY=value` line into `.env`, entirely bypassing `ALL_KEYS`'s allowlist
    on which fields the Settings form may write (that allowlist only checks
    the submitted field NAME, never a value's contents). `DATABASE_URL`/
    `RENDER_DATABASE_URL` are themselves writable through this same form, so
    this is a real privilege-escalation path, not just malformed output.
    """
    if not updates:
        return []

    bad = {k: v for k, v in updates.items() if "\n" in v or "\r" in v}
    if bad:
        raise ValueError(
            f"value(s) for {', '.join(sorted(bad))} contain a newline, which "
            "is not allowed in a .env value"
        )

    existing_lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines()
        if ENV_PATH.exists() else []
    )
    remaining = dict(updates)
    out_lines: list[str] = []
    for raw in existing_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out_lines.append(f"{key}={remaining.pop(key)}")
                continue
        out_lines.append(raw)

    if remaining:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(_MARKER)
        for key, value in remaining.items():
            out_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return list(updates.keys())
