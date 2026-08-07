"""Log into JobRight.ai and read its own internal JSON API directly.

Ported from a separate personal project (JobScoutPro) that scraped JobRight
the same way: a real (or headless) Chrome session, driven by
`undetected-chromedriver`, establishes an authenticated cookie via a normal
browser login — then every page of results comes from calling JobRight's own
`/swan/recommend/...` endpoints with those cookies
(`driver.execute_async_script(fetch(...))`), not from clicking through the
UI. That's faster, gets more fields, and doesn't fire any "Apply" actions.

Two fetch modes, both real and worth keeping:

- **Paged** (`fetch_page`): a plain `--count`-bounded pull from the
  recommend feed.
- **Sweep** (`search_page`, per role/sort/taxonomy): JobRight's own feed
  caps out around 400 results; paging the *search* endpoint per role and
  sort instead is how to collect far more than that.

This needs the `jobright` extra (`pip install '.[jobright]'`) and a real
Chrome install. Not part of the automated schedule — triggered manually or
from the dashboard's Runs page — but it does run inside the Docker image:
`setup_driver()` below auto-starts a virtual display (Xvfb, via
`pyvirtualdisplay`) when no real `DISPLAY` is present, so Chrome still runs
headful rather than falling back to the more easily fingerprinted
`--headless=new`. See `jobscrape/cli.py`'s `jobright-fetch` stage.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ENDPOINT = "/swan/recommend/list/jobs"

# Takes the whole filter in the POST body, so nothing about the account is
# changed -- your saved JobRight filter is left exactly as it is. It also pages
# deeper than the recommend feed, which stops dead at ~400.
SEARCH_ENDPOINT = "/swan/recommend/search"

# Verified against live responses: filtering on each value returned 40/40 jobs
# with the matching workModel string. Remote is 2, not 3.
WORK_MODELS = {"onsite": 1, "remote": 2, "hybrid": 3}

# Every job arrives tagged with a category id. The server will NOT filter on
# these -- passing a real id in the search body returns 0 results, it only
# accepts the free-text 00-00-00 form -- so we filter on our side instead.
# Observed groups:
TAXONOMY_GROUPS = {
    "01-01": "Backend Engineering (backend, java, python, .net, c/c++, go, full stack)",
    "01-02": "Frontend / Mobile / Game",
    "01-04": "System Reliability & Security (cyber security, network, cloud, devops)",
    "01-06": "Machine Learning & AI",
    "01-08": "Data & Analytics",
    "01-09": "Technical Leadership (eng manager, architect)",
}

# What "software engineering" means by default: writing software, not securing
# or operating it, and not data/ML roles.
CORE_SOFTWARE = ("01-01", "01-02")

# Runs in the page context so the session cookies go along automatically.
FETCH_JS = """
const cb = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include'})
  .then(r => r.text())
  .then(cb)
  .catch(e => cb(JSON.stringify({success: false, errorMsg: String(e)})));
"""

POST_JS = """
const cb = arguments[arguments.length - 1];
fetch(arguments[0], {method: 'POST', credentials: 'include',
                     headers: {'Content-Type': 'application/json'},
                     body: arguments[1]})
  .then(r => r.text())
  .then(cb)
  .catch(e => cb(JSON.stringify({success: false, errorMsg: String(e)})));
"""


def _uc_cache_dir():
    """Best-effort path to undetected-chromedriver's driver cache."""
    import undetected_chromedriver as uc

    try:
        return Path(uc.Patcher().data_path)
    except Exception:
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA", "")) / "undetected_chromedriver"
        return Path.home() / ".local" / "share" / "undetected_chromedriver"


class JobRightSession:
    """A logged-in Chrome session against JobRight.ai."""

    def __init__(self, headless: bool = False, chrome_version: int | None = None):
        self.headless = headless
        self.chrome_version = chrome_version
        self.driver: Any = None
        self.main_window: str | None = None
        self._display: Any = None

    def setup_driver(self) -> bool:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()

        # Headful by default. undetected-chromedriver's headless mode is the
        # easiest thing for a site to fingerprint, and locally there's no reason
        # to hide the window — you can watch it work.
        if self.headless:
            options.add_argument("--headless=new")
        elif sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            # No real display (the Docker image, any headless Linux box) —
            # start a virtual one (Xvfb) rather than falling back to
            # `--headless=new`, so Chrome still looks and behaves headful to
            # the site. Kept alive for the process lifetime; there's no
            # multi-session reuse here to justify tearing it down early.
            from pyvirtualdisplay import Display

            self._display = Display(visible=False, size=(1920, 1080))
            self._display.start()

        # Chrome refuses to run as root without this — true for any
        # container that doesn't set up a dedicated non-root user, which is
        # the case here. Only added when actually running as root, so the
        # local (non-root) desktop flow keeps the sandbox.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")

        # Chrome freezes CSS transitions in windows it thinks are occluded or
        # backgrounded. JobRight's login modal is an Ant Design zoom animation,
        # so a throttled window leaves it stuck at opacity:0 forever -- present
        # in the DOM, invisible to Selenium, and send_keys silently no-ops.
        # These keep it rendering even when the window isn't focused.
        options.add_argument("--disable-features=CalculateNativeWinOcclusion")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")

        # --no-sandbox / --disable-dev-shm-usage are Docker workarounds and are
        # deliberately omitted here; --no-sandbox in particular weakens Chrome.

        print("Initializing Chrome driver...")
        for attempt in (1, 2):
            try:
                self.driver = uc.Chrome(
                    options=options, version_main=self.chrome_version
                )
                # undetected-chromedriver swaps out the window it started with,
                # leaving chromedriver pointed at a closed target. Reads still
                # work, but the first click dies with "no such window". Resync
                # onto the window that actually survived.
                #
                # Do NOT navigate here -- login() loads the page itself, and
                # loading it twice leaves a stale copy of the login form in the
                # DOM that swallows the keystrokes.
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.main_window = self.driver.current_window_handle
                return True
            except FileExistsError:
                # uc leaves a half-patched driver behind if a previous run died,
                # then refuses to overwrite it. Clearing the cache fixes it.
                if attempt == 1:
                    cache = _uc_cache_dir()
                    print(f"Stale driver cache, clearing {cache} and retrying...")
                    shutil.rmtree(cache, ignore_errors=True)
                    continue
                raise
            except Exception as e:
                print(f"Error initializing Chrome driver: {e}")
                print(
                    "\nIf this is a version mismatch, pass your Chrome major "
                    "version explicitly, e.g. --chrome-version 151"
                )
                return False
        return False

    def login(self, email: str, password: str) -> bool:
        """Log in, with per-step logging.

        A timeout at any of three different waits would otherwise produce one
        identical, useless error line, so each step and failure mode is
        reported separately.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        for attempt in (1, 2, 3):
            try:
                print(f"Login attempt {attempt}/3...")
                self.driver.get("https://jobright.ai/")
                self.driver.switch_to.window(self.driver.window_handles[-1])
                time.sleep(4)

                print("  clicking SIGN IN...")
                self.driver.execute_script(
                    "arguments[0].click();",
                    self.driver.find_element(By.XPATH, "//span[text()='SIGN IN']"),
                )

                print("  waiting for the login form...")
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.ID, "basic_email"))
                )
                time.sleep(2)

                email_field = self.driver.find_element(By.ID, "basic_email")
                password_field = self.driver.find_element(By.ID, "basic_password")
                email_field.send_keys(email)
                password_field.send_keys(password)
                password_field.send_keys(Keys.RETURN)
                print("  submitted, waiting for the jobs page...")

                # Accept any of three independent success signals rather than
                # betting everything on one span that may get renamed.
                WebDriverWait(self.driver, 45).until(
                    lambda d: (
                        "/jobs/recommend" in d.current_url
                        or d.find_elements(By.XPATH, "//span[text()='Profile']")
                        or d.find_elements(
                            By.XPATH, "//div[contains(@class,'index_job-card-main__')]"
                        )
                    )
                )
                self.main_window = self.driver.current_window_handle
                print(f"  logged in -- now at {self.driver.current_url}")
                return True

            except Exception as e:
                first_line = str(e).splitlines()[0] if str(e).strip() else "timed out"
                print(f"  attempt {attempt} failed: {type(e).__name__}: {first_line}")
                try:
                    print(f"    url: {self.driver.current_url}")
                    errs = [
                        el.text.strip()
                        for el in self.driver.find_elements(
                            By.XPATH,
                            "//*[contains(@class,'ant-form-item-explain')"
                            " or contains(@class,'ant-message-notice')"
                            " or contains(@class,'ant-notification')]",
                        )
                        if el.text.strip()
                    ]
                    if errs:
                        print(f"    page says: {errs}")
                    form_up = bool(self.driver.find_elements(By.ID, "basic_email"))
                    profile = bool(
                        self.driver.find_elements(
                            By.XPATH, "//span[text()='Profile']"
                        )
                    )
                    cards = len(
                        self.driver.find_elements(
                            By.XPATH,
                            "//div[contains(@class,'index_job-card-main__')]",
                        )
                    )
                    print(
                        f"    login form still up: {form_up} | "
                        f"profile span: {profile} | job cards: {cards}"
                    )
                except Exception:
                    pass
                if attempt < 3:
                    time.sleep(5)

        return False

    def quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        if self._display:
            try:
                self._display.stop()
            except Exception:
                pass


def taxonomy_allowed(record: dict[str, Any], include: tuple[str, ...],
                      exclude: tuple[str, ...]) -> bool:
    """Keep or drop a record by its JobRight category id.

    `include`/`exclude` are prefix lists like ("01-01", "01-02"), matched
    against ids such as "01-01-07". Untagged records are kept when only an
    exclude list is given, and dropped when an include list is in force --
    an include list is a positive assertion about what you want.
    """
    tid = record.get("taxonomyId") or ""
    if exclude and any(tid.startswith(p) for p in exclude):
        return False
    if include:
        return bool(tid) and any(tid.startswith(p) for p in include)
    return True


def _bullets(heading: str, items: list[str]) -> str:
    if not items:
        return ""
    return heading + "\n" + "\n".join(f"- {i}" for i in items if i)


def compose_description(summary: str, responsibilities: list[str],
                         requirements: list[str], education: list[str],
                         benefits: list[str]) -> str:
    """Assemble the structured JD pieces into one readable block."""
    parts = [
        summary or "",
        _bullets("Responsibilities:", responsibilities),
        _bullets("Requirements:", requirements),
        _bullets("Education:", education),
        _bullets("Benefits:", benefits),
    ]
    return "\n\n".join(p for p in parts if p).strip()


def flatten(entry: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we care about out of one jobList entry."""
    job = entry.get("jobResult", {}) or {}
    company = entry.get("companyResult", {}) or {}

    # applyLink is the employer's page; originalUrl is the same thing on every
    # sample seen, but keep it as a fallback.
    url = (job.get("applyLink") or job.get("originalUrl") or "").strip()

    summary = job.get("jobSummary", "") or ""
    responsibilities = job.get("coreResponsibilities") or []
    requirements = job.get("skillSummaries") or []
    education = job.get("educationSummaries") or []
    benefits = job.get("benefitsSummaries") or []
    quals = job.get("qualifications") or {}
    skills = job.get("jdCoreSkills") or []

    tax = (job.get("jobTaxonomyList") or [{}])[0]
    taxonomy_id = tax.get("id", "") or ""
    taxonomy_path = " / ".join(
        p for p in (tax.get("second_cat"), tax.get("third_cat")) if p
    )

    return {
        "companyName": company.get("companyName", ""),
        "positionName": job.get("jobTitle", ""),
        "url": url,
        "location": job.get("jobLocation", ""),
        "workModel": job.get("workModel", ""),
        "seniority": job.get("jobSeniority", ""),
        "employmentType": job.get("employmentType", ""),
        "postedAt": job.get("publishTime", ""),
        "applicants": job.get("applicantsCount", ""),
        "minYearsExperience": job.get("minYearsOfExperience", ""),
        "h1bSponsor": job.get("isH1bSponsor", ""),
        "citizenOnly": job.get("isCitizenOnly", ""),
        "clearanceRequired": job.get("isClearanceRequired", ""),
        "jobId": job.get("jobId", ""),
        "companySize": company.get("companySize", ""),
        "companyUrl": company.get("companyURL", ""),
        # No `""` default, unlike the string fields around it: `mapper.to_job`
        # reads this one straight (`record.get("remote")`, not through the
        # `_text()` blank-squashing every other field gets), so a missing
        # `isRemote` must stay `None` here -- an `""` default was read as
        # "not None" downstream and coerced to `is_remote=False`, a false
        # "confirmed onsite" signal for a posting JobRight simply never
        # reported a work model for.
        "remote": job.get("isRemote"),
        "matchScore": entry.get("displayScore", ""),
        # --- the job description, already present in the list response ---
        "summary": summary,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "education": education,
        "benefits": benefits,
        "mustHave": quals.get("mustHave") or [],
        "preferredHave": quals.get("preferredHave") or [],
        "hardSkills": [s.get("skill") for s in skills
                       if s.get("type") == "hard_skill" and s.get("skill")],
        "softSkills": [s.get("skill") for s in skills
                       if s.get("type") == "soft_skill" and s.get("skill")],
        "taxonomy": job.get("jobTaxonomyV3") or [],
        "taxonomyId": taxonomy_id,
        "taxonomyPath": taxonomy_path,
        "description": compose_description(
            summary, responsibilities, requirements, education, benefits
        ),
    }


def fetch_page(driver: Any, position: int, count: int,
                sort_condition: int) -> list[dict[str, Any]]:
    """One page of results from the recommend feed. Returns raw jobList entries."""
    url = (
        f"{ENDPOINT}?refresh=false&sortCondition={sort_condition}"
        f"&position={position}&count={count}&syncRerank=false"
    )
    raw = driver.execute_async_script(FETCH_JS, url)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"API did not return JSON: {str(raw)[:200]}")

    if not data.get("success"):
        raise RuntimeError(
            f"API error: {data.get('errorMsg') or data.get('errorCode')}"
        )
    return data.get("result", {}).get("jobList", []) or []


def search_body(role: str, country: str, flags: dict[str, Any]) -> str:
    """The filter payload. Empty lists mean 'no constraint' on that axis."""
    body = {
        "searchType": "job_title",
        "value": role,
        # 00-00-00 is the free-text taxonomy id the UI uses for a typed query.
        "jobTaxonomyList": [{"taxonomyId": "00-00-00", "title": role}],
        "country": country,
        "jobTypes": [],
        "seniority": [],
        "workModel": [],
        "locations": [],
        "companies": [],
        "isH1BOnly": False,
        "excludeStaffingAgency": False,
        "excludeSecurityClearance": False,
        "excludeUsCitizen": False,
        "recommendationPreference": 2,
    }
    body.update(flags)
    return json.dumps(body)


def search_page(driver: Any, role: str, position: int, count: int,
                 sort_condition: int, country: str,
                 flags: dict[str, Any]) -> list[dict[str, Any]]:
    """One page from the search endpoint. Returns raw jobList entries."""
    url = (
        f"{SEARCH_ENDPOINT}?searchType=job_title&refresh=false"
        f"&count={count}&position={position}&sortCondition={sort_condition}"
    )
    raw = driver.execute_async_script(POST_JS, url, search_body(role, country, flags))
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"search did not return JSON: {str(raw)[:200]}")

    if not data.get("success"):
        raise RuntimeError(
            f"search error: {data.get('errorMsg') or data.get('errorCode')}"
        )
    return data.get("result", {}).get("jobList", []) or []


def collect(
    fetcher: Callable[[int, int], list[dict[str, Any]]],
    limit: int, page_size: int, label: str, delay: float = 1.0,
) -> tuple[list[dict[str, Any]], str]:
    """Page through `fetcher` until `limit`, the feed runs dry, or it errors.

    Returns (entries, note). Errors are captured rather than raised so one dead
    role in a sweep does not lose the rest of the run.

    Caution on "empty": the API throttles by silently returning fewer results
    rather than erroring, so an empty page can mean either a genuinely
    exhausted feed or too many requests too fast. A short page early in the
    run is the tell -- see the warning below.
    """
    entries, position, note = [], 0, ""
    while len(entries) < limit:
        want = min(page_size, limit - len(entries))
        try:
            batch = fetcher(position, want)
        except Exception as e:
            # `fetch_page`/`search_page` raise `RuntimeError` for their OWN
            # known failure modes (bad JSON, an API-level error) -- but
            # `driver.execute_async_script` underneath them can also raise
            # Selenium's own exceptions (a JS timeout, a disconnected
            # driver, a stale window), none of which are `RuntimeError`
            # subclasses. Catching only `RuntimeError` let one of those
            # escape this function entirely, aborting the whole multi-role
            # sweep on the first browser hiccup -- exactly what this
            # function's own docstring promises does not happen.
            note = str(e)
            break
        if not batch:
            note = "empty at position %d" % position
            break
        entries.extend(batch)
        position += len(batch)
        time.sleep(delay)

    suffix = f" ({note})" if note else ""
    warn = ""
    if len(entries) < page_size:
        warn = "   <-- suspiciously low, likely throttled rather than exhausted"
    print(f"    {label}: {len(entries)} entries{suffix}{warn}")
    return entries, note
