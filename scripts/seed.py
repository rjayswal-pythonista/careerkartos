"""Seed a local database with a realistic company registry and fixture job data.

Run:  python scripts/seed.py
Then: uvicorn app.api.main:app --reload
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors import ats, fixture  # noqa: F401 — registers connectors
from app.connectors.fixture import fixture_client
from app.db import SessionLocal, engine, init_db
from app.models.schema import Base, Company
from app.pipeline.normalize import LLMNormalizer
from app.pipeline.orchestrator import Orchestrator

# Real companies, each verified against its ATS's public job-board API on
# 2026-09-13 — every token below returned live listings at that time (6,400
# total). ats_identifier is the board token the platform uses.
#
# Tokens are CASE-SENSITIVE on Lever (e.g. "GoToGroup", not "gotogroup") and on
# SmartRecruiters. A wrong case returns "Document not found", not an error.
#
# All three platforms here publish these endpoints as documented, public,
# unauthenticated job-board APIs intended for surfacing a company's open roles,
# so robots_allowed=True / tos_flag=False is accurate for them. Verify both
# yourself before adding any company on a different platform — the orchestrator
# trusts these flags and refuses to dispatch anything flagged.
REGISTRY = [
    ('Databricks',            'databricks', 'greenhouse', 'databricks', 'Data Infrastructure',     'United States'),
    ('OpenAI',                'openai',     'ashby',      'openai',     'Artificial Intelligence', 'United States'),
    ('Stripe',                'stripe',     'greenhouse', 'stripe',     'Financial Technology',    'United States'),
    ('Anthropic',             'anthropic',  'greenhouse', 'anthropic',  'Artificial Intelligence', 'United States'),
    ('Cloudflare',            'cloudflare', 'greenhouse', 'cloudflare', 'Internet Infrastructure', 'United States'),
    ('Palantir Technologies', 'palantir',   'lever',      'palantir',   'Data Analytics',          'United States'),
    ('Samsara',               'samsara',    'greenhouse', 'samsara',    'IoT',                     'United States'),
    ('GitLab',                'gitlab',     'greenhouse', 'gitlab',     'Developer Tools',         'United States'),
    ('Coinbase',              'coinbase',   'greenhouse', 'coinbase',   'Cryptocurrency',          'United States'),
    ('Affirm',                'affirm',     'greenhouse', 'affirm',     'Financial Technology',    'United States'),
    ('Pinterest',             'pinterest',  'greenhouse', 'pinterest',  'Social Media',            'United States'),
    ('Flexport',              'flexport',   'greenhouse', 'flexport',   'Logistics',               'United States'),
    ('Airbnb',                'airbnb',     'greenhouse', 'airbnb',     'Travel',                  'United States'),
    ('Figma',                 'figma',      'greenhouse', 'figma',      'Design Software',         'United States'),
    ('Twilio',                'twilio',     'greenhouse', 'twilio',     'Communications',          'United States'),
    ('Reddit',                'reddit',     'greenhouse', 'reddit',     'Social Media',            'United States'),
    ('Ramp',                  'ramp',       'ashby',      'ramp',       'Financial Technology',    'United States'),
    ('Robinhood',             'robinhood',  'greenhouse', 'robinhood',  'Financial Technology',    'United States'),
    ('Instacart',             'instacart',  'greenhouse', 'instacart',  'E-commerce',              'United States'),
    ('Vanta',                 'vanta',      'ashby',      'vanta',      'Security Compliance',     'United States'),
    ('Asana',                 'asana',      'greenhouse', 'asana',      'Productivity Software',   'United States'),
    ('Duolingo',              'duolingo',   'greenhouse', 'duolingo',   'Education',               'United States'),
    ('Spotify',               'spotify',    'lever',      'spotify',    'Music Streaming',         'Sweden'),
    ('Discord',               'discord',    'greenhouse', 'discord',    'Social Media',            'United States'),
    ('Dropbox',               'dropbox',    'greenhouse', 'dropbox',    'Cloud Storage',           'United States'),
    ('Netlight',              'netlight',   'lever',      'netlight',   'Technology Consulting',   'Sweden'),
    ('GoTo Group',            'goto-group', 'lever',      'GoToGroup',  'Technology',              'Indonesia'),
    ('Linear',                'linear',     'ashby',      'linear',     'Developer Tools',         'United States'),
]


def _board_url(ats_type: str, ident: str) -> str:
    """The company's public job board — where a human browses these same roles.

    Derived from the ATS token rather than stored, so it cannot drift out of
    sync with the token the connector actually fetches.
    """
    return {
        "greenhouse": f"https://boards.greenhouse.io/{ident}",
        "lever": f"https://jobs.lever.co/{ident}",
        "ashby": f"https://jobs.ashbyhq.com/{ident}",
    }.get(ats_type, f"https://{ident}.com/careers")


def seed_registry():
    with SessionLocal() as s:
        for name, slug, ats_type, ident, industry, hq in REGISTRY:
            if s.query(Company).filter_by(slug=slug).first():
                continue
            s.add(Company(
                name=name, slug=slug, ats_type=ats_type, ats_identifier=ident,
                industry=industry, hq_country=hq,
                career_page_url=_board_url(ats_type, ident),
                robots_allowed=True, tos_flag=False, enabled=True,
            ))
        s.commit()
    print(f"registry seeded: {len(REGISTRY)} companies")


def main(reset: bool = True):
    if reset:
        Base.metadata.drop_all(engine)
    init_db()
    seed_registry()

    orch = Orchestrator(
        SessionLocal,
        llm=LLMNormalizer(enabled=False),
        concurrency=3,
        client_factory=lambda: fixture_client(seed=7, count=28),
    )
    orch.on_alert(lambda sev, msg, ctx: print(f"  ALERT [{sev}] {msg}"))
    stats = asyncio.run(orch.run_daily())

    print("\npipeline run:")
    for k, v in stats.as_dict().items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main(reset="--keep" not in sys.argv)
