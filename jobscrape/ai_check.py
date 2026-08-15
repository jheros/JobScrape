"""Second opinion on 5 job-posting dimensions from a locally-hosted LLM.

Four of the five dimensions already have rule-based classifiers elsewhere in
this project: `models.classify_workplace` (remote/hybrid/onsite/unknown),
`filters.is_us_eligible` (deliberately permissive), `models.
classify_employment_type` (full_time/part_time/contract/internship),
`seniority.classify` (title-only, ~55% coverage on the real archive). This
module does not replace any of them -- it asks the same questions again,
with an LLM reading the full description as context, and writes the answer
to separate `ai_`-prefixed columns (see `db.record_ai_check`). A
hallucinated or wrong call here can therefore never corrupt the existing
rule-based fields; at worst it's a second opinion nobody has to trust.

The fifth dimension, whether a posting requires a security clearance or is
a government job, has no rule-based counterpart anywhere in this project --
this is the one dimension where the LLM's answer is the *only* signal.

Deliberately does **not** use `jobscrape.http.Fetcher`: Fetcher's global
timeout (`config.HTTP_TIMEOUT`, sized for web fetches) and its
retry-with-backoff (`config.MAX_RETRIES`, which retries on timeout too) are
wrong for LLM inference, where a single call can legitimately take
10-60+ seconds -- retrying a call that's merely slow, not broken, just
compounds the latency instead of letting it finish. This module opens its
own `httpx.AsyncClient` with an explicit long timeout and no retries.

Ollama's structured-output `format` (JSON-schema-constrained generation)
support has not been verified against whatever Ollama version ends up
installed on the server -- verify live before trusting it (see the plan's
Verification section). `parse_response` has a regex-extraction fallback for
exactly the case where the model's raw output isn't clean JSON.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .models import EMPLOYMENT_TYPES
from .seniority import LEVELS as SENIORITY_LEVELS

log = logging.getLogger("jobscrape.ai_check")

#: Not `models.classify_workplace`'s return values re-derived -- there's no
#: exported vocabulary tuple for them there (only inline literals), so it's
#: declared once here instead of adding an unrequested export elsewhere.
WORKPLACE_TYPES = ("remote", "hybrid", "onsite", "unknown")

#: `models.EMPLOYMENT_TYPES` has no "unknown" entry (`classify_employment_type`
#: returns `None` to mean that) -- the LLM needs an explicit way to say "can't
#: tell" rather than being forced into one of the 4 real buckets.
_EMPLOYMENT_TYPES_WITH_UNKNOWN = (*EMPLOYMENT_TYPES, "unknown")

#: Characters of `description_text` fed to the model. Bounded so one huge
#: posting doesn't balloon prompt size / per-call latency -- a starting
#: point, tune once a real model and context window are known.
MAX_DESCRIPTION_CHARS = 6000

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workplace_type": {"type": "string", "enum": list(WORKPLACE_TYPES)},
        "requires_clearance": {"type": "boolean"},
        "is_us": {"type": "boolean"},
        "employment_type": {"type": "string", "enum": list(_EMPLOYMENT_TYPES_WITH_UNKNOWN)},
        "seniority": {"type": "string", "enum": list(SENIORITY_LEVELS)},
    },
    "required": ["workplace_type", "requires_clearance", "is_us",
                 "employment_type", "seniority"],
}

_SYSTEM_PROMPT = (
    "You are checking a job posting against 5 factual questions. Answer only "
    "from what the posting actually states or clearly implies -- if it's not "
    "in the text, say so honestly rather than guessing. A keyword-based "
    "classifier has already made a first pass at 3 of these questions; your "
    "job is to confirm or correct it using the full posting text, which the "
    "keyword pass didn't have. Respond with JSON only, matching the given "
    "schema exactly."
)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class Verdict:
    """One job's AI-derived answers. Any field can independently be `None` --
    a parse failure or an out-of-vocabulary answer degrades only that one
    field, never the whole verdict (same "abstain beats guessing" stance
    `models.classify_employment_type` already takes)."""

    workplace_type: str | None = None
    requires_clearance: bool | None = None
    is_us: bool | None = None
    employment_type: str | None = None
    seniority: str | None = None

    def is_empty(self) -> bool:
        """True when every field failed to parse -- the caller's signal that
        this row should count as a failure, not a checked-but-blank result."""
        return all(
            v is None for v in (
                self.workplace_type, self.requires_clearance, self.is_us,
                self.employment_type, self.seniority,
            )
        )


def build_prompt(job: dict[str, Any]) -> str:
    """Compose the classification prompt for one `jobs`-table row.

    `job` is expected to carry the columns `db.ai_check_candidates` selects:
    title, company, location_raw, description_text, plus the existing
    rule-based workplace_type/seniority/employment_type as hints -- handed
    to the model as "a keyword classifier already guessed this," the same
    "verify or correct, don't invent from nothing" framing
    `classify_workplace` already uses for a source's own hint field.
    """
    description = (job.get("description_text") or "")[:MAX_DESCRIPTION_CHARS]
    lines = [
        f"Title: {job.get('title') or '(none)'}",
        f"Company: {job.get('company') or '(none)'}",
        f"Location: {job.get('location_raw') or '(none)'}",
        "",
        "Keyword-classifier first pass (verify or correct, don't just repeat):",
        f"  workplace_type: {job.get('workplace_type') or 'unknown'}",
        f"  employment_type: {job.get('employment_type') or 'unknown'}",
        f"  seniority: {job.get('seniority') or 'unknown'}",
        "",
        "Full posting text:",
        description or "(no description available)",
        "",
        "Answer these 5 questions as JSON matching the schema:",
        "1. workplace_type: remote, hybrid, onsite, or unknown if not stated.",
        "2. requires_clearance: true if the posting requires (or says the "
        "candidate must be able to obtain) a security clearance, or if the "
        "employer is a government agency / government contractor role. "
        "false otherwise.",
        "3. is_us: true if this role is based in / open to candidates in the "
        "United States. false if it clearly names a non-US location or scope.",
        "4. employment_type: full_time, part_time, contract, internship, or "
        "unknown if not stated.",
        "5. seniority: one of "
        + ", ".join(SENIORITY_LEVELS[:-1])
        + ", or unknown if the level truly isn't stated or implied anywhere.",
    ]
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery when the model didn't return clean JSON --
    e.g. it wrapped the object in prose or a markdown code fence. Takes the
    first `{...}` block found; returns None rather than raising on anything
    that still doesn't parse."""
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_response(text: str) -> Verdict:
    """Turn the model's raw response text into a `Verdict`. Never raises.

    Tries strict `json.loads` first (the expected shape when Ollama's
    `format`-constrained structured output is working); falls back to
    `_extract_json_object` for a looser response. Each field is
    independently validated against its own vocabulary -- an
    out-of-vocabulary or wrong-type value degrades that one field to
    `None` rather than discarding the whole verdict.
    """
    if not text or not text.strip():
        return Verdict()

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = None
    except (ValueError, TypeError):
        data = None
    if data is None:
        data = _extract_json_object(text)
    if data is None:
        return Verdict()

    workplace_type = data.get("workplace_type")
    if workplace_type not in WORKPLACE_TYPES:
        workplace_type = None

    requires_clearance = data.get("requires_clearance")
    if not isinstance(requires_clearance, bool):
        requires_clearance = None

    is_us = data.get("is_us")
    if not isinstance(is_us, bool):
        is_us = None

    employment_type = data.get("employment_type")
    if employment_type not in _EMPLOYMENT_TYPES_WITH_UNKNOWN:
        employment_type = None

    seniority = data.get("seniority")
    if seniority not in SENIORITY_LEVELS:
        seniority = None

    return Verdict(
        workplace_type=workplace_type,
        requires_clearance=requires_clearance,
        is_us=is_us,
        employment_type=employment_type,
        seniority=seniority,
    )


async def classify_job(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    job: dict[str, Any],
    *,
    timeout: float,
) -> Verdict:
    """POST one job to Ollama's `/api/generate` and return a `Verdict`.

    Mirrors `liveness.probe_url`'s per-row shape: one row in, one verdict
    out, no DB access, every failure mode absorbed into a degraded
    (partially or fully `None`) `Verdict` rather than an exception -- a
    single malformed response must not sink the whole batch, the same
    reasoning `liveness.py`'s `UNCERTAIN` verdict already leans on.
    """
    payload = {
        "model": model,
        "system": _SYSTEM_PROMPT,
        "prompt": build_prompt(job),
        "format": _JSON_SCHEMA,
        "stream": False,
    }
    try:
        resp = await client.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ai_check: request failed for job %s: %s", job.get("id"), exc)
        return Verdict()

    try:
        body = resp.json()
    except (ValueError, TypeError):
        log.warning("ai_check: non-JSON response body for job %s", job.get("id"))
        return Verdict()

    return parse_response(body.get("response", "") if isinstance(body, dict) else "")
