"""Config: a blank env var must fall back to its default, not stick as empty.

The dashboard's Settings form submits every field it renders, including ones
that were blank simply because they were never in `.env` to begin with (only
shown blank because the Settings page reads the file, not config.py's own
Python-side defaults) — `app.py`'s `save_settings` now guards against writing
those through unconditionally (see its own comment), but this is the other
half: even a blank value that DOES land in `.env` (by hand, or from before
that guard existed — which is exactly what happened to APIFY_INDEED_ACTOR/
APIFY_LINKEDIN_ACTOR in practice) must not beat a non-empty code default.
Bare `os.environ.get(name, default)` only falls back when the key is
MISSING, not when it's present-but-empty; `_env_str` is the fix.
"""

from __future__ import annotations

import importlib
import os

from jobscrape import config


def test_env_str_falls_back_on_a_missing_key() -> None:
    assert config._env_str("__JOBSCRAPE_TEST_MISSING__", "fallback") == "fallback"


def test_env_str_falls_back_on_a_blank_value(monkeypatch) -> None:
    monkeypatch.setenv("__JOBSCRAPE_TEST_BLANK__", "")
    assert config._env_str("__JOBSCRAPE_TEST_BLANK__", "fallback") == "fallback"


def test_env_str_honours_a_real_value(monkeypatch) -> None:
    monkeypatch.setenv("__JOBSCRAPE_TEST_SET__", "actual")
    assert config._env_str("__JOBSCRAPE_TEST_SET__", "fallback") == "actual"


def test_a_blanked_apify_actor_falls_back_to_the_built_in_default() -> None:
    """The regression this project actually hit: a Settings save once wrote
    `APIFY_INDEED_ACTOR=`/`APIFY_LINKEDIN_ACTOR=` (blank) to `.env`, which
    silently broke `apify-pull` — it built an empty actor list and skipped
    with no error. Reloads the real module under a blanked environment and
    restores it exactly afterward, since these are module-level constants
    computed once at import time.
    """
    keys = ("APIFY_INDEED_ACTOR", "APIFY_LINKEDIN_ACTOR")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = ""
    try:
        importlib.reload(config)
        assert config.APIFY_INDEED_ACTOR == "cheap_scraper~indeed-job-scraper"
        assert config.APIFY_LINKEDIN_ACTOR == "cheap_scraper~linkedin-job-scraper"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)


def test_a_real_apify_actor_override_still_wins() -> None:
    saved = os.environ.get("APIFY_INDEED_ACTOR")
    os.environ["APIFY_INDEED_ACTOR"] = "my-own-actor"
    try:
        importlib.reload(config)
        assert config.APIFY_INDEED_ACTOR == "my-own-actor"
    finally:
        if saved is None:
            os.environ.pop("APIFY_INDEED_ACTOR", None)
        else:
            os.environ["APIFY_INDEED_ACTOR"] = saved
        importlib.reload(config)
