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
})

#: The Settings page's grouping, mirroring `.env.example`'s section headers so
#: the two stay easy to compare. `INDEED_PUBLISHER_ID` is carried through even
#: though no source reads it yet — same reason it's still in `.env.example`.
SECTIONS: list[tuple[str, list[str]]] = [
    ("Contact", ["JOBSCRAPE_CONTACT"]),
    ("Database", [
        "DATABASE_URL", "JOBSCRAPE_TEST_DATABASE_URL",
        "JOBSCRAPE_DB_POOL_SIZE", "JOBSCRAPE_DB_MAX_OVERFLOW",
        "JOBSCRAPE_CLOSURE_GRACE_HOURS",
    ]),
    ("Observability", ["JOBSCRAPE_LOG_FORMAT", "JOBSCRAPE_LOG_LEVEL"]),
    ("API keys", [
        "RAPIDAPI_KEY", "JSEARCH_MAX_REQUESTS", "USAJOBS_API_KEY", "USAJOBS_EMAIL",
        "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_API_KEY", "INDEED_PUBLISHER_ID",
    ]),
    ("Politeness / performance tuning", [
        "JOBSCRAPE_CONCURRENCY", "JOBSCRAPE_PER_HOST_CONCURRENCY",
        "JOBSCRAPE_PER_HOST_DELAY", "JOBSCRAPE_TIMEOUT", "JOBSCRAPE_MAX_RETRIES",
        "JOBSCRAPE_CACHE", "JOBSCRAPE_CACHE_TTL", "JOBSCRAPE_CC_COLLECTIONS",
    ]),
    ("Blocked employers", ["JOBSCRAPE_BLOCKED_COMPANIES"]),
    ("Per-source pagination depth", [
        "JOBSCRAPE_WORKDAY_MAX_PER_BOARD", "JOBSCRAPE_HIMALAYAS_MAX_PAGES",
        "JOOBLE_MAX_PAGES", "JOBSCRAPE_ADZUNA_MAX_PAGES", "JOBSCRAPE_ADZUNA_CALL_BUDGET",
    ]),
    ("Apify: Indeed and LinkedIn postings", [
        "APIFY_TOKEN", "APIFY_WEBHOOK_SECRET",
        "APIFY_INDEED_ACTOR", "APIFY_LINKEDIN_ACTOR", "APIFY_PAGE_SIZE",
    ]),
    ("JobRight (manual/local only — needs the jobright extra)", [
        "JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD", "JOBRIGHT_CHROME_VERSION",
    ]),
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
    """
    if not updates:
        return []

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
