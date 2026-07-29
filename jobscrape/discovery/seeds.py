"""Seed company boards, each verified live against its ATS endpoint.

Common Crawl coverage is uneven — thin for Lever, and the index itself is
occasionally unreachable or rate-limited — so these known-good boards are merged
in during discovery to guarantee a usable baseline either way. Without them a
newly added platform would sit idle whenever the index is unavailable.

Boards close over time. Nothing here is trusted blindly: `validate` re-probes
every seed like any other slug and marks the dead ones.
"""

from __future__ import annotations

SEED_BOARDS: dict[str, list[str]] = {
    'greenhouse': [
        'affirm', 'airbnb', 'airtable', 'amplitude', 'anthropic', 'archer', 'asana',
        'astranis', 'axiom', 'bird', 'block', 'brex', 'cerebral', 'chime', 'clickhouse',
        'cloudflare', 'cockroachlabs', 'coinbase', 'coursera', 'databricks', 'datadog',
        'discord', 'duolingo', 'elastic', 'epicgames', 'faire', 'figma', 'fivetran',
        'flexport', 'freenome', 'ghost', 'gitlab', 'gusto', 'honeycomb', 'honor', 'instacart',
        'khanacademy', 'kodiak', 'launchdarkly', 'lyft', 'make', 'marqeta', 'medium',
        'mercury', 'mixpanel', 'mongodb', 'monzo', 'neo4j', 'netlify', 'newrelic', 'newsela',
        'nuro', 'oscar', 'outschool', 'pacaso', 'pagerduty', 'pinterest', 'planetscale',
        'project44', 'reddit', 'relativity', 'remote', 'riotgames', 'robinhood', 'samsara',
        'smartsheet', 'spire', 'squarespace', 'stockx', 'stripe', 'sumologic', 'twitch',
        'udemy', 'vercel', 'verkada', 'waymo', 'webflow'
    ],
    'lever': [
        'gopuff', 'houzz', 'lyrahealth', 'palantir', 'ro', 'spotify', 'zoox'
    ],
    'ashby': [
        'airbyte', 'amplitude', 'angi', 'benchling', 'brightwheel', 'cedar', 'clickhouse',
        'clickup', 'cohere', 'confluent', 'docker', 'ghost', 'headway', 'insitro', 'lime',
        'linear', 'midjourney', 'notion', 'openai', 'outschool', 'patreon', 'perplexity',
        'plaid', 'poshmark', 'posthog', 'ramp', 'redis', 'render', 'replit', 'runway',
        'sentry', 'skydio', 'snowflake', 'substack', 'supabase', 'temporal', 'thumbtack',
        'unit', 'whatnot', 'zapier'
    ],
    'bamboohr': [
        '100percentgroup', '1010games', '10web', '120water', '12thtribe', '2050partners'
    ],
    'breezy': [
        '021-strategic', '1-grid', '1001', '10percent', '11onze-bank', '11sigma'
    ],
    'recruitee': [
        '11bitstudios', '1on1english', '1x', '288group', '4germany', '4players'
    ],
    'teamtailor': [
        'aebr', 'almond-board-of-cyber', 'assistansbolaget', 'aswo', 'atentel', 'avosano'
    ],
    'pinpoint': [
        'armstrongwatson', 'article', 'avaaz', 'birchesgroup', 'bnc-ox-ac', 'bright'
    ],
    'jazzhr': [
        '2020onsite', '2blivinginc', '2waytv', '36pix', '3eyetechnologies', '412foodrescue'
    ],
    'personio': [
        '1komma5grad', '1nce', '1sp-agency', '1xinternet', '2perspectives', '360t',
        '360volt-gmbh', '3dqr-gmbh', '4401'
    ],
    'avature': [
        'a2milkkf', 'advocateaurorahealth', 'aesc', 'ally', 'ameren'
    ],
}


def seed_count() -> int:
    return sum(len(v) for v in SEED_BOARDS.values())
