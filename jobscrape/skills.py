"""Technologies named in a job description.

A **controlled vocabulary**, matched with word boundaries. Both halves of that
sentence are load-bearing, and the reason is measured rather than assumed.

Why not substring matching
==========================

A prototype without word boundaries, run over 4,000 real descriptions:

=========  ===============  ====================  =========================================
Term       Substring match  Word-boundary match   What the extra hits actually were
=========  ===============  ====================  =========================================
``rust``   1,677            **305**               trustworthiness 315, trust 127,
                                                  trusted 102, trustworthy 102, thrust 23
``scala``  1,543            **137**               scalable 482, scalability 113,
                                                  escalation 27, escalate 17
``go``     1,118            **133** (via golang)  every ordinary use of the word "go"
=========  ===============  ====================  =========================================

That is 82% and 91% false positives — enough that ``rust`` and ``scala`` both
outranked ``java``, which is how the bug was noticed at all. Word boundaries are
therefore not a refinement; without them the column is noise.

Why not open extraction
=======================

Pulling arbitrary capitalised tokens out of a description yields company names,
product names, benefit providers and section headings. A closed vocabulary can
be wrong in only one direction — it misses things — and a miss is recoverable by
adding a term, whereas a false positive silently corrupts every query that
filters on skills.

Ambiguous short names
=====================

Three names cannot be matched as bare words at all:

* **Go** — only via ``golang``, or ``go`` immediately qualified
  (``go developer``, ``in go``). A bare ``\\bgo\\b`` matched 1,118 of 4,000
  descriptions, essentially all of them the ordinary verb.
* **R** — only in an unmistakable statistical-language context
  (``R and Python``, ``in R``, ``R/SAS``). A bare ``\\bR\\b`` matched 441 of
  4,000, nearly all stray capitals from bullet formatting.
* **C** — only in constructions that cannot be an initial (``C/C++``,
  ``C programming``, ``ANSI C``). ``C++`` and ``C#`` are unambiguous and are
  separate entries.

Every entry is checked against the live archive before being added; see
``tests/test_skills.py``, where the rejection cases carry more weight than the
matches.
"""

from __future__ import annotations

import re

#: Descriptions are scanned to this length. The tail of a long posting is legal
#: boilerplate and benefits copy; the tech stack is stated in the body. Also
#: bounds the cost — this runs on every upsert.
MAX_SCAN_CHARS = 12_000

#: ``canonical name -> pattern``. Patterns are matched case-insensitively and
#: every one is anchored so it cannot fire inside a longer word. Aliases are
#: folded into the pattern rather than kept as separate entries, so `k8s` and
#: `kubernetes` both store the single name ``kubernetes``.
#:
#: Ordering here is presentation only — all patterns are tested against every
#: description, and the result is sorted for stability.
VOCABULARY: dict[str, str] = {
    # --- languages -------------------------------------------------------
    "python": r"\bpython\b",
    # `java` must not match `javascript`.
    "java": r"\bjava\b(?!\s*script)",
    "javascript": r"\bjavascript\b|\bes6\b",
    "typescript": r"\btypescript\b",
    # See the module docstring: bare "go" is unusable.
    "go": r"\bgolang\b|\bgo\s+(?:developer|engineer|programming|lang)\b|\bin\s+go\b|\bgo\s*/\s*(?:rust|java|python)\b",
    "rust": r"\brust\b",
    "c": r"\bc\s*/\s*c\+\+|\bansi\s+c\b|\bc\s+programming\b",
    "c++": r"\bc\+\+",
    "c#": r"\bc#|\bc\s+sharp\b",
    ".net": r"\.net\b|\bdotnet\b|\basp\.net\b",
    "ruby": r"\bruby\b",
    "php": r"\bphp\b",
    "scala": r"\bscala\b",
    "kotlin": r"\bkotlin\b",
    "swift": r"\bswift\b(?!\s*code)",
    "objective-c": r"\bobjective[\s-]?c\b",
    "perl": r"\bperl\b",
    "r": r"\br\s+and\s+python\b|\bpython\s+and\s+r\b|\bin\s+r\b|\br\s*/\s*(?:sas|python|matlab)\b|\br\s+programming\b",
    "matlab": r"\bmatlab\b",
    "elixir": r"\belixir\b",
    "erlang": r"\berlang\b",
    "haskell": r"\bhaskell\b",
    "clojure": r"\bclojure\b",
    "lua": r"\blua\b",
    "dart": r"\bdart\b",
    "solidity": r"\bsolidity\b",
    "bash": r"\bbash\b|\bshell\s+script",
    "powershell": r"\bpowershell\b",
    "sql": r"\bsql\b",
    "cobol": r"\bcobol\b",
    "assembly": r"\bassembly\s+language\b",
    "vhdl": r"\bvhdl\b|\bverilog\b",

    # --- web & app frameworks -------------------------------------------
    "react": r"\breact\b(?!\s*native)",
    "react native": r"\breact\s*native\b",
    "angular": r"\bangular(?:js)?\b",
    "vue": r"\bvue(?:\.js)?\b",
    "svelte": r"\bsvelte(?:kit)?\b",
    "next.js": r"\bnext\.?js\b",
    "node": r"\bnode\.?js\b|\bnodejs\b",
    "express": r"\bexpress\.?js\b",
    "django": r"\bdjango\b",
    "flask": r"\bflask\b",
    "fastapi": r"\bfastapi\b",
    "rails": r"\bruby\s+on\s+rails\b|\brails\b",
    "spring": r"\bspring\s*(?:boot|framework|mvc)\b|\bspring\b(?=\s+and\b)",
    "laravel": r"\blaravel\b",
    "flutter": r"\bflutter\b",
    "jquery": r"\bjquery\b",
    "tailwind": r"\btailwind(?:\s*css)?\b",
    "graphql": r"\bgraphql\b",
    "grpc": r"\bgrpc\b",
    "rest": r"\brest(?:ful)?\s+api|\bapis?\s+\(rest\)",
    "html/css": r"\bhtml\s*/?\s*css\b|\bhtml5\b|\bcss3\b",

    # --- cloud -----------------------------------------------------------
    "aws": r"\baws\b|\bamazon\s+web\s+services\b",
    "gcp": r"\bgcp\b|\bgoogle\s+cloud\b",
    "azure": r"\bazure\b",
    "lambda": r"\baws\s+lambda\b|\blambda\s+functions?\b",
    "s3": r"\bs3\b",
    "ec2": r"\bec2\b",
    "cloudflare": r"\bcloudflare\b",
    "heroku": r"\bheroku\b",
    "digitalocean": r"\bdigital\s*ocean\b",

    # --- containers, orchestration, infrastructure ------------------------
    "kubernetes": r"\bkubernetes\b|\bk8s\b",
    "docker": r"\bdocker\b",
    "terraform": r"\bterraform\b",
    "ansible": r"\bansible\b",
    "helm": r"\bhelm\b",
    "pulumi": r"\bpulumi\b",
    "jenkins": r"\bjenkins\b",
    "github actions": r"\bgithub\s+actions\b",
    "gitlab ci": r"\bgitlab\s*ci\b",
    "argocd": r"\bargo\s*cd\b",
    "nginx": r"\bnginx\b",
    "linux": r"\blinux\b|\bunix\b",
    "serverless": r"\bserverless\b",

    # --- datastores -------------------------------------------------------
    "postgres": r"\bpostgres(?:ql)?\b",
    "mysql": r"\bmysql\b|\bmariadb\b",
    "mongodb": r"\bmongo\s?db\b",
    "redis": r"\bredis\b",
    "elasticsearch": r"\belastic\s?search\b|\bopensearch\b",
    "dynamodb": r"\bdynamo\s?db\b",
    "cassandra": r"\bcassandra\b",
    "sqlserver": r"\bsql\s+server\b|\bt-sql\b",
    "oracle db": r"\boracle\s+(?:database|db)\b|\bpl/sql\b",
    "snowflake": r"\bsnowflake\b",
    "bigquery": r"\bbig\s?query\b",
    "redshift": r"\bredshift\b",
    "databricks": r"\bdatabricks\b",
    "neo4j": r"\bneo4j\b|\bgraph\s+database\b",
    "clickhouse": r"\bclickhouse\b",

    # --- streaming & data engineering -------------------------------------
    "kafka": r"\bkafka\b",
    # "spark innovation" / "spark joy" are ordinary verbs and accounted for 38
    # of the top 100 bare-`spark` matches, so the product needs naming: either
    # explicitly, or by sitting in a list beside other data-stack tools.
    "spark": (
        r"\bapache\s+spark\b|\bpyspark\b|\bspark\s+(?:streaming|sql|structured|jobs?|clusters?)\b"
        r"|\b(?:kafka|hadoop|flink|hive|databricks|scala|emr)\b[^.]{0,60}?\bspark\b"
        r"|\bspark\b[^.]{0,60}?\b(?:kafka|hadoop|flink|hive|databricks|emr)\b"
    ),
    "airflow": r"\bairflow\b",
    "dbt": r"\bdbt\b",
    "flink": r"\bflink\b",
    "hadoop": r"\bhadoop\b",
    "rabbitmq": r"\brabbit\s?mq\b",
    "etl": r"\betl\b|\belt\s+pipelines?\b",
    "pandas": r"\bpandas\b",
    "numpy": r"\bnumpy\b",

    # --- ML / AI ----------------------------------------------------------
    "pytorch": r"\bpytorch\b",
    "tensorflow": r"\btensorflow\b",
    "scikit-learn": r"\bscikit[\s-]?learn\b|\bsklearn\b",
    "keras": r"\bkeras\b",
    "hugging face": r"\bhugging\s*face\b|\btransformers\s+library\b",
    "llm": r"\bllms?\b|\blarge\s+language\s+models?\b",
    "nlp": r"\bnlp\b|\bnatural\s+language\s+processing\b",
    "computer vision": r"\bcomputer\s+vision\b|\bopencv\b",
    "rag": r"\brag\b|\bretrieval[\s-]augmented\b",
    "langchain": r"\blangchain\b",
    "mlops": r"\bmlops\b",
    "deep learning": r"\bdeep\s+learning\b",
    "machine learning": r"\bmachine\s+learning\b",

    # --- mobile -----------------------------------------------------------
    "ios": r"\bios\b(?!\s*co)",
    "android": r"\bandroid\b",
    "swiftui": r"\bswift\s?ui\b",
    "jetpack compose": r"\bjetpack\s+compose\b",

    # --- practices & tooling ----------------------------------------------
    "git": r"\bgit\b(?!hub|lab)",
    "ci/cd": r"\bci\s*/\s*cd\b|\bcontinuous\s+(?:integration|delivery|deployment)\b",
    "agile": r"\bagile\b|\bscrum\b",
    "microservices": r"\bmicro\s?services\b",
    "tdd": r"\btdd\b|\btest[\s-]driven\b",
    "distributed systems": r"\bdistributed\s+systems?\b",
    "observability": r"\bobservability\b|\bdatadog\b|\bprometheus\b|\bgrafana\b",
    "security": r"\bappsec\b|\bapplication\s+security\b|\bpenetration\s+testing\b|\bowasp\b",
    "selenium": r"\bselenium\b|\bcypress\b|\bplaywright\b",
    "jira": r"\bjira\b",
    "figma": r"\bfigma\b",
    "salesforce": r"\bsalesforce\b|\bapex\b",
    "sap": r"\bsap\b|\babap\b",
    "tableau": r"\btableau\b",
    "power bi": r"\bpower\s?bi\b",
    # "a culture where everyone can excel" / "opportunity to excel" — the verb
    # was 132 of the top 141 bare-`excel` matches, easily the worst offender in
    # the vocabulary. The spreadsheet has to be named as a product.
    "excel": (
        r"\bmicrosoft\s+excel\b|\bms\s+excel\b|\bvba\b"
        r"|\bexcel\s+(?:spreadsheets?|models?|macros?|formulas?|pivot|skills)\b"
        r"|\badvanced\s+excel\b|\bexcel\s*\(|\bexcel\s*[,/]\s*(?:word|powerpoint|access|sql)\b"
    ),
}

_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in VOCABULARY.items())


def extract(description: str | None) -> list[str]:
    """Return the canonical names of every technology the description mentions.

    Sorted, so the stored value is stable across runs and two identical
    descriptions always produce the same string — which matters because this
    feeds a column compared on equality.
    """
    if not description:
        return []
    text = description[:MAX_SCAN_CHARS]
    return sorted({name for name, pattern in _COMPILED if pattern.search(text)})


def to_field(description: str | None) -> str:
    """Extract and render for storage: a comma-separated string.

    Comma-separated ``TEXT`` rather than an array column, matching how
    ``scam_signals`` is stored — it needs no dialect-specific type, and a
    ``LIKE '%,python,%'`` style filter works everywhere.
    """
    return ",".join(extract(description))


def parse_field(value: str | None) -> list[str]:
    """Read a stored ``skills`` column back into a list."""
    return [part for part in (value or "").split(",") if part]
