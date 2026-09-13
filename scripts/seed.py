"""Seed a local database with a realistic company registry and fixture job data.

Run:  python scripts/seed.py
Then: uvicorn app.api.main:app --reload
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors import ats, fixture, workday  # noqa: F401 — registers connectors
from app.connectors.fixture import fixture_client
from app.db import SessionLocal, engine, init_db
from app.models.schema import Base, Company
from app.pipeline.normalize import LLMNormalizer
from app.pipeline.orchestrator import Orchestrator

# Real companies, each verified against its ATS's public job-board API on
# 2026-09-13 — every token below returned live listings at that time
# (11,878 in total across 73 employers).
#
# ats_identifier is the board token the platform uses, and it is CASE-SENSITIVE
# on Lever ("GoToGroup", not "gotogroup"); the wrong case returns "Document not
# found" rather than an error, which is an easy way to add a silently dead
# source. Tokens that did not resolve were dropped rather than committed on
# reputation.
#
# Aggregators and job boards are deliberately excluded even when their ATS
# token works — this product indexes employers' own postings, and a reposter
# would break that claim.
#
# Greenhouse, Lever and Ashby publish these endpoints as documented, public,
# unauthenticated job-board APIs intended for surfacing a company's open roles,
# so robots_allowed=True / tos_flag=False is accurate for them.
#
# Workday rows are a different case and were checked individually: its CXS
# endpoint is public and unauthenticated but undocumented, so robots.txt was
# read per employer rather than assumed. See app/connectors/workday.py.
#
# Verify both flags yourself before adding any company — the orchestrator trusts
# them and refuses to dispatch anything flagged.
REGISTRY = [
    ('Databricks',            'databricks',            'greenhouse', 'databricks',   'Data Infrastructure',     'United States'),
    ('OpenAI',                'openai',                'ashby',      'openai',       'Artificial Intelligence', 'United States'),
    ('Stripe',                'stripe',                'greenhouse', 'stripe',       'Financial Technology',    'United States'),
    ('Anthropic',             'anthropic',             'greenhouse', 'anthropic',    'Artificial Intelligence', 'United States'),
    ('Shield AI',             'shield-ai',             'lever',      'shieldai',     'Defense Technology',      'United States'),
    ('Datadog',               'datadog',               'greenhouse', 'datadog',      'Observability',           'United States'),
    ('MongoDB',               'mongodb',               'greenhouse', 'mongodb',      'Databases',               'United States'),
    ('Cloudflare',            'cloudflare',            'greenhouse', 'cloudflare',   'Internet Infrastructure', 'United States'),
    ('Elastic',               'elastic',               'greenhouse', 'elastic',      'Search Infrastructure',   'Netherlands'),
    ('Harvey',                'harvey',                'ashby',      'harvey',       'Legal AI',                'United States'),
    ('Palantir Technologies', 'palantir-technologies', 'lever',      'palantir',     'Data Analytics',          'United States'),
    ('Verkada',               'verkada',               'greenhouse', 'verkada',      'Physical Security',       'United States'),
    ('Oscar Health',          'oscar-health',          'greenhouse', 'oscar',        'Health Insurance',        'United States'),
    ('Brex',                  'brex',                  'greenhouse', 'brex',         'Financial Technology',    'United States'),
    ('Samsara',               'samsara',               'greenhouse', 'samsara',      'IoT',                     'United States'),
    ('Roblox',                'roblox',                'greenhouse', 'roblox',       'Gaming',                  'United States'),
    ('GitLab',                'gitlab',                'greenhouse', 'gitlab',       'Developer Tools',         'United States'),
    ('Scale AI',              'scale-ai',              'greenhouse', 'scaleai',      'Artificial Intelligence', 'United States'),
    ('Coinbase',              'coinbase',              'greenhouse', 'coinbase',     'Cryptocurrency',          'United States'),
    ('Sierra',                'sierra',                'ashby',      'sierra',       'Artificial Intelligence', 'United States'),
    ('Affirm',                'affirm',                'greenhouse', 'affirm',       'Financial Technology',    'United States'),
    ('Pinterest',             'pinterest',             'greenhouse', 'pinterest',    'Social Media',            'United States'),
    ('Lyft',                  'lyft',                  'greenhouse', 'lyft',         'Transportation',          'United States'),
    ('Flexport',              'flexport',              'greenhouse', 'flexport',     'Logistics',               'United States'),
    ('Airbnb',                'airbnb',                'greenhouse', 'airbnb',       'Travel',                  'United States'),
    ('Epic Games',            'epic-games',            'greenhouse', 'epicgames',    'Gaming',                  'United States'),
    ('Figma',                 'figma',                 'greenhouse', 'figma',        'Design Software',         'United States'),
    ('Riot Games',            'riot-games',            'greenhouse', 'riotgames',    'Gaming',                  'United States'),
    ('Twilio',                'twilio',                'greenhouse', 'twilio',       'Communications',          'United States'),
    ('Reddit',                'reddit',                'greenhouse', 'reddit',       'Social Media',            'United States'),
    ('Ramp',                  'ramp',                  'ashby',      'ramp',         'Financial Technology',    'United States'),
    ('Cursor',                'cursor',                'ashby',      'cursor',       'Developer Tools',         'United States'),
    ('Robinhood',             'robinhood',             'greenhouse', 'robinhood',    'Financial Technology',    'United States'),
    ('Instacart',             'instacart',             'greenhouse', 'instacart',    'E-commerce',              'United States'),
    ('Vanta',                 'vanta',                 'ashby',      'vanta',        'Security Compliance',     'United States'),
    ('Asana',                 'asana',                 'greenhouse', 'asana',        'Productivity Software',   'United States'),
    ('Oura',                  'oura',                  'greenhouse', 'oura',         'Wearables',               'Finland'),
    ('Gusto',                 'gusto',                 'greenhouse', 'gusto',        'HR Software',             'United States'),
    ('Vercel',                'vercel',                'greenhouse', 'vercel',       'Developer Tools',         'United States'),
    ('Mixpanel',              'mixpanel',              'greenhouse', 'mixpanel',     'Product Analytics',       'United States'),
    ('Duolingo',              'duolingo',              'greenhouse', 'duolingo',     'Education',               'United States'),
    ('Replit',                'replit',                'ashby',      'replit',       'Developer Tools',         'United States'),
    ('Match Group',           'match-group',           'lever',      'matchgroup',   'Consumer Internet',       'United States'),
    ('Spotify',               'spotify',               'lever',      'spotify',      'Music Streaming',         'Sweden'),
    ('Chime',                 'chime',                 'greenhouse', 'chime',        'Financial Technology',    'United States'),
    ('Monzo',                 'monzo',                 'greenhouse', 'monzo',        'Banking',                 'United Kingdom'),
    ('Carta',                 'carta',                 'greenhouse', 'carta',        'Financial Technology',    'United States'),
    ('Mercury',               'mercury',               'greenhouse', 'mercury',      'Banking',                 'United States'),
    ('SoFi',                  'sofi',                  'greenhouse', 'sofi',         'Financial Technology',    'United States'),
    ('Twitch',                'twitch',                'greenhouse', 'twitch',       'Streaming',               'United States'),
    ('Zocdoc',                'zocdoc',                'greenhouse', 'zocdoc',       'Healthcare',              'United States'),
    ('Peloton',               'peloton',               'greenhouse', 'peloton',      'Fitness',                 'United States'),
    ('Checkr',                'checkr',                'greenhouse', 'checkr',       'Background Screening',    'United States'),
    ('Discord',               'discord',               'greenhouse', 'discord',      'Social Media',            'United States'),
    ('Fastly',                'fastly',                'greenhouse', 'fastly',       'Internet Infrastructure', 'United States'),
    ('Dropbox',               'dropbox',               'greenhouse', 'dropbox',      'Cloud Storage',           'United States'),
    ('Amplitude',             'amplitude',             'greenhouse', 'amplitude',    'Product Analytics',       'United States'),
    ('Netlight',              'netlight',              'lever',      'netlight',     'Technology Consulting',   'Sweden'),
    ('GoTo Group',            'goto-group',            'lever',      'GoToGroup',    'Technology',              'Indonesia'),
    ('Hex',                   'hex',                   'ashby',      'hex',          'Data Analytics',          'United States'),
    ('Modal',                 'modal',                 'ashby',      'modal',        'Cloud Infrastructure',    'United States'),
    ('Linear',                'linear',                'ashby',      'linear',       'Developer Tools',         'United States'),
    ('Komodo Health',         'komodo-health',         'greenhouse', 'komodohealth', 'Healthcare',              'United States'),
    ('Betterment',            'betterment',            'greenhouse', 'betterment',   'Financial Technology',    'United States'),
    ('Wise',                  'wise',                  'greenhouse', 'wise',         'Financial Technology',    'United Kingdom'),
    ('Airtable',              'airtable',              'greenhouse', 'airtable',     'Productivity Software',   'United States'),
    ('Doximity',              'doximity',              'greenhouse', 'doximity',     'Healthcare',              'United States'),
    ('Modern Health',         'modern-health',         'greenhouse', 'modernhealth', 'Mental Health',           'United States'),
    ('Calendly',              'calendly',              'greenhouse', 'calendly',     'Scheduling Software',     'United States'),
    ('Mytos',                 'mytos',                 'lever',      'mytos',        'Biotechnology',           'United Kingdom'),
    ('Lattice',               'lattice',               'greenhouse', 'lattice',      'HR Software',             'United States'),
    ('Netlify',               'netlify',               'greenhouse', 'netlify',      'Developer Tools',         'United States'),
    ('Calm',                  'calm',                  'greenhouse', 'calm',         'Mental Health',           'United States'),

    # Workday — robots.txt read per employer on 2026-09-13.
    # NVIDIA: Allow /NVIDIAExternalCareerSite/, disallows only /talentcommunity/
    # and /refreshFacet/, neither of which this connector touches.
    ('NVIDIA',                'nvidia',                'workday',    'nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite', 'Semiconductors', 'United States'),
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
        # Workday idents are already "{host}/{site}".
        "workday": f"https://{ident.split('/', 1)[0]}/en-US/{ident.split('/', 1)[-1]}",
    }.get(ats_type, f"https://{ident}.com/careers")


def seed_registry():
    with SessionLocal() as s:
        taken_slugs = {r[0] for r in s.query(Company.slug).all()}
        taken_names = {r[0] for r in s.query(Company.name).all()}

        added = 0
        for name, slug, ats_type, ident, industry, hq in REGISTRY:
            # name and slug are both UNIQUE. Checking only the slug let a row
            # whose name already existed under a different slug reach the INSERT,
            # and because the rows commit as one batch that IntegrityError took
            # every other company down with it — and the scrape with it, since
            # daily_run calls this before dispatching any connector.
            if slug in taken_slugs or name in taken_names:
                continue
            s.add(Company(
                name=name, slug=slug, ats_type=ats_type, ats_identifier=ident,
                industry=industry, hq_country=hq,
                career_page_url=_board_url(ats_type, ident),
                robots_allowed=True, tos_flag=False, enabled=True,
            ))
            taken_slugs.add(slug)
            taken_names.add(name)
            added += 1
        s.commit()
    print(f"registry seeded: {len(REGISTRY)} companies ({added} new)")


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
