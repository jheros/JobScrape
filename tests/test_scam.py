"""Scam-signal detection.

The point of this module is precision, so the rejection cases carry more weight
than the matches. Every phrase in `test_ordinary_postings_are_not_flagged` is
taken verbatim from a real posting in the archive that one of JobSentinel's
nine patterns flagged — six of those patterns were dropped for exactly these
false positives, and these tests are what stop them coming back.
"""

from __future__ import annotations

import pytest

from jobscrape.scam import has_scam_signals, scam_reasons, scam_signals


# --- the three kept patterns fire on real scam wording ---------------------


@pytest.mark.parametrize(
    "text,signal",
    [
        (
            "You will receive a cashier's check for equipment; deposit the check "
            "and wire the balance to our vendor.",
            "check_fraud",
        ),
        (
            "First task: purchase gift cards for the team and send us the codes.",
            "gift_cards",
        ),
        (
            "Before your interview you must submit your social security number "
            "and bank account details to our onboarding agent.",
            "identity_before_hire",
        ),
        (
            "Prior to the offer, please provide a copy of your driver's license.",
            "identity_before_hire",
        ),
    ],
)
def test_scam_wording_is_flagged(text: str, signal: str) -> None:
    assert signal in scam_signals(text)
    assert has_scam_signals(text)


# --- ordinary postings are not ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Fintech and messaging describing their own product. These are why the
        # "crypto payment", "payment request" and "offsite messaging" patterns
        # were dropped: the vocabulary IS the job.
        "This team owns the infrastructure that powers how millions of users "
        "send, receive, and move crypto on Coinbase.",
        "Block is building commerce experiences through Afterpay, Cash App Pay "
        "and Clearpay.",
        "We empower partners such as Ant Group, PayPal and Stripe.",
        "Any time you swipe your credit card, pay through a mobile app, or "
        "withdraw money from the ATM, we are behind it.",
        "Understanding of platform-specific limits, message templates and "
        "approval flows (e.g., WhatsApp template messages).",
        "Develop and maintain the Signal-in-Space Interface Control Document, "
        "including navigation message content.",
        # Recruiter boilerplate — 310 matches on its own, the largest bucket.
        "Granite will NOT pay a fee for any placement resulting from the "
        "receipt of an unsolicited resume.",
        # Ordinary benefits wording.
        "Benefits include direct deposit, medical cover and paid time off "
        "starting on day one.",
        "We offer a competitive salary and equity.",
        "We use Signal internally for incident response chat.",
        # A bank's own product feature name, not an action asked of the
        # applicant — "mobile deposit" alone used to be a fourth check_fraud
        # alternative and fired on postings like this one.
        "We are hiring engineers to build our Mobile Deposit feature used by "
        "millions of customers to deposit checks from their phone.",
        "",
    ],
)
def test_ordinary_postings_are_not_flagged(text: str) -> None:
    assert scam_signals(text) == []
    assert not has_scam_signals(text)


def test_none_description() -> None:
    assert scam_signals(None) == []


# --- the warning-context guard ---------------------------------------------
#
# The loudest false positives came from the most careful employers: Airwallex,
# PsiQuantum, Axon and Peloton all publish fraud notices that describe these
# scams in order to warn candidates about them. Flagging those would invert the
# signal entirely.


@pytest.mark.parametrize(
    "text",
    [
        "Airwallex will never ask you to deposit a check or purchase gift cards "
        "before an interview.",
        "We do not ask candidates to send gift cards or money orders at any "
        "stage of the process.",
        "Beware of fraudulent recruiters who ask you to purchase prepaid cards. "
        "Report any suspicious contact to security@example.com.",
        "Peloton does NOT charge any application, processing or training fee, "
        "and will never ask you to deposit the check we send you.",
    ],
)
def test_an_employer_warning_about_the_scam_is_not_running_it(text: str) -> None:
    assert scam_signals(text) == []


def test_the_guard_does_not_swallow_a_real_scam_that_merely_says_no() -> None:
    """The guard reads a window around the match, so an unrelated "not"
    elsewhere in a long description must not neutralise it."""
    body = (
        "We are a fast growing logistics firm. " + ("Filler sentence. " * 60)
        + "Your first task is to purchase gift cards and send us the codes. "
        + ("More filler. " * 60)
        + "This position is not open to contractors."
    )
    assert "gift_cards" in scam_signals(body)


# --- ordering matters -------------------------------------------------------


def test_identity_pattern_requires_the_demand_to_precede_the_interview() -> None:
    """The unordered variant fired 128 times, entirely on employers stating
    they would never ask. Requiring "before ... provide ..." order is what
    makes it precise."""
    # Ordered: a demand ahead of the interview.
    assert "identity_before_hire" in scam_signals(
        "Before we can start, provide your bank account number."
    )
    # Reverse order — an employer describing normal post-offer paperwork.
    assert scam_signals(
        "Your bank account details are collected by HR after the offer is "
        "signed, ahead of your start date."
    ) == []


# --- plumbing ---------------------------------------------------------------


def test_reasons_are_human_readable() -> None:
    reasons = scam_reasons(["gift_cards", "check_fraud"])
    assert len(reasons) == 2
    assert all(isinstance(r, str) and r for r in reasons)
    # Unknown names are ignored rather than raising.
    assert scam_reasons(["not_a_signal"]) == []
    assert scam_reasons(None) == []


def test_only_the_three_measured_patterns_ship() -> None:
    """A guard on scope. Six of JobSentinel's nine produced 670 matches on
    44,493 real descriptions, none of them a scam; re-adding one should be a
    deliberate act with fresh measurement behind it."""
    from jobscrape.scam import SCAM_SIGNALS

    assert {name for name, _ in SCAM_SIGNALS} == {
        "check_fraud", "gift_cards", "identity_before_hire",
    }


def test_scanning_is_bounded_on_very_long_descriptions() -> None:
    from jobscrape.scam import _MAX_SCAN_CHARS

    buried = "x " * _MAX_SCAN_CHARS + " purchase gift cards and send the codes"
    # Past the scan limit, so not flagged — the tail of a long posting is
    # boilerplate, and an unbounded scan is a denial-of-service on ourselves.
    assert scam_signals(buried) == []
