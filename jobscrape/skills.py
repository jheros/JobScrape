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

Two rules every pattern must follow
===================================

Both are consequences of :func:`extract`'s prefilter (see ``_leading_literals``)
and neither is visible from a pattern on its own, so they are stated here:

1. **Write patterns in lowercase.** The description is lowercased once per call
   and patterns are matched case-sensitively against it. A pattern containing an
   uppercase letter silently never matches.
2. **Start each alternation branch with a literal** where you can. A branch
   beginning with three or more literal characters gets a fast substring
   prefilter; one that does not (``\\bc\\+\\+``, the ``go``/``r``/``c`` patterns)
   is still correct, just slower.

Why the prefilter exists
========================

Extraction runs on **every upsert**, so its cost is paid on every fetch rather
than once. Measured over 1,500 real descriptions with a 133-term vocabulary:

============================================  ===============  =========================
Approach                                      ms/description   Full pass, 56k rows
============================================  ===============  =========================
One compiled pattern per term, ``re.I``       15.8             ~15 min
Lowercase once, no ``re.I``                   13.7             ~13 min
One combined alternation with named groups    44.1             ~41 min  (backtracking)
**Prefilter + pattern**                       **3.4**          **~3 min**
============================================  ===============  =========================

The combined-alternation result is the surprising one and the reason it is
recorded here: it is the obvious optimisation and it is 2.8x *slower*, because
Python's engine backtracks through the alternation on every position.
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
    # =====================================================================
    # Languages, runtimes and data formats
    # =====================================================================
    "python": r"\bpython\b",
    # `java` must not match `javascript`.
    "java": r"\bjava\b(?!\s*script)",
    "javascript": r"\bjavascript\b|\bes6\b|\becmascript\b",
    "typescript": r"\btypescript\b",
    # See the module docstring: bare "go" is unusable — 1,702 of 8,000.
    "go": r"\bgolang\b|\bgo\s+(?:developer|engineer|programming|lang|routines?)\b|\bin\s+go\b|\bgo\s*/\s*(?:rust|java|python)\b",
    "rust": r"\brust\b",
    "c": r"\bc\s*/\s*c\+\+|\bansi\s+c\b|\bc\s+programming\b",
    "c++": r"\bc\+\+",
    "c#": r"\bc#|\bc\s+sharp\b",
    ".net": r"\.net\b|\bdotnet\b|\basp\.net\b",
    "f#": r"\bf#|\bf\s+sharp\b",
    "ruby": r"\bruby\b",
    "php": r"\bphp\b",
    "scala": r"\bscala\b",
    "kotlin": r"\bkotlin\b",
    "swift": r"\bswift\b(?!\s*code)",
    "objective-c": r"\bobjective[\s-]?c\b",
    "perl": r"\bperl\b",
    "r": r"\br\s+and\s+python\b|\bpython\s+and\s+r\b|\bin\s+r\b|\br\s*/\s*(?:sas|python|matlab)\b|\br\s+programming\b",
    "matlab": r"\bmatlab\b",
    "julia": r"\bjulia\s+(?:programming|language)\b|\b(?:python|matlab|r)\b[^.]{0,40}\bjulia\b",
    "elixir": r"\belixir\b",
    "erlang": r"\berlang\b",
    "haskell": r"\bhaskell\b",
    "ocaml": r"\bocaml\b",
    "clojure": r"\bclojure(?:script)?\b",
    "groovy": r"\bgroovy\b",
    "lua": r"\blua\b",
    "dart": r"\bdart\b",
    "solidity": r"\bsolidity\b",
    "bash": r"\bbash\b|\bshell\s+script",
    "powershell": r"\bpowershell\b",
    "sql": r"\bsql\b",
    "t-sql": r"\bt-sql\b|\btransact[\s-]sql\b",
    "pl/sql": r"\bpl\s*/\s*sql\b",
    "cobol": r"\bcobol\b",
    "assembly": r"\bassembly\s+language\b",
    "vhdl": r"\bvhdl\b|\bverilog\b",
    "html/css": r"\bhtml\s*/?\s*css\b|\bhtml5\b|\bcss3\b",
    "sass": r"\bsass\b|\bscss\b",
    # "less experience", "less than" — 384 of 8,000 as a bare word, and the
    # preprocessor is nearly always named beside Sass.
    "less": r"\bless\s*(?:css|preprocessor)\b|\b(?:sass|scss)\s*/\s*less\b|\bless\s*/\s*(?:sass|scss)\b",
    "graphql": r"\bgraphql\b",
    "cypher": r"\bcypher\s+quer|\bneo4j\b[^.]{0,40}\bcypher\b",
    "sparql": r"\bsparql\b",
    "dax": r"\bdax\b",
    "mdx": r"\bmdx\b",
    "yaml": r"\byaml\b",
    "json": r"\bjson\b",
    "xml": r"\bxml\b",
    "protobuf": r"\bprotobuf\b|\bprotocol\s+buffers?\b",
    "avro": r"\bavro\b",
    "parquet": r"\bparquet\b",
    "orc": r"\borc\s+(?:files?|format)\b",
    "webassembly": r"\bwebassembly\b|\bwasm\b",
    "node": r"\bnode\.?js\b|\bnodejs\b",
    "deno": r"\bdeno\b",
    "bun": r"\bbun\s*(?:js|runtime)\b|\b(?:deno|node\.?js)\b[^.]{0,40}\bbun\b",
    "jvm": r"\bjvm\b",

    # =====================================================================
    # Frontend frameworks, libraries and build tooling
    # =====================================================================
    "react": r"\breact\b(?!\s*native)",
    "react native": r"\breact\s*native\b",
    "angular": r"\bangular(?:js)?\b",
    "vue": r"\bvue(?:\.?js)?\b",
    "svelte": r"\bsvelte\b",
    "sveltekit": r"\bsveltekit\b",
    "next.js": r"\bnext\.?js\b",
    "nuxt": r"\bnuxt(?:\.?js)?\b",
    "remix": r"\bremix\s*(?:js|run|framework)\b|\b(?:next\.?js|react)\b[^.]{0,40}\bremix\b",
    "astro": r"\bastro\s*(?:js|build|framework)\b|\b(?:next\.?js|svelte|nuxt)\b[^.]{0,40}\bastro\b",
    "gatsby": r"\bgatsby\b",
    "solidjs": r"\bsolid\.?js\b|\bsolidjs\b",
    "qwik": r"\bqwik\b",
    "preact": r"\bpreact\b",
    "ember": r"\bember(?:\.?js)?\b",
    "backbone": r"\bbackbone(?:\.?js)?\b",
    "alpine.js": r"\balpine\.?js\b",
    "lit": r"\blit\s*(?:element|html)\b|\bweb\s+components?\b[^.]{0,40}\blit\b",
    "stencil": r"\bstencil\s*js\b|\bstenciljs\b",
    "htmx": r"\bhtmx\b",
    "jquery": r"\bjquery\b",
    "rxjs": r"\brxjs\b|\breactive\s+extensions\b",
    "redux": r"\bredux\b",
    "mobx": r"\bmobx\b",
    "zustand": r"\bzustand\b",
    "recoil": r"\brecoil\s*js\b|\brecoiljs\b",
    "jotai": r"\bjotai\b",
    "xstate": r"\bxstate\b",
    "ngrx": r"\bngrx\b",
    "pinia": r"\bpinia\b",
    "tanstack query": r"\btanstack\s+query\b|\breact\s+query\b",
    "apollo": r"\bapollo\s*(?:client|server|graphql)\b|\bapollographql\b",
    "relay": r"\brelay\s+(?:modern|graphql)\b|\bgraphql\b[^.]{0,30}\brelay\b",
    "react router": r"\breact\s+router\b",
    "react hook form": r"\breact\s+hook\s+form\b",
    "formik": r"\bformik\b",
    "zod": r"\bzod\b",
    "yup": r"\byup\s+schema\b|\b(?:zod|joi|formik)\b[^.]{0,30}\byup\b",
    "valibot": r"\bvalibot\b",
    "tailwind": r"\btailwind(?:\s*css)?\b",
    "bootstrap": r"\bbootstrap\b(?!\s*(?:a|the|our))",
    "material ui": r"\bmaterial[\s-]ui\b|\bmui\b",
    "chakra ui": r"\bchakra\s*ui\b",
    "ant design": r"\bant\s+design\b|\bantd\b",
    "radix ui": r"\bradix\s*ui\b",
    "shadcn": r"\bshadcn\b",
    "headless ui": r"\bheadless\s*ui\b",
    "mantine": r"\bmantine\b",
    "semantic ui": r"\bsemantic\s*ui\b",
    "bulma": r"\bbulma\b",
    # "the foundation of our platform", "foundational" — 904 of 8,000 bare.
    "foundation css": r"\bfoundation\s+(?:css|framework|sass)\b|\bzurb\s+foundation\b",
    "styled components": r"\bstyled[\s-]components\b",
    "emotion": r"\bemotion\s*(?:js|css)\b|\bstyled[\s-]components\b[^.]{0,40}\bemotion\b",
    "css modules": r"\bcss\s+modules\b",
    "postcss": r"\bpostcss\b",
    "storybook": r"\bstorybook\b",
    "chromatic": r"\bchromatic\b",
    "webpack": r"\bwebpack\b",
    "vite": r"\bvite\b|\bvitejs\b",
    "vitest": r"\bvitest\b",
    "rollup": r"\brollup\.?js\b|\brollup\s+bundler\b|\b(?:webpack|vite|esbuild|parcel)\b[^.]{0,40}\brollup\b",
    "parcel": r"\bparcel\s+bundler\b|\b(?:webpack|vite|rollup|esbuild)\b[^.]{0,40}\bparcel\b",
    "esbuild": r"\besbuild\b",
    "swc": r"\bswc\b",
    "babel": r"\bbabel\b",
    "turbopack": r"\bturbopack\b",
    "rspack": r"\brspack\b",
    "turborepo": r"\bturborepo\b",
    "nx": r"\bnx\s+(?:monorepo|workspace)\b|\bnrwl\b",
    "lerna": r"\blerna\b",
    "biome": r"\bbiome\s*js\b|\bbiomejs\b",
    "trpc": r"\btrpc\b",
    "gulp": r"\bgulp\.?js\b|\b(?:grunt|webpack|npm)\b[^.]{0,40}\bgulp\b",
    "grunt": r"\bgrunt\.?js\b|\b(?:gulp|webpack|npm)\b[^.]{0,40}\bgrunt\b",
    "npm": r"\bnpm\b",
    "yarn": r"\byarn\b",
    "pnpm": r"\bpnpm\b",
    "bower": r"\bbower\b",
    "d3": r"\bd3\.?js\b|\bd3\b",
    "chart.js": r"\bchart\.?js\b",
    "highcharts": r"\bhighcharts\b",
    "echarts": r"\becharts\b",
    "three.js": r"\bthree\.?js\b",
    "babylon.js": r"\bbabylon\.?js\b",

    # =====================================================================
    # Backend frameworks and libraries
    # =====================================================================
    "express": r"\bexpress\.?js\b",
    "nestjs": r"\bnest\.?js\b|\bnestjs\b",
    "fastify": r"\bfastify\b",
    "koa": r"\bkoa\.?js\b|\b(?:express|hapi|fastify)\b[^.]{0,40}\bkoa\b",
    "hapi": r"\bhapi\.?js\b|\bhapijs\b",
    "adonisjs": r"\badonis\.?js\b|\badonisjs\b",
    "feathersjs": r"\bfeathers\.?js\b|\bfeathersjs\b",
    "loopback": r"\bloopback\b",
    "sails": r"\bsails\.?js\b|\bsailsjs\b",
    "meteor": r"\bmeteor\.?js\b|\bmeteorjs\b",
    "socket.io": r"\bsocket\.?io\b",
    "passport": r"\bpassport\.?js\b",
    "prisma": r"\bprisma\b",
    "typeorm": r"\btypeorm\b",
    "sequelize": r"\bsequelize\b",
    "drizzle": r"\bdrizzle\s*orm\b",
    "mongoose": r"\bmongoose\b",
    "knex": r"\bknex\b",
    "bullmq": r"\bbullmq\b",
    "agenda": r"\bagenda\.?js\b|\bagendajs\b",
    "spring": r"\bspring\s*(?:boot|framework|mvc)\b|\bspring\b(?=\s+and\b)",
    "spring boot": r"\bspring\s*boot\b",
    "spring webflux": r"\bspring\s*webflux\b|\bwebflux\b",
    "spring security": r"\bspring\s*security\b",
    "spring data": r"\bspring\s*data\b",
    "spring cloud": r"\bspring\s*cloud\b",
    "hibernate": r"\bhibernate\b",
    "jakarta ee": r"\bjakarta\s*ee\b|\bjava\s*ee\b|\bj2ee\b",
    "quarkus": r"\bquarkus\b",
    "micronaut": r"\bmicronaut\b",
    "vertx": r"\bvert\.?x\b",
    "dropwizard": r"\bdropwizard\b",
    "play framework": r"\bplay\s+framework\b",
    "akka": r"\bakka\b",
    "maven": r"\bmaven\b",
    "gradle": r"\bgradle\b",
    "asp.net core": r"\basp\.?net\s+core\b",
    "blazor": r"\bblazor\b",
    "entity framework": r"\bentity\s+framework\b|\bef\s+core\b",
    "dapper": r"\bdapper\s+orm\b|\b(?:entity\s+framework|\.net|ef\s+core)\b[^.]{0,40}\bdapper\b",
    "signalr": r"\bsignalr\b",
    "masstransit": r"\bmasstransit\b",
    "mediatr": r"\bmediatr\b",
    "hangfire": r"\bhangfire\b",
    "django": r"\bdjango\b",
    "django rest framework": r"\bdjango\s+rest\s+framework\b|\bdrf\b",
    "flask": r"\bflask\b",
    "fastapi": r"\bfastapi\b",
    "starlette": r"\bstarlette\b",
    "tornado": r"\btornado\s+(?:web|framework|python)\b",
    "sanic": r"\bsanic\b",
    "falcon": r"\bfalcon\s+(?:framework|api)\b",
    "pyramid": r"\bpyramid\s+framework\b",
    "celery": r"\bcelery\b",
    "sqlalchemy": r"\bsqlalchemy\b",
    "pydantic": r"\bpydantic\b",
    "gunicorn": r"\bgunicorn\b",
    "uvicorn": r"\buvicorn\b",
    "rails": r"\bruby\s+on\s+rails\b|\brails\b",
    "sinatra": r"\bsinatra\b",
    "hanami": r"\bhanami\b",
    "sidekiq": r"\bsidekiq\b",
    "laravel": r"\blaravel\b",
    "symfony": r"\bsymfony\b",
    "codeigniter": r"\bcodeigniter\b",
    "cakephp": r"\bcakephp\b",
    "yii": r"\byii\b",
    "slim": r"\bslim\s+framework\b",
    "phoenix": r"\bphoenix\s+(?:framework|liveview)\b|\belixir\b[^.]{0,40}\bphoenix\b",
    "ecto": r"\becto\b",
    "gin": r"\bgin\s+(?:gonic|framework|web)\b|\bgin-gonic\b",
    "echo": r"\becho\s+framework\b|\bgolang\b[^.]{0,30}\becho\b",
    # "fiber optic", "high fiber" — 88 of 8,000 bare, mostly hardware roles.
    "fiber": r"\bfiber\s+framework\b|\bgofiber\b|\bgolang\b[^.]{0,30}\bfiber\b",
    "chi": r"\bchi\s+router\b|\bgo-chi\b",
    "beego": r"\bbeego\b",
    "revel": r"\brevel\s+framework\b",
    "gorm": r"\bgorm\b",
    "actix": r"\bactix\b",
    "axum": r"\baxum\b",
    "rocket": r"\brocket\s+(?:framework|rs)\b|\brust\b[^.]{0,30}\brocket\b",
    "warp": r"\bwarp\s+framework\b|\brust\b[^.]{0,30}\bwarp\b",
    "ktor": r"\bktor\b",
    "vapor": r"\bvapor\s+framework\b|\bswift\b[^.]{0,30}\bvapor\b",

    # =====================================================================
    # Mobile
    # =====================================================================
    "flutter": r"\bflutter\b",
    "android": r"\bandroid\b",
    "ios": r"\bios\b(?!\s*co)",
    "jetpack compose": r"\bjetpack\s+compose\b",
    "android jetpack": r"\bandroid\s+jetpack\b",
    "kotlin coroutines": r"\bkotlin\s+coroutines\b|\bcoroutines\b",
    "kotlin flow": r"\bkotlin\s+flow\b",
    # "room to grow", "conference room" — 193 of 8,000 bare.
    "room": r"\broom\s+(?:database|persistence|dao)\b|\bandroid\b[^.]{0,40}\broom\s+db\b",
    "workmanager": r"\bworkmanager\b",
    "retrofit": r"\bretrofit\b",
    "okhttp": r"\bokhttp\b",
    "dagger": r"\bdagger\s*(?:2|hilt)?\b(?=[^.]{0,40}\b(?:android|kotlin|inject))|\bdagger\s+dependency\b",
    "hilt": r"\bhilt\b",
    "koin": r"\bkoin\b",
    "glide": r"\bglide\s+library\b|\bandroid\b[^.]{0,30}\bglide\b",
    "coil": r"\bcoil\s+library\b|\bkotlin\b[^.]{0,30}\bcoil\b",
    "android studio": r"\bandroid\s+studio\b",
    "firebase": r"\bfirebase\b",
    "swiftui": r"\bswift\s?ui\b",
    "uikit": r"\buikit\b",
    # "combine multiple sources", "combine our" — 301 of 8,000 bare.
    "combine": r"\bcombine\s+framework\b|\bswiftui\b[^.]{0,40}\bcombine\b|\bcombine\b[^.]{0,40}\bswiftui\b",
    "swift concurrency": r"\bswift\s+concurrency\b|\basync\s*/\s*await\b[^.]{0,30}\bswift\b",
    "core data": r"\bcore\s*data\b",
    "core animation": r"\bcore\s*animation\b",
    "mapkit": r"\bmapkit\b",
    "avfoundation": r"\bavfoundation\b",
    "xcode": r"\bxcode\b",
    "swift package manager": r"\bswift\s+package\s+manager\b",
    "cocoapods": r"\bcocoapods\b",
    "carthage": r"\bcarthage\b",
    "testflight": r"\btestflight\b",
    "xctest": r"\bxctest\b|\bxcuitest\b",
    "expo": r"\bexpo\s+(?:go|router|sdk)\b|\breact\s*native\b[^.]{0,40}\bexpo\b",
    "ionic": r"\bionic\s+(?:framework|angular|react)\b|\bionic\b(?=[^.]{0,30}\b(?:capacitor|cordova|hybrid))",
    "capacitor": r"\bcapacitor\s*js\b|\bionic\b[^.]{0,30}\bcapacitor\b",
    "nativescript": r"\bnativescript\b",
    "maui": r"\b\.net\s+maui\b|\bmaui\b",
    "xamarin": r"\bxamarin\b",

    # =====================================================================
    # Datastores, search, cache and vector
    # =====================================================================
    "postgres": r"\bpostgres(?:ql)?\b",
    "mysql": r"\bmysql\b|\bmariadb\b",
    "sqlite": r"\bsqlite\b",
    "sqlserver": r"\bsql\s+server\b|\bmssql\b",
    "oracle db": r"\boracle\s+(?:database|db)\b",
    "db2": r"\bdb2\b",
    "cockroachdb": r"\bcockroach\s?db\b",
    "yugabytedb": r"\byugabyte\s?db\b",
    "tidb": r"\btidb\b",
    "aurora": r"\baurora\s+(?:postgres|mysql|serverless|db|database|cluster)\b|\bamazon\s+aurora\b|\baws\s+aurora\b",
    "spanner": r"\bcloud\s+spanner\b|\bgoogle\s+spanner\b",
    "azure sql": r"\bazure\s+sql\b",
    "snowflake": r"\bsnowflake\b",
    "redshift": r"\bredshift\b",
    "bigquery": r"\bbig\s?query\b",
    "teradata": r"\bteradata\b",
    "sap hana": r"\bsap\s+hana\b|\bhana\s+database\b",
    "clickhouse": r"\bclickhouse\b",
    "duckdb": r"\bduckdb\b",
    "vertica": r"\bvertica\b",
    "greenplum": r"\bgreenplum\b",
    "singlestore": r"\bsinglestore\b|\bmemsql\b",
    "firebird": r"\bfirebird\b",
    "mongodb": r"\bmongo\s?db\b",
    "dynamodb": r"\bdynamo\s?db\b",
    "cassandra": r"\bcassandra\b",
    "scylladb": r"\bscylla\s?db\b",
    "couchbase": r"\bcouchbase\b",
    "couchdb": r"\bcouch\s?db\b",
    "ravendb": r"\braven\s?db\b",
    "arangodb": r"\barango\s?db\b",
    "neo4j": r"\bneo4j\b|\bgraph\s+database\b",
    "janusgraph": r"\bjanusgraph\b",
    "dgraph": r"\bdgraph\b",
    "redis": r"\bredis\b",
    "memcached": r"\bmemcached\b",
    "aerospike": r"\baerospike\b",
    "hazelcast": r"\bhazelcast\b",
    "ignite": r"\bapache\s+ignite\b",
    "elasticsearch": r"\belastic\s?search\b",
    "opensearch": r"\bopensearch\b",
    "solr": r"\bsolr\b",
    "meilisearch": r"\bmeilisearch\b",
    "typesense": r"\btypesense\b",
    "algolia": r"\balgolia\b",
    "influxdb": r"\binflux\s?db\b",
    "timescaledb": r"\btimescale\s?db\b",
    "questdb": r"\bquest\s?db\b",
    "hbase": r"\bhbase\b",
    "bigtable": r"\bbigtable\b",
    "kudu": r"\bapache\s+kudu\b",
    "supabase": r"\bsupabase\b",
    "planetscale": r"\bplanetscale\b",
    "neon": r"\bneon\s+(?:postgres|database|db|serverless)\b",
    "fauna": r"\bfaunadb\b|\bfauna\s+database\b",
    "pinecone": r"\bpinecone\b",
    "weaviate": r"\bweaviate\b",
    "milvus": r"\bmilvus\b",
    "qdrant": r"\bqdrant\b",
    "chroma": r"\bchroma\s?db\b",
    "pgvector": r"\bpgvector\b",
    "vector search": r"\bvector\s+(?:search|database|store|embeddings?)\b",

    # =====================================================================
    # Cloud platforms
    # =====================================================================
    "aws": r"\baws\b|\bamazon\s+web\s+services\b",
    "azure": r"\bazure\b",
    "gcp": r"\bgcp\b|\bgoogle\s+cloud\b",
    "oci": r"\boracle\s+cloud\b|\boci\b",
    "ibm cloud": r"\bibm\s+cloud\b",
    "cloudflare": r"\bcloudflare\b",
    "digitalocean": r"\bdigital\s*ocean\b",
    "heroku": r"\bheroku\b",
    "vercel": r"\bvercel\b",
    "netlify": r"\bnetlify\b",

    # --- AWS services -----------------------------------------------------
    "ec2": r"\bec2\b",
    "lambda": r"\baws\s+lambda\b|\blambda\s+functions?\b|\bserverless\b[^.]{0,30}\blambda\b",
    "s3": r"\bs3\b",
    "ebs": r"\bebs\b",
    "efs": r"\befs\b",
    "rds": r"\brds\b",
    "ecs": r"\becs\b",
    "eks": r"\beks\b",
    "fargate": r"\bfargate\b",
    "app runner": r"\bapp\s+runner\b",
    "api gateway": r"\bapi\s+gateway\b",
    "cloudfront": r"\bcloudfront\b",
    "route 53": r"\broute\s*53\b",
    "elb": r"\belastic\s+load\s+balanc|\balb\b|\bnlb\b",
    "vpc": r"\bvpc\b",
    "transit gateway": r"\btransit\s+gateway\b",
    "privatelink": r"\bprivatelink\b",
    "iam": r"\biam\b",
    "aws organizations": r"\baws\s+organizations\b",
    "control tower": r"\bcontrol\s+tower\b",
    "cloudformation": r"\bcloudformation\b",
    "aws cdk": r"\baws\s+cdk\b|\bcloud\s+development\s+kit\b",
    "systems manager": r"\bsystems\s+manager\b|\bssm\b",
    "secrets manager": r"\bsecrets\s+manager\b",
    "kms": r"\bkms\b",
    "cloudwatch": r"\bcloudwatch\b",
    "cloudtrail": r"\bcloudtrail\b",
    "aws config": r"\baws\s+config\b",
    "sqs": r"\bsqs\b",
    "sns": r"\bsns\b",
    "eventbridge": r"\beventbridge\b",
    "step functions": r"\bstep\s+functions\b",
    "kinesis": r"\bkinesis\b",
    "glue": r"\baws\s+glue\b|\bglue\s+(?:jobs?|catalog|etl)\b",
    "emr": r"\bemr\b",
    "sagemaker": r"\bsagemaker\b",
    "bedrock": r"\bbedrock\b",
    "cognito": r"\bcognito\b",
    "elasticache": r"\belasticache\b",
    "lake formation": r"\blake\s+formation\b",

    # --- Azure services ---------------------------------------------------
    "azure functions": r"\bazure\s+functions\b",
    "azure app service": r"\bazure\s+app\s+service\b",
    "azure container apps": r"\bazure\s+container\s+apps\b",
    "aks": r"\baks\b|\bazure\s+kubernetes\s+service\b",
    "azure blob storage": r"\bazure\s+blob\b|\bblob\s+storage\b",
    "cosmos db": r"\bcosmos\s?db\b",
    "azure api management": r"\bazure\s+api\s+management\b|\bapim\b",
    "azure front door": r"\bfront\s+door\b",
    "azure devops": r"\bazure\s+devops\b|\bazure\s+pipelines\b|\bazure\s+repos\b",
    "entra id": r"\bentra\s+id\b|\bazure\s+ad\b|\bazure\s+active\s+directory\b",
    "azure key vault": r"\bkey\s+vault\b",
    "arm templates": r"\bazure\s+resource\s+manager\b|\barm\s+templates?\b",
    "bicep": r"\bbicep\b",
    "azure monitor": r"\bazure\s+monitor\b|\bapplication\s+insights\b",
    "azure service bus": r"\bservice\s+bus\b",
    "azure event hubs": r"\bevent\s+hubs?\b",
    "azure data factory": r"\bdata\s+factory\b|\badf\b",
    "azure synapse": r"\bsynapse\b",
    "azure machine learning": r"\bazure\s+(?:machine\s+learning|ml)\b",
    "azure openai": r"\bazure\s+openai\b|\bazure\s+ai\s+foundry\b",

    # --- Google Cloud services --------------------------------------------
    "compute engine": r"\bcompute\s+engine\b",
    "cloud run": r"\bcloud\s+run\b",
    "cloud functions": r"\bcloud\s+functions\b",
    "gke": r"\bgke\b|\bgoogle\s+kubernetes\s+engine\b",
    "cloud storage": r"\bgoogle\s+cloud\s+storage\b|\bgcs\b",
    "dataflow": r"\bdataflow\b",
    "dataproc": r"\bdataproc\b",
    "pub/sub": r"\bpub\s*/\s*sub\b|\bpubsub\b",

    # =====================================================================
    # DevOps, platform and infrastructure
    # =====================================================================
    "git": r"\bgit\b(?!hub|lab)",
    "github": r"\bgithub\b",
    "gitlab": r"\bgitlab\b",
    "bitbucket": r"\bbitbucket\b",
    "github actions": r"\bgithub\s+actions\b",
    "gitlab ci": r"\bgitlab\s*ci\b",
    "jenkins": r"\bjenkins\b",
    "circleci": r"\bcircle\s?ci\b",
    "travis ci": r"\btravis\s*ci\b",
    "teamcity": r"\bteamcity\b",
    "bamboo": r"\bbamboo\b",
    "buildkite": r"\bbuildkite\b",
    "argocd": r"\bargo\s*cd\b",
    "argo workflows": r"\bargo\s+workflows\b|\bargo\s+events\b",
    "fluxcd": r"\bflux\s*cd\b",
    "tekton": r"\btekton\b",
    "spinnaker": r"\bspinnaker\b",
    # "harness the power of" — 190 of 8,000 bare.
    "harness": r"\bharness\.io\b|\bharness\s+(?:ci|cd|platform)\b",
    "octopus deploy": r"\boctopus\s+deploy\b",
    "drone ci": r"\bdrone\s+ci\b",
    "concourse": r"\bconcourse\s+ci\b",
    "artifactory": r"\bartifactory\b",
    "nexus": r"\bsonatype\s+nexus\b|\bnexus\s+repository\b",
    "docker": r"\bdocker\b",
    "podman": r"\bpodman\b",
    "buildah": r"\bbuildah\b",
    "containerd": r"\bcontainerd\b",
    "kubernetes": r"\bkubernetes\b|\bk8s\b",
    "openshift": r"\bopenshift\b",
    "rancher": r"\brancher\b",
    "nomad": r"\bnomad\b",
    "helm": r"\bhelm\b",
    "kustomize": r"\bkustomize\b",
    "istio": r"\bistio\b",
    "linkerd": r"\blinkerd\b",
    "consul": r"\bconsul\b",
    "envoy": r"\benvoy\s+proxy\b|\benvoy\b(?=[^.]{0,40}\b(?:istio|service\s+mesh|proxy))",
    "traefik": r"\btraefik\b",
    "nginx": r"\bnginx\b",
    "haproxy": r"\bhaproxy\b",
    "terraform": r"\bterraform\b",
    "opentofu": r"\bopentofu\b",
    "pulumi": r"\bpulumi\b",
    "ansible": r"\bansible\b",
    # "chef" and "puppet" are named beside each other and beside ansible far
    # more often than alone; that co-occurrence is the signal.
    "chef": r"\bchef\s*/\s*puppet\b|\bpuppet\s*/\s*chef\b|\bchef\s+(?:cookbooks?|infra|automate)\b|\b(?:ansible|puppet|terraform|saltstack)\b[^.]{0,50}\bchef\b",
    "puppet": r"\bpuppet\s+(?:modules?|enterprise|manifests?)\b|\b(?:ansible|chef|terraform|saltstack)\b[^.]{0,50}\bpuppet\b",
    "saltstack": r"\bsaltstack\b|\bsalt\s+stack\b",
    "packer": r"\bpacker\b",
    "vagrant": r"\bvagrant\b",
    "nix": r"\bnixos\b|\bnix\s+(?:package|shell|flakes?)\b",
    "bazel": r"\bbazel\b",
    "cmake": r"\bcmake\b",
    # "make sure", "make an impact" — 3,867 of 8,000 bare, the worst offender
    # in the entire vocabulary.
    "make": r"\bmakefiles?\b|\bgnu\s+make\b",
    "msbuild": r"\bmsbuild\b",
    "crossplane": r"\bcrossplane\b",
    "backstage": r"\bbackstage\.io\b|\bbackstage\s+(?:portal|catalog|developer)\b",
    "open policy agent": r"\bopen\s+policy\s+agent\b|\bopa\b|\brego\b",
    "kyverno": r"\bkyverno\b",
    # "the vault", "vaulted" — 87 of 8,000 bare.
    "vault": r"\bhashicorp\s+vault\b|\bvault\s+(?:secrets?|agent|enterprise)\b|\b(?:terraform|consul|nomad)\b[^.]{0,40}\bvault\b",
    "cert-manager": r"\bcert[\s-]manager\b",
    "external secrets": r"\bexternal\s+secrets\b",
    "knative": r"\bknative\b",
    "keda": r"\bkeda\b",
    "dapr": r"\bdapr\b",
    "cloud foundry": r"\bcloud\s+foundry\b",
    "skaffold": r"\bskaffold\b",
    "tilt": r"\btilt\.dev\b|\btilt\s+(?:up|dev)\b",
    "telepresence": r"\btelepresence\b",
    "minikube": r"\bminikube\b",
    # "what kind of", "any kind" — 272 of 8,000 bare.
    "kind": r"\bkind\s+clusters?\b|\bkubernetes\s+in\s+docker\b",
    "k3s": r"\bk3s\b",
    "linux": r"\blinux\b|\bunix\b",
    "serverless": r"\bserverless\b",
    "karpenter": r"\bkarpenter\b",
    "cilium": r"\bcilium\b",
    "ebpf": r"\bebpf\b",

    # =====================================================================
    # Data engineering and analytics
    # =====================================================================
    "spark": (
        r"\bapache\s+spark\b|\bpyspark\b|\bspark\s+(?:streaming|sql|structured|jobs?|clusters?)\b"
        r"|\b(?:kafka|hadoop|flink|hive|databricks|scala|emr)\b[^.]{0,60}?\bspark\b"
        r"|\bspark\b[^.]{0,60}?\b(?:kafka|hadoop|flink|hive|databricks|emr)\b"
    ),
    "flink": r"\bflink\b",
    "beam": r"\bapache\s+beam\b",
    "hadoop": r"\bhadoop\b|\bhdfs\b",
    # "beehive", "hive mind" — the product is named beside its ecosystem.
    "hive": r"\bapache\s+hive\b|\bhive\s*(?:ql|metastore|tables?)\b|\b(?:hadoop|spark|presto|trino|impala)\b[^.]{0,40}\bhive\b",
    "kafka": r"\bkafka\b",
    "pulsar": r"\bpulsar\b",
    "rabbitmq": r"\brabbit\s?mq\b",
    "nats": r"\bnats\s+(?:streaming|jetstream|messaging)\b|\bnats\.io\b",
    "redpanda": r"\bredpanda\b",
    "airflow": r"\bairflow\b",
    "dagster": r"\bdagster\b",
    "prefect": r"\bprefect\s+(?:flows?|cloud|orchestration)\b|\b(?:airflow|dagster|luigi)\b[^.]{0,40}\bprefect\b",
    "luigi": r"\bluigi\b",
    "dbt": r"\bdbt\b",
    "databricks": r"\bdatabricks\b",
    "delta lake": r"\bdelta\s+lake\b",
    "iceberg": r"\bapache\s+iceberg\b|\biceberg\s+tables?\b",
    "hudi": r"\bhudi\b",
    "trino": r"\btrino\b",
    "presto": r"\bpresto\s*(?:db|sql)?\b(?=[^.]{0,40}\b(?:hive|trino|hadoop|query))|\bprestodb\b",
    "druid": r"\bapache\s+druid\b",
    "pinot": r"\bapache\s+pinot\b",
    "dremio": r"\bdremio\b",
    "fivetran": r"\bfivetran\b",
    "airbyte": r"\bairbyte\b",
    "debezium": r"\bdebezium\b",
    "talend": r"\btalend\b",
    "informatica": r"\binformatica\b",
    "matillion": r"\bmatillion\b",
    "stitch": r"\bstitch\s+data\b",
    "meltano": r"\bmeltano\b",
    "singer": r"\bsinger\s+taps?\b",
    "nifi": r"\bnifi\b",
    "streamsets": r"\bstreamsets\b",
    "snowpark": r"\bsnowpark\b",
    "etl": r"\betl\b|\belt\s+pipelines?\b",
    "tableau": r"\btableau\b",
    "power bi": r"\bpower\s?bi\b",
    "looker": r"\blooker\b",
    "metabase": r"\bmetabase\b",
    "superset": r"\bapache\s+superset\b",
    "qlik": r"\bqlik\b",
    "mode": r"\bmode\s+analytics\b",
    "hex": r"\bhex\s+notebooks?\b",
    "jupyter": r"\bjupyter(?:lab)?\b",
    "zeppelin": r"\bzeppelin\b",
    "great expectations": r"\bgreat\s+expectations\b",
    "datahub": r"\bdatahub\b",
    "openmetadata": r"\bopenmetadata\b",
    "unity catalog": r"\bunity\s+catalog\b",
    "collibra": r"\bcollibra\b",
    "alation": r"\balation\b",
    "segment": r"\bsegment\.com\b|\btwilio\s+segment\b|\bsegment\s+(?:analytics|tracking)\b",
    "amplitude": r"\bamplitude\s+analytics\b|\b(?:mixpanel|segment|heap)\b[^.]{0,40}\bamplitude\b",
    "mixpanel": r"\bmixpanel\b",

    # =====================================================================
    # AI, ML, LLM and MLOps
    # =====================================================================
    "numpy": r"\bnumpy\b",
    "pandas": r"\bpandas\b",
    "polars": r"\bpolars\b",
    "scipy": r"\bscipy\b",
    "scikit-learn": r"\bscikit[\s-]?learn\b|\bsklearn\b",
    "pytorch": r"\bpytorch\b",
    "tensorflow": r"\btensorflow\b",
    "keras": r"\bkeras\b",
    "jax": r"\bjax\b(?=[^.]{0,50}\b(?:flax|numpy|pytorch|tensorflow|tpu|autodiff|machine\s+learning))|\bgoogle\s+jax\b",
    "xgboost": r"\bxgboost\b",
    "lightgbm": r"\blightgbm\b",
    "catboost": r"\bcatboost\b",
    "onnx": r"\bonnx\b",
    "computer vision": r"\bcomputer\s+vision\b|\bopencv\b",
    "spacy": r"\bspacy\b",
    "nltk": r"\bnltk\b",
    "statsmodels": r"\bstatsmodels\b",
    "prophet": r"\bfbprophet\b|\bprophet\s+forecasting\b",
    "hugging face": r"\bhugging\s*face\b|\btransformers\s+library\b",
    "peft": r"\bpeft\b|\blora\s+fine[\s-]tuning\b",
    "trl": r"\btrl\s+library\b|\brlhf\b",
    "sentence transformers": r"\bsentence[\s-]transformers\b",
    "diffusers": r"\bdiffusers\b",
    "langchain": r"\blangchain\b",
    "langgraph": r"\blanggraph\b",
    "llamaindex": r"\bllama[\s-]?index\b",
    "haystack": r"\bhaystack\b",
    "semantic kernel": r"\bsemantic\s+kernel\b",
    "autogen": r"\bautogen\b",
    "crewai": r"\bcrew\s?ai\b",
    "dspy": r"\bdspy\b",
    "openai": r"\bopenai\b|\bgpt-?[345]\b|\bchatgpt\b",
    "anthropic": r"\banthropic\b",
    # "Claude" is a person's name — 357 of 8,000 bare. The product needs its
    # own context, which in practice is always an AI one.
    "claude": r"\bclaude\s+(?:api|code|opus|sonnet|haiku|models?)\b|\banthropic\b[^.]{0,40}\bclaude\b",
    "gemini": r"\bgemini\s+(?:api|pro|models?|flash)\b|\bgoogle\b[^.]{0,30}\bgemini\b",
    "llama": r"\bllama\s*[23]\b|\bllama\.cpp\b|\bmeta\s+llama\b",
    "mistral": r"\bmistral\b",
    "cohere": r"\bcohere\s+(?:api|models?|embed|rerank)\b",
    "ollama": r"\bollama\b",
    "vllm": r"\bvllm\b",
    "triton": r"\btriton\s+inference\b",
    "mlflow": r"\bmlflow\b",
    "kubeflow": r"\bkubeflow\b",
    "kserve": r"\bkserve\b",
    "seldon": r"\bseldon\b",
    "bentoml": r"\bbentoml\b",
    "ray": r"\bray\s+(?:serve|tune|train|cluster|core|data)\b|\banyscale\b|\b(?:dask|spark|kubeflow)\b[^.]{0,40}\bray\b",
    "dask": r"\bdask\b",
    "weights & biases": r"\bweights\s*&?\s*(?:and)?\s*biases\b|\bwandb\b",
    "neptune": r"\bneptune\.ai\b|\bneptune\s+ml\b",
    "comet": r"\bcomet\s+ml\b",
    "tensorboard": r"\btensorboard\b",
    "optuna": r"\boptuna\b",
    "hyperopt": r"\bhyperopt\b",
    "dvc": r"\bdvc\b",
    "feast": r"\bfeast\s+feature\s+store\b|\bfeature\s+store\b[^.]{0,30}\bfeast\b",
    "tecton": r"\btecton\b",
    "langsmith": r"\blangsmith\b",
    "langfuse": r"\blangfuse\b",
    "guardrails": r"\bguardrails\s+ai\b|\bnemo\s+guardrails\b",
    "ragas": r"\bragas\b",
    "llm": r"\bllms?\b|\blarge\s+language\s+models?\b",
    "nlp": r"\bnlp\b|\bnatural\s+language\s+processing\b",
    "rag": r"\brag\b|\bretrieval[\s-]augmented\b",
    "mlops": r"\bmlops\b",
    "deep learning": r"\bdeep\s+learning\b",
    "machine learning": r"\bmachine\s+learning\b",
    "embeddings": r"\bembeddings?\b",
    "fine-tuning": r"\bfine[\s-]tun(?:e|ing|ed)\b",
    "agentic": r"\bagentic\b|\bai\s+agents?\b",
    "mcp": r"\bmodel\s+context\s+protocol\b|\bmcp\s+servers?\b",

    # =====================================================================
    # Testing
    # =====================================================================
    "junit": r"\bjunit\b",
    "testng": r"\btestng\b",
    "mockito": r"\bmockito\b",
    "assertj": r"\bassertj\b",
    "spock": r"\bspock\s+framework\b",
    "pytest": r"\bpytest\b",
    "hypothesis": r"\bhypothesis\s+testing\s+library\b|\bproperty[\s-]based\s+testing\b",
    "tox": r"\btox\.ini\b|\btox\s+environments\b",
    "jest": r"\bjest\b",
    "mocha": r"\bmocha\b",
    "chai": r"\bchai\b",
    "jasmine": r"\bjasmine\b",
    "ava": r"\bava\s+test\b",
    "cypress": r"\bcypress\b",
    "playwright": r"\bplaywright\b",
    "selenium": r"\bselenium\b",
    "puppeteer": r"\bpuppeteer\b",
    "webdriverio": r"\bwebdriver\s?io\b",
    "cucumber": r"\bcucumber\b|\bgherkin\b",
    "robot framework": r"\brobot\s+framework\b",
    "postman": r"\bpostman\b",
    "newman": r"\bnewman\b",
    "rest assured": r"\brest[\s-]assured\b",
    "soapui": r"\bsoapui\b",
    "karate": r"\bkarate\s+(?:dsl|framework|api)\b",
    "k6": r"\bk6\b",
    "jmeter": r"\bjmeter\b",
    "gatling": r"\bgatling\b",
    "locust": r"\blocust\s+(?:io|load|performance)\b|\blocust\.io\b",
    "artillery": r"\bartillery\s+(?:io|load)\b|\bartillery\.io\b",
    "pact": r"\bpact\s+(?:broker|testing|contract)\b|\bcontract\s+testing\b[^.]{0,30}\bpact\b",
    "wiremock": r"\bwiremock\b",
    "testcontainers": r"\btestcontainers\b",
    "tdd": r"\btdd\b|\btest[\s-]driven\b|\bbdd\b",

    # =====================================================================
    # Code quality, security and observability
    # =====================================================================
    "sonarqube": r"\bsonarqube\b|\bsonarcloud\b",
    "eslint": r"\beslint\b",
    "prettier": r"\bprettier\b",
    "stylelint": r"\bstylelint\b",
    "pylint": r"\bpylint\b",
    "ruff": r"\bruff\b",
    "flake8": r"\bflake8\b",
    "black": r"\bblack\s*(?:,|/|\s+and\s+)\s*(?:flake8|isort|ruff|mypy)\b|\b(?:flake8|isort|ruff|mypy)\b[^.]{0,30}\bblack\b",
    "mypy": r"\bmypy\b",
    "checkstyle": r"\bcheckstyle\b",
    "pmd": r"\bpmd\b",
    "spotbugs": r"\bspotbugs\b",
    "jacoco": r"\bjacoco\b",
    "codecov": r"\bcodecov\b",
    "snyk": r"\bsnyk\b",
    "dependabot": r"\bdependabot\b",
    "renovate": r"\brenovate\b",
    "trivy": r"\btrivy\b",
    "grype": r"\bgrype\b",
    "owasp zap": r"\bowasp\s+zap\b",
    "burp suite": r"\bburp\s+suite\b",
    "metasploit": r"\bmetasploit\b",
    "nmap": r"\bnmap\b",
    "wireshark": r"\bwireshark\b",
    "semgrep": r"\bsemgrep\b",
    "codeql": r"\bcodeql\b",
    "checkmarx": r"\bcheckmarx\b",
    "veracode": r"\bveracode\b",
    "security": r"\bappsec\b|\bapplication\s+security\b|\bpenetration\s+testing\b|\bowasp\b",
    "sast": r"\bsast\b",
    "dast": r"\bdast\b",
    "siem": r"\bsiem\b",
    "soc 2": r"\bsoc\s*2\b",
    "zero trust": r"\bzero[\s-]trust\b",
    "oauth": r"\boauth\b",
    "oidc": r"\boidc\b|\bopenid\s+connect\b",
    "saml": r"\bsaml\b",
    "jwt": r"\bjwt\b",
    "mtls": r"\bmtls\b|\bmutual\s+tls\b",
    "auth0": r"\bauth0\b",
    "okta": r"\bokta\b",
    "keycloak": r"\bkeycloak\b",
    "prometheus": r"\bprometheus\b",
    "grafana": r"\bgrafana\b",
    "datadog": r"\bdatadog\b",
    "new relic": r"\bnew\s+relic\b",
    "splunk": r"\bsplunk\b",
    "opentelemetry": r"\bopentelemetry\b|\botel\b",
    "jaeger": r"\bjaeger\b",
    "loki": r"\bgrafana\s+loki\b|\bloki\b(?=[^.]{0,40}\b(?:grafana|logs|promtail))",
    "tempo": r"\bgrafana\s+tempo\b",
    "thanos": r"\bthanos\b",
    "sentry": r"\bsentry\b",
    "pagerduty": r"\bpagerduty\b",
    "opsgenie": r"\bopsgenie\b",
    "observability": r"\bobservability\b",

    # =====================================================================
    # Architecture, practices and business platforms
    # =====================================================================
    "ci/cd": r"\bci\s*/\s*cd\b|\bcontinuous\s+(?:integration|delivery|deployment)\b",
    "agile": r"\bagile\b|\bscrum\b",
    "microservices": r"\bmicro\s?services\b",
    "distributed systems": r"\bdistributed\s+systems?\b",
    "event-driven": r"\bevent[\s-]driven\b",
    "cqrs": r"\bcqrs\b",
    "event sourcing": r"\bevent\s+sourcing\b",
    "ddd": r"\bdomain[\s-]driven\s+design\b|\bddd\b",
    "rest": r"\brest(?:ful)?\s+api|\bapis?\s+\(rest\)",
    "grpc": r"\bgrpc\b",
    "websocket": r"\bweb\s?sockets?\b",
    "mqtt": r"\bmqtt\b",
    "openapi": r"\bopenapi\b|\bswagger\b",
    "feature flags": r"\bfeature\s+flags?\b|\bfeature\s+toggles?\b",
    "launchdarkly": r"\blaunchdarkly\b",
    "temporal": r"\btemporal\.io\b|\btemporal\s+(?:workflows?|io)\b",
    "camunda": r"\bcamunda\b",
    "kong": r"\bkong\s+(?:gateway|api)\b",
    "apigee": r"\bapigee\b",
    "stripe": r"\bstripe\s+(?:api|payments?|connect|billing)\b|\bpayments?\b[^.]{0,30}\bstripe\b",
    "twilio": r"\btwilio\b",
    "salesforce": r"\bsalesforce\b|\bapex\b",
    "sap": r"\bsap\b|\babap\b",
    "jira": r"\bjira\b",
    "figma": r"\bfigma\b",
    # "a culture where everyone can excel" — the verb was 132 of the top 141
    # bare matches, so the spreadsheet has to be named as a product.
    "excel": (
        r"\bmicrosoft\s+excel\b|\bms\s+excel\b|\bvba\b"
        r"|\bexcel\s+(?:spreadsheets?|models?|macros?|formulas?|pivot|skills)\b"
        r"|\badvanced\s+excel\b|\bexcel\s*\(|\bexcel\s*[,/]\s*(?:word|powerpoint|access|sql)\b"
    ),
}

#: The leading literal run of an alternation branch, and whether the run is
#: followed by a quantifier that makes its last character optional.
_LEAD = re.compile(r"^(?:\\b)?([a-z0-9]{3,})([?*]?)")


def _leading_literals(pattern: str) -> tuple[str, ...]:
    """Literals of which at least one must appear for ``pattern`` to match.

    Returns ``()`` when no such guarantee can be made, which means the pattern
    always runs — correct, just not accelerated.

    Soundness is the whole point, and two obvious versions of this are wrong:

    * **The longest literal run** fails on ``\\bspring\\s*(?:boot|framework|mvc)\\b``,
      where ``framework`` is the longest run but only ``spring`` is required.
    * **The leading run, taken whole** fails on ``\\bllms?\\b``: the trailing
      ``s`` is optional, so ``llms`` is not required and a description saying
      "LLM" is skipped. That version produced 80 wrong answers over 1,500
      descriptions before the quantifier check was added.

    What holds is the leading run of each branch, minus its final character when
    a ``?`` or ``*`` follows.
    """
    literals: list[str] = []
    for branch in pattern.split("|"):
        match = _LEAD.match(branch)
        if not match:
            return ()
        literal = match.group(1)
        if match.group(2):
            literal = literal[:-1]
        if len(literal) < 3:
            return ()
        literals.append(literal)
    return tuple(dict.fromkeys(literals))


#: ``(name, compiled pattern, prefilter literals)``. Patterns are compiled
#: WITHOUT ``re.I`` — `extract` lowercases the text once instead, which is both
#: faster and what makes the plain-substring prefilter valid.
_COMPILED = tuple(
    (name, re.compile(pattern), _leading_literals(pattern))
    for name, pattern in VOCABULARY.items()
)


def extract(description: str | None) -> list[str]:
    """Return the canonical names of every technology the description mentions.

    Sorted, so the stored value is stable across runs and two identical
    descriptions always produce the same string — which matters because this
    feeds a column compared on equality.
    """
    if not description:
        return []
    text = description[:MAX_SCAN_CHARS].lower()
    found = set()
    for name, pattern, literals in _COMPILED:
        # A plain substring scan is C-level and far cheaper than a regex; the
        # great majority of a several-hundred-term vocabulary is absent from any
        # one posting, so this skips most of the work.
        if literals and not any(literal in text for literal in literals):
            continue
        if pattern.search(text):
            found.add(name)
    return sorted(found)


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
