"""What level is this role?

Read from the **title only**. That is a deliberate limit, not an oversight: the
title is the one field every source supplies, it is required on `Job`, and it is
never blanked by :func:`jobscrape.sources.base.shallow_refresh` — so seniority
recomputes correctly on every pass with no storage guard and no risk of a repeat
sweep erasing it. Descriptions would raise coverage (about half the unclassified
titles state a years-of-experience figure somewhere in the body) at the cost of
a per-posting text scan and a preservation problem; that trade was declined.

Measured on 37,995 open postings, this ladder classifies **54.7%**:

===========  =======
senior        25.5%
staff          8.9%
lead           4.5%
principal      4.5%
manager        4.2%
mid            2.1%
entry          2.0%
intern         1.1%
director       1.0%
executive      0.8%
unknown       45.3%
===========  =======

``unknown`` is the honest answer for a bare "Software Engineer" and is expected
to stay large. Nothing is filtered on this value — it annotates, exactly as
``workplace_type`` and the staleness verdict do.

**Order is the whole design.** 5.8% of titles match more than one rung
("Senior Staff Engineer", "Principal Engineering Manager", "Lead Software
Engineer II"), so the ladder is evaluated top to bottom and the first match
wins. Every ordering decision below is a judgement about which word carries more
information when two collide.
"""

from __future__ import annotations

import re

UNKNOWN = "unknown"

#: ``(level, pattern)`` in precedence order — **first match wins**.
#:
#: The reasoning behind the contentious orderings:
#:
#: * ``intern`` outranks everything. "Software Engineer Intern, Senior Platform
#:   Team" is an internship; the team's name is not the candidate's level.
#: * People-management rungs (executive → director → manager) outrank the
#:   individual-contributor ladder. "Principal Engineering Manager" is a manager
#:   role — the IC word describes the scope of what they manage.
#: * ``principal`` outranks ``staff`` outranks ``senior``, since a title
#:   carrying two IC rungs ("Senior Staff Engineer") names the higher one last
#:   but means the higher one.
#: * ``lead`` sits below ``staff`` but above ``senior``: "Staff Engineer, Lead
#:   for Payments" is staff, while "Lead Engineer" on its own is a rung above a
#:   plain senior.
#: * Numeric suffixes come last, so an explicit word always beats a roman
#:   numeral in "Senior Software Engineer II".
LADDER: tuple[tuple[str, str], ...] = (
    ("intern", r"\b(intern|interns|internship|co-?op|apprentice|trainee)\b"),
    # `chief` is qualified: "Chief Engineer" on a vessel is not an executive,
    # and the role filter already rejects those, but "Chief of Staff" and
    # "Chief Architect" should not read as C-suite either.
    ("executive", r"\b(vp|svp|evp|vice\s+president|head\s+of|c[tioe]o|chief\s+(technology|information|executive|operating|product|data|security)\s+officer)\b"),
    ("director", r"\b(director|dir\.)\b"),
    ("manager", r"\b(manager|mgr\.?)\b"),
    ("principal", r"\b(principal|distinguished|fellow)\b"),
    ("staff", r"\b(senior\s+staff|staff)\b"),
    ("lead", r"\b(tech(nical)?\s+lead|team\s+lead|eng(ineering)?\s+lead|lead)\b"),
    ("senior", r"\b(senior|sr\.?|snr\.?)\b"),
    (
        "entry",
        r"\b(new\s+grad(uate)?|entry[\s-]?level|junior|jr\.?|associate|"
        r"early\s+career|campus|university\s+grad(uate)?|graduate\s+(engineer|developer|program))\b",
    ),
    # Level suffixes: "Software Engineer II", "Software Engineer II, Backend".
    #
    # The numeral must end the title OR be followed by a comma or dash, which is
    # how ATSes append a team name. Anchoring only to the end missed 408 real
    # titles ("Software Engineer II - Device Drivers", "NOC Engineer III,
    # Incident Management Team"); allowing it anywhere would read the "8" in
    # "Section 8 Housing Systems Engineer" as a level.
    #
    # Checked against every unclassified title in the archive: of the 408 this
    # recovers, none is a non-seniority numeral. "Software Engineer Level 2"
    # and "SDET, Level 3" match too, and are also right.
    ("mid", r"\b(ii|iii|2|3)\b\s*(?:[,–—-]|$)"),
    ("entry", r"\b(i|1)\b\s*(?:[,–—-]|$)"),
    ("senior", r"\b(iv|v|4|5)\b\s*(?:[,–—-]|$)"),
)

#: Every level this module can return, in ladder order. ``unknown`` last.
LEVELS: tuple[str, ...] = (
    "intern", "entry", "mid", "senior", "staff", "principal",
    "lead", "manager", "director", "executive", UNKNOWN,
)

_COMPILED = tuple((level, re.compile(pattern, re.I)) for level, pattern in LADDER)


def classify(title: str | None) -> str:
    """Return the seniority level named by a job title, or ``"unknown"``.

    Pure: no clock, no database, no network — so it can be recomputed at any
    time and is safe to call from ``Job.__post_init__``.
    """
    text = (title or "").strip()
    if not text:
        return UNKNOWN
    for level, pattern in _COMPILED:
        if pattern.search(text):
            return level
    return UNKNOWN


#: Display order for a UI that groups by level: junior to senior, then
#: management. Deliberately not alphabetical — "director, entry, executive,
#: intern" tells a reader nothing.
DISPLAY_ORDER: tuple[str, ...] = (
    "intern", "entry", "mid", "senior", "staff", "principal",
    "lead", "manager", "director", "executive", UNKNOWN,
)


def sort_key(level: str | None) -> int:
    """Position in :data:`DISPLAY_ORDER`; unrecognised values sort last."""
    try:
        return DISPLAY_ORDER.index(level or UNKNOWN)
    except ValueError:
        return len(DISPLAY_ORDER)
