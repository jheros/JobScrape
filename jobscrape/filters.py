"""Classifiers deciding what counts as a US software job.

These two predicates determine output quality more than anything else in the
project, so both are conservative: when a signal is ambiguous we prefer to drop
the row rather than pollute the dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from . import seniority
from .models import Job

# --- Software role classification ----------------------------------------

# Unambiguous software titles. Matching any of these is sufficient.
_SOFTWARE_TERMS = [
    r"software\s+(engineer|developer|architect|dev)",
    r"\bsde\b", r"\bswe\b", r"\bsdet\b",
    r"(back|front|full)[\s-]?end",
    r"full[\s-]?stack",
    r"\bweb\s+developer\b", r"\bprogrammer\b",
    r"\bmobile\s+(engineer|developer)", r"\b(ios|android)\s+(engineer|developer)",
    r"\b(devops|sre|site\s+reliability)\b",
    r"\bplatform\s+engineer", r"\binfrastructure\s+engineer", r"\bcloud\s+engineer",
    r"\bdata\s+(engineer|scientist)", r"\banalytics\s+engineer",
    r"\b(machine\s+learning|ml|ai|deep\s+learning)\s+(engineer|scientist|researcher)",
    r"\bresearch\s+(engineer|scientist)\b",
    r"\bsecurity\s+engineer", r"\bapplication\s+security", r"\bappsec\b",
    r"\b(qa|quality\s+assurance|test)\s+(engineer|automation)",
    r"\bautomation\s+engineer",
    r"\bembedded\s+(engineer|software)", r"\bfirmware\s+engineer",
    # `network(ing)?` and the `of`-less management forms below are spelled
    # loosely on purpose: these titles are excluded by default, and the accept
    # pattern is what `--include-roles` restores them through. If the exclusion
    # regex matches a spelling the accept regex does not, the flag silently
    # fails to bring it back — `--include-roles network` restoring "Network
    # Engineer" but not "Networking Engineer" is the bug that shape produces.
    r"\bsystems?\s+engineer", r"\bnetwork(ing)?\s+engineer",
    r"\bdatabase\s+(engineer|administrator)", r"\bdba\b",
    r"\btechnical\s+architect",
    # "Principal Architect, Cloud Platform" and friends: a bare "architect"
    # only counts when a technical qualifier sits on either side of it.
    r"\b(cloud|software|data|platform|security|systems?|enterprise|application|"
    r"infrastructure|devops|integration)\s+architect\b",
    r"\barchitect\b[\s,–—-]+(cloud|platform|data|software|security|infrastructure|"
    r"engineering|systems?|application)",
    r"\bengineering\s+manager", r"\bdirector\s+of\s+engineering",
    r"\b(vp|vice\s+president|head)\s+(of\s+)?engineering",
    r"\bcompiler\s+engineer", r"\bgraphics\s+engineer", r"\bgame\s+(engineer|programmer)",
    r"\bblockchain\s+(engineer|developer)", r"\brobotics\s+(engineer|software)",
    r"\bperformance\s+engineer", r"\bintegration\s+engineer",
    r"\bapi\s+engineer", r"\bdeveloper\s+advocate", r"\bdeveloper\s+relations",
    # "Solutions Engineer" and "Solutions Architect" are deliberately absent:
    # both are usually pre-sales and consulting rather than building the
    # product. "Sales Engineer" was already blocked for exactly this reason —
    # these two carried the same work in on a different word. "Forward Deployed
    # Engineer" stays: that one does write code, against a customer deployment.
    r"\bforward\s+deployed\s+engineer",
    r"\bcomputer\s+(scientist|vision)",
    # --- explicitly requested titles ---
    r"\bai\s*/?\s*ml\s+(engineer|developer|scientist)",
    r"\ba\.?i\.?\s+(engineer|developer|architect)",
    r"\bfounding\s+(engineer|developer)",
    r"\bproduction\s+engineer", r"\bproduct\s+engineer",
    r"\bweb\s+engineer",
    r"\bimplementations?\s+engineer",
    r"\bgrowth\s+engineer",
    r"\bdata\s+analyst\b",
    r"\b(platform|reliability|infrastructure|systems?|cloud|backend|frontend)\s+developer",
    r"\breliability\s+engineer",
    # --- ML / data breadth ---
    r"\b(llm|nlp|genai|gen\s?ai|mlops|dataops|aiops)\b",
    r"\bcomputer\s+vision\b", r"\bdeep\s+learning\b",
    r"\bdata\s+(platform|infrastructure|architect)",
    r"\b(analytics|bi|business\s+intelligence)\s+(engineer|developer)",
    r"\b(search|ranking|recommendation|personalization)\s+(engineer|scientist)",
    # --- systems / infra breadth ---
    r"\bdistributed\s+systems?\b", r"\bkernel\s+(engineer|developer)",
    r"\bsystems?\s+programmer\b", r"\bobservability\s+engineer",
    r"\b(release|build|tooling|developer\s+experience|devex|dx)\s+engineer",
    r"\bdeveloper\s+experience\b", r"\bdeveloper\s+productivity\b",
    r"\bstorage\s+engineer", r"\bvirtualization\s+engineer",
    r"\b(sre|site\s+reliability)\b",
    # --- seniority / program variants that imply engineering ---
    r"\bmember\s+of\s+technical\s+staff\b", r"\bmts\b",
    r"\b(staff|principal|distinguished|senior\s+staff)\s+(engineer|developer|scientist)",
    r"\btech(nical)?\s+lead\b", r"\bengineering\s+lead\b",
    r"\b(software|engineering)\s+(intern|internship|apprentice|new\s+grad)",
    r"\b(new\s+grad|early\s+career|university\s+grad)\s+(engineer|developer|software)",
    # --- enterprise / stack-named developers ---
    #
    # Salesforce, ServiceNow, SAP, Siebel, SharePoint, PeopleSoft, Dynamics and
    # Workday used to be accepted here as a group. They are configuration and
    # customisation of a vendor platform rather than product engineering, so the
    # whole group is gone. Mainframe, COBOL, ETL and integration developers stay
    # — legacy stacks, but they write code.
    r"\bmainframe\s+(engineer|developer|programmer)", r"\bcobol\b",
    r"\betl\s+(developer|engineer)", r"\bintegration\s+developer",
    # --- more stacks ---
    r"\b(rails|django|flask|spring|vue|svelte|next\.?js|nuxt|ember|"
    r"flutter|react\s?native|unity|unreal|terraform|kubernetes|k8s|"
    r"elixir|erlang|haskell|clojure|perl|dart|solidity|matlab|"
    r"c\s?sharp|objective[\s-]?c)\s+(engineer|developer)",
    # --- noun-last phrasing: "Engineer, Platform" / "Developer — Backend" ---
    r"\b(engineer|developer)\b[\s,:–—-]+\s*(platform|backend|frontend|full[\s-]?stack|"
    r"infrastructure|data|security|mobile|web|cloud|api|systems?|software|ml|ai|devops|qa)",
    # Common language/stack-named roles.
    r"\b(python|java|golang|go|rust|c\+\+|c#|\.net|ruby|php|scala|kotlin|swift|typescript|javascript|node|react|angular)\s+(engineer|developer)",
    # --- rescued when the bare-word rule was removed ---
    #
    # These are genuine software titles that used to qualify only because they
    # contained the word "engineer" or "developer". Once that rule went they had
    # to stand on their own name, and measuring the archive is how they were
    # found: ~314 postings that would otherwise have been lost with the 5,035.
    r"\bui\b(\s*/\s*ux)?\s+(engineer|developer)",
    r"\bmobile\s+app(lication)?s?\s+(engineer|developer)",
    r"\bdevsecops\b",
    r"\bagile\s+developer\b", r"\bapplications?\s+developer\b",
]
_SOFTWARE_RE = re.compile("|".join(_SOFTWARE_TERMS), re.I)

# There used to be a `_BARE_TECH` rule here that accepted any title containing
# the bare word "engineer" or "developer", with `_NOT_SOFTWARE_RE` expected to
# carry the weight of keeping non-software out. It could not: a blocklist has to
# anticipate every discipline that uses the word, and the archive kept finding
# ones nobody had listed. Measured, it alone admitted 5,035 of 22,839 open
# postings — 1,279 hardware (Design Verification, Electronics, Mechanical, FPGA)
# and 3,442 generic (Design Engineer, General Engineer, "Engineer"), against 314
# genuine software titles now named explicitly in `_SOFTWARE_TERMS` above.
#
# So the filter is an allowlist: a title is kept because it *names* a software
# discipline, never because it merely contains the word. The failure mode is now
# a missed real title rather than an unbounded stream of unrelated ones, and the
# fix for that is one line in the list above.

# Non-software disciplines that also use "engineer"/"developer". These are
# rejected outright, even if a software term also matched (checked first).
_NOT_SOFTWARE_TERMS = [
    # Non-software engineering disciplines.
    r"\b(mechanical|civil|structural|electrical|chemical|industrial|process|"
    r"manufacturing|aerospace|petroleum|mining|nuclear|biomedical|environmental|"
    r"geotechnical|hardware|rf|optical|packaging|materials|thermal|acoustic|"
    r"automotive|marine|agricultural|textile|welding|hvac|plumbing|"
    r"validation|quality|safety|field|facilities|maintenance|project|stationary|"
    # Added when bare "Engineer" became a match on its own.
    r"water|wastewater|traffic|transportation|survey|surveying|geological|geology|"
    r"metallurgical|metallurgy|foundry|boiler|elevator|refrigeration|pipeline|"
    r"mine|drilling|completions|subsea|hydraulic|pneumatic|corrosion|welding|"
    r"paint|coatings|ceramics|polymer|food|dairy|brewing|pharmaceutical|"
    r"clinical|regulatory|forestry|irrigation|landscape|architectural|"
    r"fire|hse|ehs|osha|noise|lighting|acoustical|energy|solar|wind|"
    r"nuclear\s+power|power\s+plant|substation|transmission|distribution|"
    r"broadcast|catv|telecom\s+field|cable|antenna|satellite\s+hardware)\s+engineer",
    # Customer-facing and commercial "engineer" titles.
    r"\b(sales|account|customer\s+success|customer\s+support|pre[\s-]?sales|"
    r"post[\s-]?sales|partner|channel|revenue|support|implementation|training|"
    r"marketing|proposal|bid|estimating|procurement|purchasing|contracts)\s+engineer",
    # Same job as "Sales Engineer", different word. Blocked rather than merely
    # left out of `_SOFTWARE_TERMS`, because "architect" is still a
    # department-confirmable word in `_GENERIC_TECH` — dropping the accept alone
    # let "Solutions Architect" back in under an Engineering department.
    r"\bsolutions?\s+(engineer|architect)\b",
    # Recruiting / admin roles that mention engineering.
    r"\bengineer(ing)?\s+(technician|technologist|aide|assistant|clerk|"
    r"intern(ship)?\s+program\s+coordinator|manager\s+of\s+facilities)",
    r"\b(engineering|technical|software)\s+recruiter\b",
    r"\brecruiter\b", r"\bsourcer\b",
    r"\bengineering\s+(assistant|aide|clerk|admin(istrator)?\s+of\s+records)",
    # "Developer" in non-software senses.
    r"\bbusiness\s+develop(er|ment)\b", r"\breal\s+estate\s+developer",
    r"\bland\s+developer", r"\bproperty\s+developer",
    r"\b(residential|commercial|housing|solar|wind|energy)\s+developer",
    r"\bdeveloper\s+(marketing|relations\s+manager\s+of\s+sales)",
    r"\bfund\s+developer", r"\bdonor\s+developer",
    # Same-word traps.
    r"\btrain\s+(engineer|driver)", r"\blocomotive\b", r"\bflight\s+engineer",
    r"\bmarine\s+engineer", r"\bship\s+engineer", r"\bchief\s+engineer,?\s+(vessel|ship|hotel|building)",
    r"\baudio\s+engineer", r"\bsound\s+engineer", r"\bbroadcast\s+engineer",
    r"\bstudio\s+engineer", r"\bmix(ing)?\s+engineer", r"\bmastering\s+engineer",
    r"\brecording\s+engineer", r"\blive\s+sound\b",
    r"\bdrilling\s+engineer", r"\breservoir\s+engineer",
    r"\bengineer\s+in\s+training\b", r"\bengine\s+(mechanic|technician|assembler)\b",
    r"\bbuilding\s+engineer", r"\bbuilding\s+automation\b",
    r"\boperating\s+engineer", r"\bhvac\b", r"\bjanitorial\b",
]
_NOT_SOFTWARE_RE = re.compile("|".join(_NOT_SOFTWARE_TERMS), re.I)

# Department names that make an otherwise-generic title software.
_SOFTWARE_DEPTS = re.compile(
    r"\b(engineering|software|technology|technical|r&d|research\s+and\s+development|"
    r"product\s+development|data|infrastructure|platform|security|it\b|"
    r"information\s+technology|devops|machine\s+learning|artificial\s+intelligence)\b",
    re.I,
)

# `_NON_SOFTWARE_DEPTS` lived here: a list of departments (facilities, sales,
# nursing, warehouse) used to *veto* a bare "Engineer". It existed only because
# that title was accepted in the first place. With the allowlist, "Engineer"
# under Facilities is rejected on its title alone and the veto has nothing left
# to do.

# Generic titles that only qualify when the department confirms them.
#
# Bare "engineer" and "developer" are deliberately NOT here. They were, and it
# silently undid the allowlist: `_SOFTWARE_DEPTS` matches the department
# "Engineering", which is what almost every engineering board files its postings
# under, so "Design Engineer" and "Chief Engineer" walked straight back in
# through this line. Measured at 1,459 rows — the department is simply not
# evidence that an engineer is a *software* engineer.
#
# What is left is the set the docstring below always meant: words that are
# genuinely ambiguous rather than merely generic. A "Scientist" or "Architect"
# under an Engineering department really is more likely to be technical, and
# neither word appears in the hardware titles this change exists to remove.
_GENERIC_TECH = re.compile(
    r"\b(architect|scientist|technical\s+lead|tech\s+lead|intern|co-?op)\b",
    re.I,
)


# --- Roles that are software but deliberately not wanted ------------------
#
# Distinct from `_NOT_SOFTWARE_TERMS`, which means "this is not a software job
# at all". A QA engineer and an engineering manager both *are* software roles;
# they are excluded by preference, and each preference is individually
# reversible with a flag. Keeping the two lists apart is what lets
# `--include-qa` work without also re-admitting mechanical engineers.

#: QA / test / automation. Word-boundary anchored throughout.
#:
#: `test engineer` and `automation engineer` are deliberately included on the
#: broad reading. That also removes ~268 hardware test roles ("Flight Test
#: Engineer", "Propulsion Test Engineer"), which do not belong in a software
#: archive anyway, and a handful of genuine software titles whose only sin is
#: containing the word — "Data and Automation Engineer - Python" is the clearest
#: example. That trade was chosen knowingly: the alternative left every plain
#: "Test Engineer" in.
_QA_TERMS = [
    r"\b(qa|q\.a\.)\b",
    r"\bquality\s+(assurance|engineer)",
    r"\bsdet\b",
    r"\bsoftware\s+(development\s+)?engineer\s+in\s+test\b",
    r"\bsoftware\s+tester\b", r"\btesters?\b",
    r"\btest\s+(engineer|automation|analyst|architect)",
    r"\bautomation\s+engineer",
]
_QA_RE = re.compile("|".join(_QA_TERMS), re.I)

#: Seniority levels that mean people management rather than engineering.
#:
#: Read from `jobscrape.seniority` rather than restated as regexes here. That
#: module already resolves the cases a naive `\bchief\b` or `\bmanager\b` gets
#: wrong — "Principal Engineering Manager" is a manager despite the IC word, and
#: "Tech Lead" is an IC, so it stays.
#:
#: This layer still matters under the allowlist, because `_SOFTWARE_TERMS` names
#: "engineering manager", "director of engineering", "vp of engineering" and
#: "head of engineering" explicitly — they are kept there so `--include-roles
#: management` can restore them, and this is what removes them by default.
MANAGEMENT_LEVELS = frozenset({"manager", "director", "executive"})

#: The role nouns a category word can modify. Shared by the three patterns
#: below so they widen together and cannot drift apart.
_ROLE_NOUN = r"(?:engineer|architect|administrator|analyst|specialist|developer)"

#: Up to two words may sit between the category word and the role noun.
#:
#: Without this the rules only fired when the two were adjacent, which is not
#: how titles are actually written: "Systems Engineer" was caught while "Systems
#: **Integration** Engineer", "System **Design** Engineer" and "Lead Systems
#: **Operations** Engineer" all sailed through. Measured on 24,057 open
#: postings, 1,481 titles named a category and *none* of them were caught,
#: because every adjacent form had already been removed at fetch time.
#:
#: Two words, not unbounded: enough for the real shapes, while stopping a
#: category word at one end of a long title from binding to a role noun at the
#: other. "Senior Software Engineer - Distributed Systems" keeps its head noun.
_GAP = r"(?:\s+\w+){0,2}\s+"

#: Networking infrastructure.
_NETWORK_RE = re.compile(
    rf"\bnetwork(?:ing)?\b{_GAP}{_ROLE_NOUN}\b",
    re.I,
)

#: Systems engineering and administration.
#:
#: The broad reading, deliberately. Sampling the archive, the large majority are
#: aerospace and defence hardware roles — "Avionics Systems Engineer", "Fluids
#: Systems Engineer", "Spacecraft Data Systems Engineer" — which were only ever
#: in a software archive because "systems engineer" is in the accept list. The
#: cost is a handful of genuine software titles carrying the word, "Backend and
#: System Engineer, Flows" being the clearest.
_SYSTEMS_RE = re.compile(
    rf"\bsystems?\b{_GAP}{_ROLE_NOUN}\b|\bsysadmin\b",
    re.I,
)

#: "Systems" that names a *software* discipline rather than the systems
#: engineering one.
#:
#: Distributed systems is core backend work and systems programming is OS and
#: compiler work; neither is what "no systems engineer" means, and the test
#: suite asserted "Distributed Systems Engineer" should be kept before this
#: category existed.
#:
#: **Embedded systems used to be here and is not any more** — it belongs to the
#: `firmware` category now, which is excluded outright. Backend stays, firmware
#: goes.
_SOFTWARE_SYSTEMS_RE = re.compile(
    r"\bdistributed\s+systems?\b|\bsystems?\s+programm",
    re.I,
)

#: Security, including application and product security.
#:
#: AppSec and Product Security are genuine software-engineering disciplines and
#: are excluded anyway: the reading chosen was "if you don't want security work,
#: AppSec is still security work". `\bsecurity\s+operations\b` catches SecOps
#: without matching an unrelated "operations engineer".
_SECURITY_RE = re.compile(
    rf"\bsecurity\b{_GAP}{_ROLE_NOUN}\b|\bsecurity\s+operations\b"
    r"|\bapplication\s+security\b|\bappsec\b|\bproduct\s+security\b"
    r"|\bcyber\s*security\b|\binfosec\b|\bpenetration\s+test",
    re.I,
)

#: Data analysis, including the engineering-flavoured Analytics Engineer.
#:
#: Note this is NOT a bare `\banalyst\b`: that would also take "Software
#: Developer/Analyst", a software role with an unfortunate title, and every
#: Business/Security/Systems Analyst that the other categories already handle.
_ANALYST_RE = re.compile(r"\bdata\s+analyst\b|\banalytics\s+engineer\b", re.I)

#: Firmware and embedded work.
#:
#: A bare word match on purpose — "Firmware Engineer", "Embedded Software
#: Engineer", "Embedded Developer" and "Embedded Systems Engineer" are all the
#: same kind of work, and the distinction you drew was backend vs firmware
#: rather than one job title vs another. ~750 postings.
_FIRMWARE_RE = re.compile(r"\bfirmware\b|\bembedded\b", re.I)

#: Category name -> matcher. A registry rather than one boolean parameter per
#: category: with six of them, separate flags would mean six booleans threaded
#: through five function signatures and the CLI, and wiring one to only half the
#: call sites is exactly the failure the prefilter/filter split punishes
#: silently.
#:
#: `management` has no regex — it reads the seniority ladder instead, which is
#: why the values are callables rather than patterns.
EXCLUDED_ROLES: dict[str, Any] = {
    "qa": lambda text: bool(_QA_RE.search(text)),
    "management": lambda text: seniority.classify(text) in MANAGEMENT_LEVELS,
    "network": lambda text: bool(_NETWORK_RE.search(text)),
    "systems": lambda text: bool(
        _SYSTEMS_RE.search(text) and not _SOFTWARE_SYSTEMS_RE.search(text)
    ),
    "security": lambda text: bool(_SECURITY_RE.search(text)),
    "analyst": lambda text: bool(_ANALYST_RE.search(text)),
    "firmware": lambda text: bool(_FIRMWARE_RE.search(text)),
}

#: Stable order for the CLI's `choices` and for anything that reports by
#: category, so help text and logs don't reshuffle between runs.
EXCLUDED_ROLE_NAMES: tuple[str, ...] = tuple(EXCLUDED_ROLES)


def is_excluded_role(
    title: str | None,
    allow: Iterable[str] = (),
) -> str | None:
    """Which excluded category this title falls into, or ``None``.

    These are all roles that *are* software work — a QA engineer, a security
    engineer and an engineering manager all qualify under the accept lists.
    They are excluded by preference, and each preference is individually
    reversible via ``allow``. That is why this is kept separate from
    `_NOT_SOFTWARE_TERMS`, which means "not a software job at all": re-admitting
    security roles must not also re-admit mechanical engineers.

    Checked *before* the accept lists by both :func:`is_software_role` and
    :func:`cheap_prefilter`. Those two are independent copies of the role
    decision and the prefilter runs before the expensive detail request, so an
    exclusion applied to only one of them half-works: the posting is downloaded
    and then discarded, or worse, kept.

    Returns the category name so callers can report *why* something was
    dropped without re-testing it.
    """
    text = (title or "").strip()
    if not text:
        return None
    allowed = set(allow)
    for name, matches in EXCLUDED_ROLES.items():
        if name not in allowed and matches(text):
            return name
    return None


def is_software_role(
    title: str | None,
    department: str | None = None,
    *,
    allow_roles: Iterable[str] = (),
) -> bool:
    """True when a posting is a software/engineering role.

    This is an **allowlist**: a title has to name a software discipline. Bare
    "Engineer" or "Developer" is not enough, because that word is shared with
    every other engineering profession and no blocklist can enumerate them all
    — see the note where `_BARE_TECH` used to be.

    Two layers, doing different jobs. `_SOFTWARE_RE` decides what counts as
    software at all. `EXCLUDED_ROLES` then removes the software roles that are
    named but unwanted — QA, management, network, systems, security, analyst,
    firmware. The second layer is not made redundant by the first: `_SOFTWARE_RE`
    accepts "systems engineer", "security engineer", "qa engineer" and
    "engineering manager" by name, so every one of them would pass the allowlist
    on its own. The accept patterns stay so `--include-roles` can restore them.
    """
    text = (title or "").strip()
    if not text:
        return False
    if is_excluded_role(text, allow_roles):
        return False
    if _NOT_SOFTWARE_RE.search(text):
        return False
    if _SOFTWARE_RE.search(text):
        return True
    # Genuinely ambiguous words (scientist, architect, analyst, intern) still
    # need a department to confirm them — on their own they are as likely to be
    # a research biologist or a building architect.
    if department and _GENERIC_TECH.search(text) and _SOFTWARE_DEPTS.search(department):
        return True
    return False


# --- US location detection ------------------------------------------------

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}
_STATE_ABBRS = set(_STATES.values())

_US_COUNTRY = re.compile(
    r"\b(united\s+states(\s+of\s+america)?|u\.?s\.?a\.?|^us$|\busa\b)\b", re.I
)
_CITY_STATE = re.compile(r",\s*([A-Z]{2})\b(?:\s*,?\s*(?:US|USA|United States))?\s*$")
_STATE_ANYWHERE = re.compile(r",\s*([A-Z]{2})\s*(?:,|$|\s+-)")

# Well-known US metros that often appear without a state.
_US_CITIES = {
    "new york", "nyc", "san francisco", "sf", "seattle", "boston", "chicago",
    "los angeles", "austin", "denver", "atlanta", "miami", "dallas", "houston",
    "philadelphia", "phoenix", "san diego", "san jose", "portland", "washington dc",
    "washington, d.c.", "pittsburgh", "detroit", "minneapolis", "nashville",
    "salt lake city", "boulder", "palo alto", "mountain view", "sunnyvale",
    "cupertino", "menlo park", "redmond", "bellevue", "cambridge, ma",
    "brooklyn", "manhattan", "silicon valley", "bay area",
}

# Regions that a posting can be *scoped to*, excluding US candidates. These are
# the only remote listings we reject: "Remote - EMEA" cannot hire in the US,
# whereas a bare "Remote" might.
_NON_US_SCOPE = re.compile(
    r"\b(emea|apac|apj|latam|anz|"
    r"eu\s+(only|based|residents?|citizens?)|"
    r"(only|must)\s+.{0,24}\b(eu|europe|uk|india|canada|australia|singapore|brazil|"
    r"germany|france|spain|poland|ukraine|philippines|mexico|argentina|japan|china)\b|"
    r"(europe|uk|india|canada|australia|latin\s+america|asia|africa)[\s\-–—]*(only|based)|"
    r"work\s+authorization\s+in\s+(?!the\s+us)|"
    r"(?:must\s+)?(?:reside|located|based)\s+in\s+(?!the\s+us|the\s+united\s+states)"
    r"\s*(eu|europe|uk|india|canada|australia|germany|france|brazil|mexico))\b",
    re.I,
)

# Remote scopes broad enough to include US candidates.
_GLOBAL_REMOTE = re.compile(
    r"\b(worldwide|world\s?wide|global|globally|anywhere|any\s+location|"
    r"international|distributed|remote\s*[-–—]?\s*(global|world|anywhere))\b",
    re.I,
)

# Countries and cities that indicate a non-US posting.
_NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ottawa|calgary|"
    r"united\s+kingdom|england|london|manchester|edinburgh|scotland|ireland|dublin|"
    r"germany|berlin|munich|hamburg|france|paris|spain|madrid|barcelona|"
    r"portugal|lisbon|porto|netherlands|amsterdam|belgium|brussels|"
    r"italy|rome|milan|switzerland|zurich|geneva|austria|vienna|"
    r"sweden|stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|"
    r"poland|warsaw|krakow|czech|prague|romania|bucharest|hungary|budapest|"
    r"ukraine|kyiv|kiev|greece|athens|turkey|istanbul|"
    r"india|bangalore|bengaluru|mumbai|delhi|hyderabad|pune|chennai|gurgaon|noida|"
    r"china|beijing|shanghai|shenzhen|hong\s+kong|taiwan|taipei|"
    r"japan|tokyo|osaka|korea|seoul|singapore|malaysia|kuala\s+lumpur|"
    r"indonesia|jakarta|thailand|bangkok|vietnam|hanoi|philippines|manila|"
    r"australia|sydney|melbourne|brisbane|new\s+zealand|auckland|"
    r"brazil|sao\s+paulo|são\s+paulo|argentina|buenos\s+aires|chile|santiago|"
    r"colombia|bogota|mexico|mexico\s+city|guadalajara|peru|lima|uruguay|"
    r"israel|tel\s+aviv|jerusalem|haifa|raanana|uae|dubai|abu\s+dhabi|"
    r"saudi|riyadh|qatar|doha|egypt|cairo|"
    r"south\s+africa|cape\s+town|johannesburg|nigeria|lagos|kenya|nairobi|"
    r"emea|apac|latam|europe|asia)\b",
    re.I,
)

_REMOTE = re.compile(r"\bremote\b|\bwork\s+from\s+home\b|\bwfh\b|\bdistributed\b|\banywhere\b", re.I)
_US_REMOTE = re.compile(
    r"remote[\s,\-–—]*(us|usa|u\.s\.|united\s+states|america|nationwide)|"
    r"(us|usa|u\.s\.|united\s+states)[\s,\-–—]*(based\s+)?remote|"
    r"remote\s*\(\s*(us|usa|united\s+states)",
    re.I,
)


def _has_us_state(text: str) -> bool:
    if _CITY_STATE.search(text) or _STATE_ANYWHERE.search(text):
        match = _CITY_STATE.search(text) or _STATE_ANYWHERE.search(text)
        return bool(match and match.group(1) in _STATE_ABBRS)
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in _STATES)


_US_COUNTRY_CODES = {
    "us", "usa", "united states", "united states of america", "u.s.", "u.s.a.",
}
_UNSCOPED_COUNTRY = {"", "remote", "worldwide", "global", "anywhere", "international"}


def is_us_eligible(
    location: str | None,
    country: str | None = None,
    is_remote: bool | None = None,
) -> bool:
    """Whether a posting could plausibly hire a US-based candidate.

    This is a **can't-rule-it-out** test, not a guarantee. The default is to
    accept: a job is only rejected when the listing gives positive evidence that
    it cannot hire in the US — a named non-US location, or a remote scope
    restricted to another region ("Remote - EMEA", "EU residents only").

    Consequences worth knowing:

    * A bare ``Remote`` is **accepted** — it may well be open to the US.
    * A **missing** location is accepted; an absent field is not evidence.
    * A US signal anywhere in the string wins, so "New York / London" is kept.
    * Anything unrecognized is accepted.
    """
    text = (location or "").strip()
    haystack = f"{text} {country or ''}".strip()

    # 1. Positive US signals win outright, wherever they appear.
    if country and country.strip().lower() in _US_COUNTRY_CODES:
        return True
    if haystack:
        if _US_COUNTRY.search(haystack) or _US_REMOTE.search(haystack):
            return True
        if _has_us_state(text):
            return True
        lowered = text.lower()
        if any(city in lowered for city in _US_CITIES):
            return True

    # 2. A remote scope naming another region excludes US candidates.
    if haystack and _NON_US_SCOPE.search(haystack):
        return False

    # 3. Remote with a global scope can hire anywhere, including the US.
    if haystack and _GLOBAL_REMOTE.search(haystack):
        return True

    # 4. A named non-US place with no countervailing signal.
    if text and _NON_US.search(text):
        return False
    if country:
        code = country.strip().lower()
        if code not in _UNSCOPED_COUNTRY and (len(code) <= 3 or code.isalpha()):
            return False

    # 5. Everything else — bare "Remote", empty, or unrecognized — is accepted.
    return True


#: Retained under the old name so callers and tests can migrate gradually.
is_us_location = is_us_eligible


def parse_us_location(location: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a raw location string into (city, state)."""
    if not location:
        return None, None
    text = location.strip()
    match = _CITY_STATE.search(text) or _STATE_ANYWHERE.search(text)
    if match and match.group(1) in _STATE_ABBRS:
        city = text[: match.start()].strip().strip(",")
        return (city or None), match.group(1)
    lowered = text.lower()
    for name, abbr in _STATES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            city = re.sub(rf"\b{re.escape(name)}\b.*", "", text, flags=re.I)
            return (city.strip().strip(",") or None), abbr
    return None, None


# --- Date window ----------------------------------------------------------

def parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {value!r} (expected YYYY-MM-DD)")


def in_window(job: Job, since: date | None, until: date | None) -> bool:
    """Whether a job's posting date falls inside the requested window.

    Undated jobs are kept only when no window was requested — silently
    including them would misrepresent the period the user asked for.
    """
    posted = job.posted_date
    if posted is None:
        return since is None and until is None
    if since and posted < since:
        return False
    if until and posted > until:
        return False
    return True


def cheap_prefilter(
    title: str | None,
    department: str | None = None,
    location: str | None = None,
    country: str | None = None,
    *,
    allow_roles: Iterable[str] = (),
) -> bool:
    """Decide whether a listing deserves an expensive detail request.

    Never stricter than :func:`apply_filters` — a listing is rejected only when
    the data already in hand is conclusive: a clearly non-software title, or a
    location that :func:`is_us_eligible` already rules out. Everything uncertain
    passes through, because a wasted request costs one HTTP call while a wrongly
    skipped one loses the job permanently.

    ``country`` is accepted so that a structured code can rescue a listing whose
    location string names a non-US city; without it, such a job would be dropped
    here even though the full filters would keep it.

    The role exclusions must be kept in step with :func:`is_software_role`. The
    invariant test between these two functions only catches a prefilter that is
    *stricter* than the full filter, so a missing exclusion here would not fail
    it — it would just quietly download every QA posting before discarding it.
    """
    text = (title or "").strip()
    if not text:
        return True
    if is_excluded_role(text, allow_roles):
        return False
    if _NOT_SOFTWARE_RE.search(text):
        return False
    # `_GENERIC_TECH` alone is enough to pass here, deliberately without the
    # department confirmation `is_software_role` requires of it: a listing often
    # has no department yet at prefilter time, and a wasted request costs one
    # HTTP call while a wrongly skipped one loses the job permanently. That also
    # keeps this function strictly looser than the full filter, which is the
    # contract — the invariant test only catches the opposite mistake.
    if not (_SOFTWARE_RE.search(text) or _GENERIC_TECH.search(text)):
        return False
    # Delegate the location call so the two can never disagree.
    if (location or country) and not is_us_eligible(location, country):
        return False
    return True


def apply_filters(
    jobs: Iterable[Job],
    *,
    since: date | None = None,
    until: date | None = None,
    software_only: bool = True,
    us_only: bool = True,
    allow_roles: Iterable[str] = (),
) -> list[Job]:
    """Apply role, location and date filters, enriching city/state as we go."""
    kept, _ = apply_filters_counted(
        jobs, since=since, until=until,
        software_only=software_only, us_only=us_only,
        allow_roles=allow_roles,
    )
    return kept


@dataclass
class FilterFunnel:
    """Why postings were dropped, so coverage becomes explainable.

    Without this the pipeline discards rows silently, and a question like "why
    did 16 Recruitee boards yield 0 jobs?" needs a debugging session instead of
    a glance at a chart.
    """

    seen: int = 0
    dropped_role: int = 0
    dropped_location: int = 0
    dropped_window: int = 0
    kept: int = 0

    def add(self, other: "FilterFunnel") -> None:
        """Accumulate in place.

        Deliberately a method rather than ``__iadd__``: ``total += part`` inside
        a nested function rebinds the name, which makes Python treat it as a
        local and raises ``UnboundLocalError``. A plain method call mutates the
        enclosing object with no such trap.
        """
        self.seen += other.seen
        self.dropped_role += other.dropped_role
        self.dropped_location += other.dropped_location
        self.dropped_window += other.dropped_window
        self.kept += other.kept

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "dropped_role": self.dropped_role,
            "dropped_location": self.dropped_location,
            "dropped_window": self.dropped_window,
            "kept": self.kept,
        }


def apply_filters_counted(
    jobs: Iterable[Job],
    *,
    since: date | None = None,
    until: date | None = None,
    software_only: bool = True,
    us_only: bool = True,
    allow_roles: Iterable[str] = (),
) -> tuple[list[Job], FilterFunnel]:
    """Filter, and report why each rejected posting was rejected.

    A posting is attributed to the **first** stage that dropped it, so the
    counts sum to ``seen`` and read as a genuine funnel.
    """
    kept: list[Job] = []
    funnel = FilterFunnel()
    for job in jobs:
        funnel.seen += 1
        if software_only and not is_software_role(
            job.title, job.department, allow_roles=allow_roles,
        ):
            funnel.dropped_role += 1
            continue
        if us_only and not is_us_eligible(job.location_raw, job.country, job.is_remote):
            funnel.dropped_location += 1
            continue
        if not in_window(job, since, until):
            funnel.dropped_window += 1
            continue
        if not job.city and not job.state:
            job.city, job.state = parse_us_location(job.location_raw)
        kept.append(job)
    funnel.kept = len(kept)
    return kept, funnel
