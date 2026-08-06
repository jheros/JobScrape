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

from . import config, seniority
from .models import Job, infer_country, slugify

# --- Software role classification ----------------------------------------

# Unambiguous software titles. Matching any of these is sufficient.
_SOFTWARE_TERMS = [
    r"software\s+(engineer|developer|architect|dev)",
    r"\bsde\b", r"\bswe\b", r"\bsdet\b",
    # A role noun is REQUIRED beside it — the same rule a bare `architect` gets
    # further down, and for the same reason. "Front end" is a software tier only
    # inside a software title; everywhere else it is the front of a shop and the
    # back of a production line. Measured on 52,089 open postings, the bare form
    # was the sole thing admitting 492 of them: "Pharmacy Technician Back End"
    # (111, CVS), "Teammate Endzone & Loyalty (Front End)" (66, a sporting-goods
    # retailer), "FRONT END/CASHIER", "Front End Team Leader", "Machine Operator
    # US - Back End Days", "Behavioral Health Specialist (FT, Back End Nights)".
    #
    # None of that was visible while the sweep only reached tech companies'
    # boards; it arrived with the retailers, grocers and hospital systems that
    # came in when board coverage went from ~17,000 live boards to 32,910.
    #
    # THE SPELLING IS THE DISCRIMINATOR, and it was worth measuring rather than
    # assuming. Across 52,205 open postings, the closed-up form `frontend` /
    # `backend` carries a retail or shift marker in 26 of 4,066 titles (1%,
    # and two of those 26 are software anyway); the spaced form does in 195 of
    # 1,253 (16%). Software writes "Backend Engineer"; a supermarket writes
    # "Front End Team Leader". So the closed-up form still qualifies alone, and
    # only the spaced form has to earn it.
    # `(?<![a-z0-9])` rather than `\b` on the left: an underscore IS a word
    # character, so `\b` refuses to fire in ".NET Developer_backend".
    r"(?<![a-z0-9])(frontend|backend|fullstack)\b",
    # Up to three intervening words, because the stack usually sits between:
    # "Back-End Java Spring Boot Engineer" is a real posting this dropped when
    # the noun had to be adjacent.
    # The separator class covers slashes and commas, not just space and hyphen:
    # "Front End/ CSS/ HTML /Email Developer" is a real posting that stacks the
    # whole stack between the tier and the noun.
    r"(back|front|full)[\s-]end[\s,:/–—-]*(?:\w+[\s,:/–—-]+){0,3}"
    r"(engineer|develop|dev\b|programmer|architect|web|software|technolog)",
    # And the noun-first spelling. Brackets are in the separator class because
    # a title routinely parenthesises the tier: "Mid-Level Everything Engineer
    # (Front End)".
    r"(engineer|developer|architect|programmer)[\s,:/()\[\]–—-]+\s*"
    r"(?:\w+[\s,:/()\[\]–—-]+){0,2}(back|front|full)[\s-]end",
    r"full[\s-]?stack",
    r"\bweb\s+developer\b", r"\bprogrammer\b",
    r"\bmobile\s+(engineer|developer)", r"\b(ios|android)\s+(engineer|developer)",
    r"\b(devops|sre|site\s+reliability)\b",
    r"\bplatform\s+engineer", r"\binfrastructure\s+engineer", r"\bcloud\s+engineer",
    # "science" as well as "scientist": "Data Science Intern" and "Data Science
    # Manager" name the field rather than the person, and without it
    # `--include-roles early_career` could not restore the intern postings it
    # excludes — the half-working-flag shape.
    r"\bdata\s+(engineer|scientist|science)", r"\banalytics\s+engineer",
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
    # Same family as "Solutions Architect": strategy and governance rather than
    # building anything. "enterprise" is in the architect qualifier list in
    # `_SOFTWARE_TERMS`, so this has to be blocked rather than un-accepted.
    r"\b(enterprise|business|information)\s+architect\b",
    # Vendor-platform work routing around the removed enterprise-stack accept.
    # "Salesforce Technical Architect" and "ServiceNow Senior Tech Lead" match
    # `technical architect` / `tech lead`, neither of which asks for a software
    # qualifier, so dropping the vendor accept alone did not remove them.
    # Two lookaheads from the start rather than one after the vendor name: the
    # role word sits on either side in real titles ("Salesforce Technical
    # Architect" but also "Senior Technical Consultant - Salesforce DevOps"),
    # and a trailing-only lookahead silently missed the second shape.
    r"^(?=.*\b(salesforce|servicenow|sap|abap|siebel|sharepoint|peoplesoft|"
    r"workday|netsuite|dynamics\s+365)\b)"
    r"(?=.*\b(architect|tech(nical)?\s+lead|consultant)\b)",
    # Salesforce additionally blocks on engineer/developer, which you named
    # directly. "Salesforce Platform Engineer", "Salesforce DevOps Engineer" and
    # "Software Engineer - Salesforce" are all Salesforce engineers; the vendor
    # accept was removed but they came back via `platform engineer`, `devops`
    # and `software engineer`. Deliberately not extended to SAP and Workday:
    # "Lead Software Engineer, Workday Finance Integrations" is ordinary backend
    # work at a normal company, and only Salesforce was named.
    #
    # Two lookaheads again, since "Salesforce" sits on either side. This leaves
    # "Research Scientist - Salesforce AI Research" alone, which is right —
    # there Salesforce is the employer, not the platform.
    r"^(?=.*\bsalesforce\b)(?=.*\b(engineer(ing)?|developer)\b)",
    # Bootcamp and body-shop adverts, not job postings. `plac\w+` rather than a
    # spelled-out "placement" because these listings are typo-ridden — one in
    # the archive reads "training and placemt".
    r"\btraining\s+and\s+plac\w+\b|\bplacement\s+program\b",
    # Pre-sales in any role noun. `pre-sales engineer` was already blocked above;
    # the architect spelling was not, so "Technical Architect (Pre-Sales)" and
    # "Pre-Sales Data and AI Architect" came through. This also outranks the
    # `forward deployed engineer` accept for "Forward Deployed Engineer |
    # Pre-sales", which is the correct precedence — the work is pre-sales.
    r"\bpre[\s-]?sales\b", r"\bpartner\s+(technical\s+)?architect\b",
    # Two or more seniority words run together with no separator
    # ("SeniorStaffPrincipal Software Consultant", "PrincipalStaff Test
    # Consultant"). Not a title anyone writes; it is a churned-listing tell.
    r"\b(?:senior|staff|principal|lead|junior|associate|sr|jr)"
    r"(?:senior|staff|principal|lead|junior|associate)+\b",
    # Machining and IT-clerical senses of "programmer", which the bare
    # `\bprogrammer\b` accept admits. Deliberately narrow: games and research use
    # "Programmer" as the ordinary word for a software engineer, so "Engine
    # Programmer", "Sound Programmer" and "C++ Programmer" must survive.
    # Two words of slack: "Instrumentation and Controls Programmer" and
    # "Laser and Tool Room Programmer" are the real shapes, and requiring
    # adjacency here would repeat the very bug this round is fixing elsewhere.
    # `cam` is computer-aided manufacturing and `av` is audio-visual system
    # programming (Crestron, AMX) — both are "programmer" in a sense that has
    # nothing to do with software engineering.
    #
    # The separator class allows a slash: "Senior CNC Machinist/Programmer" was
    # written with one and slipped straight through a rule that demanded a
    # space. That is the third time this week a separator assumption has cost
    # real rows, in three different patterns.
    r"\b(cnc|cmm|nc|cam|av|machine\s+tool|laser|mill|lathe|tool\s+room|metering|"
    r"metrology|instrumentation|machinist)\b(?:[\s/&-]+\w+){0,2}[\s/&-]+programmer\b",
    # Clinical-trial biostatistics. A distinct profession that writes SAS, not
    # a software engineering role — and the same employers the wet-lab rule
    # already covers.
    r"\b(statistical|clinical|sas|biostatistic\w*)\s+programmer\b",
    r"\bprogrammer\s+analyst\b",
    # Recruiting / admin roles that mention engineering.
    r"\bengineer(ing)?\s+(technician|technologist|aide|assistant|clerk|"
    r"intern(ship)?\s+program\s+coordinator|manager\s+of\s+facilities)",
    r"\b(engineering|technical|software)\s+recruiter\b",
    # Teaching. A teaching job is a teaching job whoever employs you, which is
    # why bootcamps are here alongside universities: "Software Engineer
    # Instructor" (General Assembly), "Lead Instructor: GenAI" (Correlation
    # One) and "Adjunct Faculty (Data Science and AI)" are the same work. 107
    # postings, and they reached the accept list through `data science` and
    # `software engineer` sitting in the subject they teach.
    r"\b(professor|adjunct|faculty|lecturer|postdoc|instructor|teaching\s+assistant)\b",
    # On-site hardware repair. Spelled WITH the role noun so "Field Service
    # Software Engineer" — which writes software *for* a field-service
    # operation — survives, while "Field Service Engineer - Airports Security &
    # Audio Systems" does not.
    r"\bfield\s+service\s+(engineer|technician|specialist|representative)\b",
    # The phrase only. A bare `\bbd\b` is two letters shared with Becton
    # Dickinson and half a dozen other things, and would be a standing
    # liability for zero present benefit — the archive has no such row today.
    r"\bbusiness\s+development\b",
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
#: `programmer` is here so `_SYSTEMS_RE` and `_NETWORK_RE` can see "Systems
#: Programmer" and "Network Programmer" at all — without it the categories
#: covered every other role noun and silently skipped that one.
_ROLE_NOUN = (
    r"(?:engineer|architect|administrator|analyst|specialist|developer|programmer)"
)

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
#: Up to two words between a qualifier and the role noun. Tokens may carry
#: brackets — "Finite Element (FEA) Research Engineer" puts a parenthesised
#: acronym in the gap, and counting only `\w+` broke the chain there.
_GAP = r"(?:\s+[\w()\[\]/-]+){0,2}\s+"

#: The same idea for the HARDWARE rules alone, where the gap may contain an
#: ampersand, slash or comma: "Injection Molding & Tooling Engineer",
#: "Mechanical, Materials, Chemical & Robotics Engineer".
#:
#: Deliberately NOT `_GAP` itself. That one is shared with `_NETWORK_RE` and
#: `_SYSTEMS_RE`, and widening it there made the `systems` category swallow
#: "Radar Systems/Software Engineer" — a software job, caught by a rule this
#: change was not trying to touch.
_HW_GAP = r"(?:[\s,&/]+[\w()\[\]/-]+){0,2}[\s,&/]+"

#: The same adjacency fix, applied to the *accept* side.
#:
#: Nearly every pattern in `_SOFTWARE_TERMS` requires its qualifier to touch the
#: role noun. That was survivable while a bare "Engineer" qualified on its own —
#: the broad rule caught whatever the specific ones missed. Once the filter
#: became an allowlist it stopped being survivable, and the first purge dry-run
#: showed it plainly: "iOS Frameworks Engineer", "Machine Learning Video
#: Processing Engineer", "Senior Software Advanced Developer" and "Senior API
#: Developer" were all headed for deletion, purely because a word sat in the
#: middle.
#:
#: Only qualifiers that an intervening word cannot turn non-software are listed.
#: "design" and "systems" are deliberately absent — those are exactly the words
#: where the middle of the title decides the discipline. "data" is absent too:
#: with a gap it matches "Data Center Engineer", which is facilities.
_STRONG_QUALIFIER = (
    r"software|machine\s+learning|deep\s+learning|artificial\s+intelligence|"
    r"\bml\b|\bai\b|\bllm\b|\bnlp\b|\bios\b|android|mobile|"
    r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|devops|cloud|\bapi\b|\bweb\b|"
    r"compiler|inference|distributed|kubernetes"
)
#: Like `_GAP`, but the intervening tokens may carry punctuation. Real titles
#: are full of it — "Artificial Intelligence (AI) Engineer" was rejected because
#: `_GAP` only crosses word characters and could not get past the brackets.
#: Deliberately used on the accept side only: a looser gap here can at worst keep
#: a job that should have gone, while the same looseness on the exclusion side
#: would let a category word bind across a punctuation boundary it should not.
_QUALIFIER_GAP = r"(?:\W+\w+){0,2}\W+"

_QUALIFIED_SOFTWARE_RE = re.compile(
    rf"(?:{_STRONG_QUALIFIER}){_QUALIFIER_GAP}(?:engineer|developer|programmer)\b",
    re.I,
)

#: And the same adjacency fix on the *blocklist*, which never got it.
#:
#: `_NOT_SOFTWARE_TERMS` spells its disciplines as `(mechanical|...)\s+engineer`,
#: so it catches "Mechanical Engineer" and misses "Mechanical Reliability
#: Engineer". Once the allowlist began accepting generic role nouns on their own
#: — `integration engineer`, `reliability engineer`, `performance engineer` —
#: that gap became the way hardware got in. 85 postings: "Hardware Integration
#: Engineer", "Senior VLSI Integration Engineer", "SoC Silicon Reliability
#: Engineer", "IC Packaging Integration Engineer".
#:
#: A curated subset of the disciplines, not the whole list. `_NOT_SOFTWARE_TERMS`
#: also names "validation", "process", "distribution", "energy" and "field",
#: which an intervening word genuinely can make software ("Content Distribution
#: Engineer", "Process Automation Engineer") — those keep requiring adjacency.
#: Listed here are only the physical disciplines where no middle word can change
#: the answer.
_HARDWARE_DISCIPLINE = (
    r"mechanical|civil|structural|electrical|chemicals?|aerospace|petroleum|"
    r"mining|nuclear|biomedical|geotechnical|hardware|optical|photonic|"
    r"packaging|materials|thermal|acoustic|automotive|marine|agricultural|"
    r"metallurgical|metallurgy|foundry|boiler|elevator|refrigeration|pipeline|"
    r"drilling|subsea|hydraulic|pneumatic|corrosion|ceramics|polymer|"
    r"asic|fpga|rtl|vlsi|silicon|semiconductor|analog|\bpcb\b|\brf\b|"
    # Added with the reversed rule below. `circuit`, `sram` and `soc` are chip
    # work under any spelling; `facilities` is building maintenance, which is
    # where "Mobile Engineer - Multi-Site Facilities" comes from — the accept
    # list's bare `mobile engineer` reads that as an app developer.
    # `soc` is deliberately absent, though it is a chip word: it is also the
    # Security Operations Centre, and "Senior Backend Engineer, SOC & Threat
    # Hunting" is a real posting it cost. The `_SILICON` words below cover the
    # chip titles without that ambiguity.
    r"circuit|sram|design\s+verification|physical\s+design|facilities|"
    # Semiconductor metrology and lithography — "Electron Beam Product
    # Engineer" (KLA) reaches the accept list through the bare `product
    # engineer` term.
    r"electron\s+beam|e-beam|ebeam|"
    # Added by the audit. Each was measured against the archive first, and the
    # `_names_software` exemption keeps the software ones: LeoLabs' "Radar
    # Software Engineer" and Applied Materials' "Electron Optics Devices
    # Software Engineer" both survive.
    #
    # `optics` is the NOUN — only the adjective `optical` was here, which is
    # why "Optics Research Scientist" walked through.
    #
    # `catalyst` carries a future hazard worth naming: it is also a Cisco
    # product line. It matches one row today (a chemistry simulation role) and
    # a "Catalyst Network Engineer" would already be excluded by the `network`
    # category, so the exposure is small but real.
    r"radar|optics|dram|wafer|lithograph\w*|molding|moulding|"
    r"fea\b|finite\s+element|catalyst"
)

#: The subset safe to match AFTER the role noun — a curated list again, and for
#: a sharper reason than the forward rule's.
#:
#: Reading backwards from the noun crosses whatever the title puts in the
#: trailing clause, and that clause is where product names live. Five words in
#: `_HARDWARE_DISCIPLINE` are also software: **foundry** (Palantir Foundry),
#: **polymer** (Polymer JS), **materials** (Material UI), **packaging**
#: (software packaging) and **pipeline** (data pipelines). Each one cost a real
#: posting when the whole list was used here — "Java Developer - with Polymer JS
#: Experience" and "Data / Cloud Native Engineer - Palantir Foundry Experience"
#: are the two the archive caught.
_HARDWARE_REVERSED = (
    r"mechanical|civil|structural|electrical|chemicals?|aerospace|petroleum|"
    r"mining|nuclear|geotechnical|hardware|photonic|metallurgical|metallurgy|"
    r"boiler|elevator|refrigeration|drilling|subsea|hydraulic|pneumatic|"
    r"corrosion|ceramics|circuit|sram|design\s+verification|physical\s+design|"
    r"facilities|asic|fpga|rtl|vlsi|silicon|semiconductor|radar|optics|dram|"
    r"wafer|lithograph\w*|molding|moulding|catalyst|finite\s+element|fea\b"
)

#: Disciplines so unambiguous that no distance limit applies — see the reversed
#: rule below. A software title never contains these words except beside
#: "software" itself, which `_names_software` already rescues.
#:
#: `soc` is deliberately NOT here, though it is a chip word. At any distance it
#: also matches the security sense — "Senior Backend Engineer, SOC & Threat
#: Hunting" is a Security Operations Centre, and blocking it cost a real
#: posting. It stays in `_HARDWARE_DISCIPLINE` above, where adjacency to the
#: role noun disambiguates it.
_SILICON = (
    r"asic|fpga|rtl|vlsi|silicon|semiconductor|sram|design\s+verification|"
    r"radar|dram|wafer|molding|moulding|lithograph\w*"
)

_HARDWARE_NEAR_RE = re.compile(
    # `scientist` is in the role-noun set because bench research in a hardware
    # discipline is the same job as engineering in it: "Optics Research
    # Scientist" survived only because this list named engineers and not
    # scientists.
    rf"\b(?:{_HARDWARE_DISCIPLINE})\b{_HW_GAP}(?:engineer(?:ing)?|developer|scientist)\b"
    # The three role nouns named as unwanted. Blocked wherever they appear
    # rather than merely left out of the accept list, because the accept side's
    # qualifier gap binds them to a software word two positions away: "Senior AI
    # Chip Design Engineer", "SoC Machine Learning Design Engineer" and "Mobile
    # Device Engineer" all qualified on `ai` / `machine learning` / `mobile`
    # while being chip design and device management. 34 postings.
    rf"|\b(?:design|simulation|device)\s+(?:engineer|developer)\b"
    # THE REVERSED SHAPE. Everything above reads left to right — discipline,
    # then role noun — so it catches "ASIC Design Engineer" and sails past
    # "Principal Engineer, ASIC Design", which is the same job written the other
    # way round. 109 open postings were getting in through that: "Principal
    # Engineer, Design Verification" (Analog Devices), "Principal Engineer, ASIC
    # Design" (Micron), "Staff Engineer, SRAM Circuit Design" (Samsung
    # Semiconductor), "Principal/Sr. Principal Engineer - Structural Analysis".
    #
    # They qualify on the accept side's bare `(staff|principal|distinguished)
    # \s+(engineer|developer)` rule, which asks for seniority and a role noun and
    # nothing about the discipline at all.
    # The silicon subset at ANY distance, because chip titles stack qualifiers:
    # "ASIC Design and Integration Engineer" puts three words between the
    # discipline and the noun, one more than `_GAP` allows.
    # `lead` and `manager` are in the role-noun set for the silicon arm only,
    # because chip titles use them where software would say "engineer": "ASIC
    # DFT Emulation Technical Lead", "Senior Custom ASIC Engineering Lead",
    # "RTL Design Tech Lead (ASIC/SoC)". Safe here and nowhere else — a silicon
    # word has to be present, and the exemption still rescues "Lead Software
    # Engineer - C++, FPGA".
    rf"|\b(?:{_SILICON})\b.*\b(?:engineer(?:ing)?|developer|architect|lead|manager)\b",
    re.I,
)

#: The reversed shape — discipline AFTER the role noun — held separately
#: because it needs a wider exemption than `_names_software`.
#:
#: Reading backwards from the noun crosses the trailing clause, and that clause
#: is as often the *domain served* as the discipline practised. "Frontend
#: Engineer (Civil Aviation)" is front-end work in aviation and "Forward
#: Deployed Engineer - Aerospace" is a software role at a software company; both
#: look identical to "Principal Engineer - Structural Analysis" as far as a
#: regex can tell. The one thing separating them is that the first two already
#: NAME a software discipline before the clause starts, so this arm stands down
#: whenever the title does — see `_UNAMBIGUOUS_SOFTWARE_RE`.
#:
#: Brackets stay out of the separator class here, unlike the accept side's
#: mirror of it, for the same reason: a parenthesised clause is a domain.
_HARDWARE_REVERSED_RE = re.compile(
    # Brackets ARE in the separator class. They were excluded when this arm was
    # written, so that "Frontend Engineer (Civil Aviation)" would survive — but
    # `_UNAMBIGUOUS_SOFTWARE_RE` was added afterwards and covers that case on
    # the right grounds: the title names a software role, whatever punctuation
    # follows. Excluding brackets was punctuation standing in for meaning, and
    # it cost "Product Engineer (Chemicals)".
    rf"\b(?:engineer(?:ing)?|developer|architect|scientist)\b[\s,:/()\[\]–—-]+\s*(?:\w+[\s-]+){{0,2}}"
    rf"(?:{_HARDWARE_REVERSED})\b"
    rf"|\b(?:engineer(?:ing)?|developer|architect|lead|manager)\b.*\b(?:{_SILICON})\b",
    re.I,
)

#: Wet-lab work, which reaches the allowlist through the word "Scientist".
#:
#: `\bresearch\s+(engineer|scientist)\b` and `(staff|principal|distinguished)
#: \s+(engineer|developer|scientist)` both accept a bare "Scientist" on
#: seniority alone, with nothing said about the field. That is correct for
#: "Research Scientist" at an AI lab and wrong for "Principal Scientist,
#: Bioassay Methods Development" — and the pharmaceutical companies that post
#: the second kind only entered this archive when board coverage widened.
#:
#: Deliberately narrow: every entry names a laboratory technique or a biological
#: subject, never an industry. "Scientist at a biotech" is not the test —
#: biotechs employ plenty of software engineers, and one of them is the reason
#: the exemption below applies here too ("Robotics Software Engineer — Cell
#: Therapy Automation", AstraZeneca). Measured: 41 open postings, of which that
#: one is the only genuine software title.
_LIFE_SCIENCE_TERMS = [
    r"\bbioassay\b", r"\bradiochem", r"\bchemical\s+biology\b",
    r"\banalytical\s+development\b", r"\b(up|down)stream\s+process",
    r"\bin\s+(vivo|vitro)\b", r"\bassay\s+develop", r"\bclinical\s+variant\b",
    r"\bformulation\b", r"\bmedicinal\s+chem", r"\bimmunolog", r"\btoxicolog",
    r"\bmolecular\s+biolog", r"\b(cell|gene)\s+therapy\b", r"\bcell\s+culture\b",
    r"\bhistolog", r"\bpatholog", r"\bpharmacokinetic", r"\bbiomarker\b",
    r"\bmicrobiolog", r"\bantibod",
    # Health-economics and outcomes research. "Research Scientist, Evidence
    # Synthesis", "Associate Research Scientist, Evidence Generation",
    # "Research Scientist, Health Economic Modeling" — bench-adjacent medical
    # research that reached the accept list through the bare `research
    # scientist` term. 8 postings.
    r"\bevidence\s+(synthesis|generation)\b", r"\bhealth\s+econom",
    r"\bepidemiolog",
]
_LIFE_SCIENCE_RE = re.compile("|".join(_LIFE_SCIENCE_TERMS), re.I)

#: Role nouns that name a software discipline outright, for the exemption above.
#: Narrower than `_SOFTWARE_RE` on purpose — that one accepts a bare "Principal
#: Scientist" on seniority, which is the very thing being filtered here, so
#: reusing it would make the rule a no-op.
_UNAMBIGUOUS_SOFTWARE_RE = re.compile(
    r"\b(data|backend|frontend|full[\s-]?stack|platform|devops|sre|ml|machine\s+learning)"
    r"[\s-]*(engineer|developer)\b"
    r"|\b(back|front)[\s-]end\s+(engineer|developer)\b"
    r"|\b(devops|sre|site\s+reliability)\b"
    # "AI Scientist - Image Analysis & Digital Pathology" is a machine-learning
    # role at a pharmaceutical company, which is exactly the shape this whole
    # rule risks losing.
    r"|\b(ai|ml|machine\s+learning|computer\s+vision)[\s-]+(scientist|researcher)\b"
    r"|\bforward[\s-]?deployed\s+engineer\b"
    r"|\bui\b(\s*/\s*ux)?\s+(engineer|developer)\b"
    r"|\bdata\s+scien(tist|ce)\b"
    r"|\bsoftware\s+(engineer|developer|architect)\b",
    re.I,
)

#: The exemption that makes the rule above safe rather than harmful.
#:
#: Eight of those 85 say "Software Engineer" outright — "Optical Software
#: Engineer", "Senior VLSI CAD Software Engineer", "Silicon Validation Software
#: Engineer", "Analog Mixed-Signal Modeling Software Engineer". They write
#: software for hardware teams, and they are the difference between this rule
#: removing 77 wrong rows and removing 85 including 8 right ones.
#:
#: Only the explicit spelling counts. "Optical Design Engineer" names no software
#: role and stays blocked.
#:
_EXPLICIT_SOFTWARE_RE = re.compile(
    r"\bsoftware\s+(engineer|developer|dev)\b|\bsde\b|\bswe\b",
    re.I,
)
_SOFTWARE_WORD_RE = re.compile(r"\bsoftware\b", re.I)
_HARDWARE_WORD_RE = re.compile(r"\bhardware\b", re.I)


def _names_software(text: str) -> bool:
    """Whether a title claims to be software work, for the exemption above.

    Two ways to qualify. The strict spelling ("Software Engineer", "SDE"), or
    the bare word `software` with no `hardware` anywhere — which is what rescues
    "SAI Software Design Engineer" and "Senior Software Simulation Engineer",
    whose qualifier does not touch the noun, while still dropping "Hardware/
    Software Co-Design Engineer", where the co-design is the hardware half.

    The `hardware` clause is doing real work: without it a single slash in a
    title would be enough to rescue a chip role.
    """
    if _EXPLICIT_SOFTWARE_RE.search(text):
        return True
    return bool(_SOFTWARE_WORD_RE.search(text) and not _HARDWARE_WORD_RE.search(text))

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
#:
#: **Systems programming used to be here too.** The argument for it was that
#: systems programming means OS and compiler work; measured against the archive
#: it means mainframe sysprog, which is administration. It is now part of the
#: `systems` category like every other "Systems X" title, and
#: `--include-roles systems` restores it — `\bsystems?\s+programmer\b` is still
#: in the accept list for exactly that reason.
_SOFTWARE_SYSTEMS_RE = re.compile(
    r"\bdistributed\s+systems?\b",
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

#: Internships, co-ops, apprenticeships and new-grad postings.
#:
#: Word-anchored throughout, and that is the whole difficulty: a bare `intern`
#: substring also matches "internal" and "international", which 13 titles in the
#: archive carry ("Internal Tools Engineer", "International Payments"). ~349
#: postings.
_EARLY_CAREER_RE = re.compile(
    r"\bintern(ship)?s?\b|\bco-?ops?\b|\bapprentice(ship)?\b|\btrainee\b|"
    # Plurals matter: "Software Engineer - 2027 New Grads" and "New College
    # Graduates Welcome" are how three postings escaped the first version.
    r"\bnew\s+grad(uate)?s?\b|\b(university|college)\s+grad(uate)?s?\b|"
    r"\bearly\s+career\b|\bcampus\s+hire\b|"
    # A thesis placement or a working-student contract is not a job opening,
    # which is the same reason interns are here. "GenAI Research Student
    # Worker", "Master's Thesis in the Field of Data Science".
    r"\bstudent\s+(worker|assistant)\b|\bwerkstudent|\bmaster['’]?s\s+thesis\b",
    re.I,
)

#: Flight software and avionics.
#:
#: "Senior Flight Software Engineer" is genuine C++ work, so this is a
#: preference rather than a not-software judgement — which is why it lives here
#: and not in `_NOT_SOFTWARE_TERMS`. It is embedded software on a vehicle, and
#: the same reasoning that excluded `firmware` excludes it. The avionics
#: production and test roles caught alongside are hardware regardless. ~54.
#:
#: `\bflight\s+engineer` is separately in `_NOT_SOFTWARE_TERMS` — that one is a
#: cockpit crew role, a different job that happens to share the words.
_FLIGHT_RE = re.compile(
    r"\bflight\s+(software|control|test|systems?|dynamics)\b|\bavionics\b",
    re.I,
)

#: Architecture roles, of every stripe.
#:
#: A preference and not a judgement, which is why it belongs here: an Architect
#: at a software company writes software, and the accept list takes these on
#: purpose — `(cloud|software|data|platform|security|enterprise|application|
#: infrastructure|devops|integration)\s+architect` is in `_SOFTWARE_TERMS` and
#: stays there, because `--include-roles architect` restoring nothing would be
#: worse than not having the flag.
#:
#: Deliberately the whole word rather than a curated subset. Three architect
#: senses were already excluded through other categories — `Systems Architect`
#: via `systems`, `Network Architect` via `network`, `Solutions Architect` and
#: `Enterprise Architect` via `_NOT_SOFTWARE_TERMS` — so a partial rule here
#: would have left the archive with an arbitrary half of them and no way to
#: describe which half. Measured: 1,342 open postings, led by Data Architect
#: (70), Controls Software Architect (30), Software Architect (29), Cloud
#: Architect (26), AI Architect (19), Technical Architect (17).
_ARCHITECT_RE = re.compile(r"\barchitect\b", re.I)

#: Mainframe and its ecosystem.
#:
#: A preference, and one that REVERSES an earlier decision: `\bmainframe\s+
#: (engineer|developer|programmer)` and `\bcobol\b` are in the accept list on
#: purpose, and they stay there so `--include-roles mainframe` restores these.
#: Mainframe development is software; it is simply not the software wanted here.
#:
#: `db2` AND `z/os` are deliberately absent, both for the same reason and both
#: found by measuring rather than by reasoning. Between them they matched one
#: title three times — "Data Engineer II — DB2 z/OS, Oracle, SQL Server,
#: PostgreSQL, or Netezza" — which is an ordinary data engineer listing every
#: database they might touch. `z/os` contributed nothing else at all: its only
#: other match was already caught by `mainframe`.
#:
#: `jcl` matches nothing today and stays anyway. It is Job Control Language,
#: which exists nowhere but a mainframe, so it cannot become a false positive.
_MAINFRAME_RE = re.compile(r"\b(mainframe|cobol|jcl|ims)\b", re.I)

#: Configuring somebody else's enterprise platform.
#:
#: Not the vendors as EMPLOYERS — that is `BLOCKED_COMPANIES`, a different list.
#: This is the consultancy that implements their product. Every one of the 19
#: Palantir postings measured was Deloitte, Booz Allen, Guidehouse, L3Harris or
#: Endava deploying Foundry for a client; not one was a job at Palantir.
#:
#: `foundry` alone is deliberately ABSENT: it caught NXP's "APAC Foundry
#: Operations Senior Principal Engineer", which is a *semiconductor* foundry.
#: Only the vendor names match, and `epic` is guarded against Epic Games.
_VENDOR_PLATFORM_RE = re.compile(
    r"\b(palantir|peoplesoft|oracle\s+fusion|servicenow|sap|workday)\b"
    r"|\bepic\b(?!\s*games)",
    re.I,
)

#: Industrial control and materials handling — SCADA, sortation lines.
#:
#: `plc` is absent on purpose: "Senior Software Engineer - C# / PLC" is a
#: software job that happens to talk to a controller, and the line between that
#: and "CNC/PLC Production Engineer" is not one a title match can draw.
_INDUSTRIAL_RE = re.compile(r"\b(scada|flexsort|sortation)\b", re.I)

#: Data science named for the industry it serves rather than the work.
#:
#: "Marketing Data Scientist", "Chemical Data Scientist", "Senior Clinical Data
#: Scientist", "Data Scientist, Supply Chain" — 59 postings. The role is data
#: science and the qualifier is the domain, which is why this is a preference
#: rather than a not-software judgement, and why it is reversible.
#:
#: **This one demonstrably costs genuine software work** and was chosen with
#: that stated: "Staff Data Scientist, Marketing Inference" and "Senior Data
#: Scientist / Risk Modeling" are ordinary ML jobs whose qualifier names the
#: industry, exactly as "Data Engineer, Payments" does. The domain list is
#: fixed and deliberately short rather than open-ended, so the blast radius
#: stays the 59 that were measured.
_DOMAIN_DATA_SCIENCE_RE = re.compile(
    r"\bdata\s+scien(tist|ce)\b(?=.*\b(?:marketing|chemical|clinical|epidemiolog\w*|"
    r"biolog\w*|pharma\w*|health|financ(?:e|ial)|risk|insurance|supply\s+chain|retail|sports)\b)"
    r"|\b(?:marketing|chemical|clinical|epidemiolog\w*|biolog\w*|pharma\w*|health|"
    r"financ(?:e|ial)|risk|insurance|supply\s+chain|retail|sports)\b.*\bdata\s+scien(tist|ce)\b",
    re.I,
)

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
    "early_career": lambda text: bool(_EARLY_CAREER_RE.search(text)),
    "flight": lambda text: bool(_FLIGHT_RE.search(text)),
    "architect": lambda text: bool(_ARCHITECT_RE.search(text)),
    "mainframe": lambda text: bool(_MAINFRAME_RE.search(text)),
    "vendor_platform": lambda text: bool(_VENDOR_PLATFORM_RE.search(text)),
    "industrial": lambda text: bool(_INDUSTRIAL_RE.search(text)),
    "domain_data_science": lambda text: bool(_DOMAIN_DATA_SCIENCE_RE.search(text)),
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
    # A hardware discipline a word or two from the role noun, unless the title
    # names a software role — that exemption is what keeps "Optical Software
    # Engineer" while dropping "Optical Design Engineer".
    #
    # All three discipline rules (this, the reversed shape below, and life
    # science) now share the WIDER exemption. They used to differ: this one
    # asked only for the literal word "software", which was fine while its role
    # nouns were engineer and developer. Adding `scientist` broke that — a
    # "Chemical Data Scientist" is a data scientist, and holding it here meant
    # `--include-roles domain_data_science` could not give it back, which makes
    # the flag a promise the filter does not keep.
    if _HARDWARE_NEAR_RE.search(text) and not (
        _names_software(text) or _UNAMBIGUOUS_SOFTWARE_RE.search(text)
    ):
        return False
    # Same rule for wet-lab work, but a WIDER exemption. See _LIFE_SCIENCE_RE:
    # the hardware exemption asks for the word "software", and that is too
    # narrow here because the software roles in this industry are named after
    # their own discipline — "Senior Data Engineer - Digital Pathology" is a
    # data engineer, and losing it to the word "pathology" is the mistake this
    # rule is most likely to make.
    if _LIFE_SCIENCE_RE.search(text) and not (
        _names_software(text) or _UNAMBIGUOUS_SOFTWARE_RE.search(text)
    ):
        return False
    # The reversed hardware shape, with that same wider exemption — see
    # `_HARDWARE_REVERSED_RE` for why it cannot reuse `_names_software` alone.
    if _HARDWARE_REVERSED_RE.search(text) and not (
        _names_software(text) or _UNAMBIGUOUS_SOFTWARE_RE.search(text)
    ):
        return False
    if _SOFTWARE_RE.search(text) or _QUALIFIED_SOFTWARE_RE.search(text):
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

# Well-known US metros that often appear without a state, mapped to the state
# each one implies.
#
# This was a bare set, consulted only by `is_us_eligible` to answer "could this
# be a US job". It now carries the state too, because `parse_us_location`
# needed exactly the same list: it can only resolve a location that names a
# state, and the most common unresolved shapes in the archive are bare city
# names — "San Francisco" (1,257 rows), "Cupertino" (350), "Seattle" (133),
# "Austin" (118). 2,421 rows matched one of these exactly with no state
# attached.
#
# Curated, not a geocoder — the same doctrine as every other dictionary here.
# Ambiguous names are deliberately absent: there is a Portland in Oregon and
# Maine, a Columbus in Ohio and Georgia, so neither implies a state on its own.
# Entries that already carry their state ("cambridge, ma") stay for the
# eligibility check but resolve through the normal state-code path.
_US_CITIES: dict[str, str | None] = {
    "new york": "NY", "nyc": "NY", "brooklyn": "NY", "manhattan": "NY",
    "san francisco": "CA", "sf": "CA", "los angeles": "CA", "san diego": "CA",
    "san jose": "CA", "palo alto": "CA", "mountain view": "CA",
    "sunnyvale": "CA", "cupertino": "CA", "menlo park": "CA",
    "seattle": "WA", "redmond": "WA", "bellevue": "WA",
    "boston": "MA", "chicago": "IL", "austin": "TX", "dallas": "TX",
    "houston": "TX", "denver": "CO", "boulder": "CO", "atlanta": "GA",
    "miami": "FL", "philadelphia": "PA", "pittsburgh": "PA", "phoenix": "AZ",
    "detroit": "MI", "minneapolis": "MN", "nashville": "TN",
    "salt lake city": "UT", "washington dc": "DC", "washington, d.c.": "DC",
    # Named for the eligibility check, but they imply no single state.
    "portland": None, "cambridge, ma": None,
    "silicon valley": None, "bay area": None,
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


def _city_before(text: str, cut: int) -> str | None:
    """Everything before index `cut` (the matched state), cleaned up.

    That leading chunk is usually the city — but not always: Workday in
    particular sometimes orders a location "US, Minnesota, Eagan" (country,
    state, city) rather than the usual "Eagan, Minnesota" — the state name
    sits in the *middle*, so cutting there leaves "US" as the leading chunk,
    a country code, not a city. Same shape for "Remote - Connecticut",
    where cutting leaves "Remote -". Running the leftover through
    `_CITY_NOISE_RE` (already built to strip exactly this kind of
    decoration in the hub-city branch below) turns either case into "" — a
    bare country code or workplace word is a wrong guess, so this returns
    `None` rather than store one.

    Also rejects a leftover with no letters at all — e.g. "09507 - Colorado
    Remote" leaves "09507" (an internal requisition code), which
    `_CITY_NOISE_RE` doesn't catch since it only strips specific words, not
    arbitrary numbers. A number is never a city name either way.
    """
    candidate = _CITY_NOISE_RE.sub("", text[:cut].strip().strip(",")).strip(" ,-–—")
    if not candidate or not any(ch.isalpha() for ch in candidate):
        return None
    return candidate


def parse_us_location(location: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a raw location string into (city, state).

    Three passes, most specific first: a trailing state code, a full state name
    anywhere, then the curated hub-city table for a string that names a city and
    no state at all. Returns ``(None, None)`` when nothing resolves — a bare
    "Remote" or an unlisted city yields nothing rather than a guess.
    """
    if not location:
        return None, None
    text = location.strip()
    match = _CITY_STATE.search(text) or _STATE_ANYWHERE.search(text)
    if match and match.group(1) in _STATE_ABBRS:
        return _city_before(text, match.start()), match.group(1)
    lowered = text.lower()
    for name, abbr in _STATES.items():
        state_match = re.search(rf"\b{re.escape(name)}\b", lowered)
        if state_match:
            return _city_before(text, state_match.start()), abbr
    # No state named anywhere. The string may still be a city we know — the
    # single most common unresolved shape in the archive. Matched on the whole
    # cleaned string, not by containment, so "San Francisco Bay Area" and
    # "Greater Boston" do not silently become the city itself.
    stripped = _CITY_NOISE_RE.sub("", lowered).strip(" ,-–—")
    state = _US_CITIES.get(stripped)
    if state:
        return _CITY_DISPLAY.get(stripped, stripped.title()), state
    return None, None


#: Cities whose canonical name is not their key title-cased. Without this "nyc"
#: would be stored as the city "Nyc".
_CITY_DISPLAY = {
    "nyc": "New York", "sf": "San Francisco",
    "washington dc": "Washington", "washington, d.c.": "Washington",
}


#: Wording that decorates a city name without changing which city it is, so
#: "Remote - Austin" and "San Francisco Office" still resolve. Anchored to the
#: ends of the string: an interior match would turn "Remote" itself into "".
_CITY_NOISE_RE = re.compile(
    r"^(remote|hybrid|onsite|on-site|us|usa)\s*[-–—,]\s*"
    r"|\s*[-–—,]?\s*(remote|hybrid|onsite|on-site|office|hq|headquarters|"
    r"metro|area|us|usa|united states)$",
    re.I,
)


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
    if _HARDWARE_NEAR_RE.search(text) and not _names_software(text):
        return False
    # `_GENERIC_TECH` alone is enough to pass here, deliberately without the
    # department confirmation `is_software_role` requires of it: a listing often
    # has no department yet at prefilter time, and a wasted request costs one
    # HTTP call while a wrongly skipped one loses the job permanently. That also
    # keeps this function strictly looser than the full filter, which is the
    # contract — the invariant test only catches the opposite mistake.
    if not (_SOFTWARE_RE.search(text) or _QUALIFIED_SOFTWARE_RE.search(text)
            or _GENERIC_TECH.search(text)):
        return False
    # Delegate the location call so the two can never disagree.
    if (location or country) and not is_us_eligible(location, country):
        return False
    return True


# --- blocked employers ------------------------------------------------------

#: Companies whose postings are never collected, whatever they are advertising.
#:
#: A preference, like the excluded role categories — nothing is wrong with these
#: postings, they are simply not wanted. Extend it without touching code through
#: `JOBSCRAPE_BLOCKED_COMPANIES` in `.env`, which is appended to this set.
#:
#: Names are written as you would say them and normalised on both sides of the
#: comparison, so "PayPal", "Paypal" and "paypal" are one entry, and
#: "Airbnb, Inc." matches "airbnb" because `slugify` strips legal suffixes.
#:
#: **Matching is EXACT, never a prefix, and that is load-bearing.** The company
#: registry contains `apple-roofing` (58 postings), `appletreedental` (50),
#: `applewoodcenters` (34), `applebank` (20), `applepiecapital`, `appleseed`,
#: `appletreeprep`, a `microsoftc1`, and a board literally called `stripes`.
#: A prefix rule would take every one of them.
BLOCKED_COMPANIES = frozenset({
    "Amazon", "Microsoft", "Apple", "Coinbase", "Airbnb",
    # Both spellings of one name, not a subsidiary of it. `UnitedHealthcare`
    # and `Optum` are separate brands and are deliberately NOT here.
    "UnitedHealth Group", "UnitedHealth",
    "Stripe", "PayPal",
})

#: Strip a company name or board slug to its comparable core.
#:
#: Deliberately the same normalisation as `sources.apify.ats_link.normalise_slug`
#: rather than an import of it: `filters` is imported BY the source adapters, so
#: importing one back would close the loop `filters -> sources -> ingest ->
#: filters`. Two lines of regex is the cheaper price.
_COMPANY_KEY_RE = re.compile(r"[^a-z0-9]+")


def company_key(value: str | None) -> str:
    """Normalise a company name or slug for blocklist comparison."""
    if not value:
        return ""
    # A Workday slug is `tenant|wdN|site`; only the tenant names the company.
    head = str(value).split("|")[0]
    return _COMPANY_KEY_RE.sub("", slugify(head).lower())


def _blocked_keys() -> frozenset[str]:
    """The built-in list plus whatever `.env` adds, normalised."""
    names = set(BLOCKED_COMPANIES) | set(config.BLOCKED_COMPANIES)
    return frozenset(k for k in (company_key(n) for n in names) if k)


def is_blocked_company(value: str | None) -> bool:
    """Whether this company name or board slug is on the blocklist."""
    key = company_key(value)
    return bool(key) and key in _blocked_keys()


#: How much description a posting must carry before its silence means anything.
#:
#: `classify_workplace` returns `unknown` when a posting never states an
#: arrangement anywhere in its text — but "never states one" reads very
#: differently depending on how much text there was. Measured across the
#: `unknown` rows, description length is sharply BIMODAL:
#:
#:     jooble        p25 269   median 273   p75 277     <- snippet
#:     adzuna            500          500       500     <- truncated at exactly 500
#:     cornerstone       331          382     1,472
#:     ukg               452          612       717
#:     greenhouse      3,980        5,506     6,895     <- real description
#:     workday         4,911        6,014     7,248
#:     ashby           3,042        4,158     5,234
#:
#: Snippet sources cluster at 270-700 characters and real descriptions start
#: around 2,300, with nothing in between. 800 sits in that empty valley, and the
#: choice is insensitive across it: moving the cut from 600 to 800 keeps 91 more
#: rows, and 800 to 1,200 keeps 142 more.
#:
#: Set this to 400 and Adzuna disappears entirely — all 3,274 of its unknown
#: rows are exactly 500 characters, and cutting below that punishes them for a
#: truncation their source imposed rather than for anything the employer said.
DESCRIPTION_EVIDENCE_CHARS = 800


def keeps_workplace(workplace: str | None, description_len: int) -> bool:
    """Whether a posting's work arrangement is one worth collecting.

    Three-way, not a set membership, because `unknown` is conditional:

    * ``remote`` — always kept.
    * ``hybrid`` / ``onsite`` — never. These postings said so themselves.
    * ``unknown`` — kept ONLY when the description was too thin to tell. A
      posting carrying 5,000 characters that never mentions an arrangement is
      not advertising remote work; one carrying a 273-character Jooble snippet
      simply has not been asked.

    The cost of the strict half is real and worth stating: a genuinely remote
    job whose full posting forgot to say so is deleted here, and once the
    listing closes it cannot be recovered by refetching. 353 such postings were
    measured before this rule went in — 350 of them one employer whose text
    reads "a 100% distributed setting - Speechify has no office", which names
    no word the classifier knows.
    """
    if workplace == "remote":
        return True
    if workplace in ("hybrid", "onsite"):
        return False
    return description_len < DESCRIPTION_EVIDENCE_CHARS


def apply_filters(
    jobs: Iterable[Job],
    *,
    since: date | None = None,
    until: date | None = None,
    software_only: bool = True,
    us_only: bool = True,
    remote_only: bool = True,
    allow_roles: Iterable[str] = (),
) -> list[Job]:
    """Apply role, location, date and workplace filters, enriching as we go."""
    kept, _ = apply_filters_counted(
        jobs, since=since, until=until,
        software_only=software_only, us_only=us_only, remote_only=remote_only,
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
    dropped_company: int = 0
    dropped_role: int = 0
    dropped_location: int = 0
    dropped_window: int = 0
    dropped_workplace: int = 0
    kept: int = 0

    def add(self, other: "FilterFunnel") -> None:
        """Accumulate in place.

        Deliberately a method rather than ``__iadd__``: ``total += part`` inside
        a nested function rebinds the name, which makes Python treat it as a
        local and raises ``UnboundLocalError``. A plain method call mutates the
        enclosing object with no such trap.
        """
        self.seen += other.seen
        self.dropped_company += other.dropped_company
        self.dropped_role += other.dropped_role
        self.dropped_location += other.dropped_location
        self.dropped_window += other.dropped_window
        self.dropped_workplace += other.dropped_workplace
        self.kept += other.kept

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "dropped_company": self.dropped_company,
            "dropped_role": self.dropped_role,
            "dropped_location": self.dropped_location,
            "dropped_window": self.dropped_window,
            "dropped_workplace": self.dropped_workplace,
            "kept": self.kept,
        }


def apply_filters_counted(
    jobs: Iterable[Job],
    *,
    since: date | None = None,
    until: date | None = None,
    software_only: bool = True,
    us_only: bool = True,
    remote_only: bool = True,
    allow_roles: Iterable[str] = (),
) -> tuple[list[Job], FilterFunnel]:
    """Filter, and report why each rejected posting was rejected.

    A posting is attributed to the **first** stage that dropped it, so the
    counts sum to ``seen`` and read as a genuine funnel. The stages run in the
    order the evidence becomes available — role and location from the listing,
    the date next, and workplace last because it is the only one derived from
    the description.
    """
    kept: list[Job] = []
    funnel = FilterFunnel()
    for job in jobs:
        funnel.seen += 1
        # First, because it is the most specific reason a posting can be dropped
        # and the cheapest to test. Attributing an Amazon posting to "not
        # software" would answer a question nobody asked.
        #
        # Board sources never reach this — a blocked board is skipped before any
        # request (see `cli._fetch_boards`). This is the aggregator path, where
        # the company name is the only thing identifying the employer.
        if is_blocked_company(job.company_slug) or is_blocked_company(job.company):
            funnel.dropped_company += 1
            continue
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
        # Last, because it is the only stage that needs the DESCRIPTION.
        # `workplace_type` is derived in `Job.__post_init__`, and three of the
        # five tiers that decide it read the posting body — so unlike the three
        # stages above, this one cannot save the detail request that fetched it.
        # It keeps the archive to what you want to publish; it does not make a
        # sweep cheaper.
        if remote_only and not keeps_workplace(
            job.workplace_type or "unknown", len(job.description_text or "")
        ):
            funnel.dropped_workplace += 1
            continue
        # Each field filled independently. Requiring BOTH to be blank meant an
        # adapter that supplied only one blocked the other from ever resolving —
        # 1,835 rows carried a state with no city for that reason.
        if not job.city or not job.state:
            city, state = parse_us_location(job.location_raw)
            job.city = job.city or city
            job.state = job.state or state
        if not job.country:
            job.country = infer_country(job.state, job.location_raw)
        kept.append(job)
    funnel.kept = len(kept)
    return kept, funnel
