"""Advance-fee and identity-harvest signals in a job description.

Separate from :mod:`jobscrape.staleness` on purpose. Staleness asks "is this
opening still real?" of a genuine employer's posting; this asks "is this posting
a fraud?". They are orthogonal — a scam ad is *fresh*, specific and recently
posted — so folding scam evidence into the staleness convergence count would
make two unrelated things vote on one verdict.

Annotating, not filtering: a match is surfaced as its own badge and never
removes a posting, exactly as ``workplace_type`` and ``staleness`` behave.

Why only three patterns
=======================

JobSentinel (MIT) ships nine. All nine were run against **44,493 real
descriptions** in this archive before any of them were adopted, and six of them
produced **670 matches, none of them a scam**. They failed in three structural
ways, none of which is a tuning problem:

1. **The vocabulary is the job.** Payments, identity and messaging words are the
   *subject matter* of a large slice of software work. The "crypto payment"
   pattern hit Coinbase ("send, receive, and move crypto"), Block ("Cash App
   Pay") and PPRO ("PayPal"); the messaging pattern hit a chatbot role at
   Mindrift and a satellite-navigation role at logos-space ("Signal-in-Space").
2. **The signal is anti-correlated with the risk.** The loudest matches came
   from Airwallex, PsiQuantum, Axon and Peloton — because careful employers
   publish anti-fraud warnings that describe these exact scams in order to warn
   candidates about them.
3. **Recruiter boilerplate.** "will NOT pay a placement fee" alone accounted for
   310 matches, the single largest bucket.

A warning-context guard removed 567 of the 670, and every one of the surviving
103 was still a false positive. So the six were dropped rather than tuned.

The three kept here matched **zero** times across those 44,493 descriptions —
no false positives at all — while still naming the classic advance-fee scam
precisely. They are cheap insurance on the sources where a fraudulent ad could
actually reach us (the aggregators — jooble, adzuna — rather than a company's
own Greenhouse board, which is not how these scams are distributed).

Credit: pattern shapes from JobSentinel (MIT) —
https://github.com/cboyd0319/JobSentinel
"""

from __future__ import annotations

import re

#: ``(name, pattern)``. Each is multi-word and describes an *action being asked
#: of the applicant*, which is what keeps them clear of ordinary product prose.
SCAM_SIGNALS: tuple[tuple[str, str], ...] = (
    # Overpayment / fake-cheque fraud: the applicant is sent a cheque, told to
    # deposit it and wire part back. No legitimate posting discusses this.
    (
        "check_fraud",
        # `mobile deposit` on its own used to be a fourth alternative here,
        # but it's a real, common bank-product *feature name* ("Mobile
        # Deposit" — Chase, USAA, Ally, most consumer banks), not an action
        # asked of the applicant — a fintech/banking engineering posting
        # describing that feature triggers it despite naming zero fraud.
        # The other three alternatives all still require "check" paired
        # with either a qualifying adjective or a direct action verb, so
        # dropping this one rather than tuning it follows the module's own
        # measured precedent above for the six JobSentinel patterns cut for
        # the same reason.
        r"\b(?:cashier'?s\s+check|fake\s+check|deposit\s+(?:the\s+)?check)\b",
    ),
    # Being asked to buy stored-value instruments — the standard way scam
    # proceeds are made irreversible.
    (
        "gift_cards",
        r"\b(?:buy|purchase|get|send)\b.{0,40}?"
        r"\b(?:gift\s+cards?|prepaid\s+cards?|money\s+orders?)\b",
    ),
    # Identity documents demanded *ahead of* an interview or offer. The order
    # matters and is what makes this precise: the ordered form matched nothing,
    # while the unordered variant fired 128 times on employers stating they
    # would never ask.
    (
        "identity_before_hire",
        r"\b(?:before|prior\s+to)\b.{0,40}?\b(?:interview|start|offer)\b.{0,60}?"
        r"\b(?:send|provide|share|submit)\b.{0,50}?"
        r"\b(?:social\s+security\s+number|ssn|bank\s+account|direct\s+deposit"
        r"|passport|date\s+of\s+birth|driver'?s\s+license)\b",
    ),
)

_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in SCAM_SIGNALS)

#: Wording that marks the surrounding text as a *warning about* the scam rather
#: than the scam itself. Kept even though the three patterns above showed no
#: false positives, because an employer adding a fraud notice tomorrow is the
#: single most likely way one would appear.
_WARNING_CONTEXT = re.compile(
    r"(never|not\b|n't\b|without|beware|caution|scam|fraud|phish|impersonat"
    r"|suspicious|illegitimate|legitimate|official\s+(?:career|website|email)"
    r"|we\s+do\s+not|will\s+not)",
    re.I,
)

#: How far either side of a match to read when looking for that warning wording.
_CONTEXT_CHARS = 180

#: Descriptions are scanned to this length. The tail of a long posting is legal
#: boilerplate; scam instructions appear in the body.
_MAX_SCAN_CHARS = 12_000


def scam_signals(description: str | None) -> list[str]:
    """Return the names of any scam patterns the description trips.

    Empty for the overwhelming majority of postings — it matched nothing at all
    across the whole archive, which is the intended resting state for a fraud
    check, not a sign that it is broken.
    """
    if not description:
        return []
    text = description[:_MAX_SCAN_CHARS].replace("\n", " ")
    found: list[str] = []
    for name, pattern in _COMPILED:
        match = pattern.search(text)
        if match is None:
            continue
        window = text[
            max(0, match.start() - _CONTEXT_CHARS) : match.end() + _CONTEXT_CHARS
        ]
        if _WARNING_CONTEXT.search(window):
            # An employer warning candidates about this scam, not running it.
            continue
        found.append(name)
    return found


def has_scam_signals(description: str | None) -> bool:
    return bool(scam_signals(description))


#: Human wording for each signal, for a UI that has to explain the badge.
SIGNAL_REASONS = {
    "check_fraud": "mentions depositing a cheque sent to you — a hallmark of "
                   "overpayment fraud",
    "gift_cards": "asks you to buy gift cards, prepaid cards or money orders",
    "identity_before_hire": "asks for identity or bank details before an "
                            "interview or offer",
}


def scam_reasons(signals: list[str] | None) -> list[str]:
    return [SIGNAL_REASONS[s] for s in (signals or []) if s in SIGNAL_REASONS]
