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

# A realistic starter registry. ats_identifier is the board token/slug each
# platform uses. Verify robots.txt and ToS before flipping `enabled` on for
# any real source — that gate is enforced in the orchestrator.
REGISTRY = [
    ("Aurora Systems",   "aurora-systems",   "greenhouse", "aurorasystems",  "Technology",   "United States"),
    ("Northwind Labs",   "northwind-labs",   "lever",      "northwindlabs",  "Technology",   "United Kingdom"),
    ("Kestrel Financial","kestrel-financial","greenhouse", "kestrelfin",     "Finance",      "Singapore"),
    ("Vantage Health",   "vantage-health",   "lever",      "vantagehealth",  "Healthcare",   "United States"),
    ("Meridian Retail",  "meridian-retail",  "greenhouse", "meridianretail", "Retail",       "Germany"),
    ("Solstice Energy",  "solstice-energy",  "lever",      "solsticeenergy", "Energy",       "Netherlands"),
    ("Cobalt Logistics", "cobalt-logistics", "greenhouse", "cobaltlog",      "Logistics",    "India"),
    ("Lumen Media",      "lumen-media",      "lever",      "lumenmedia",     "Media",        "Australia"),
]


def seed_registry():
    with SessionLocal() as s:
        for name, slug, ats_type, ident, industry, hq in REGISTRY:
            if s.query(Company).filter_by(slug=slug).first():
                continue
            s.add(Company(
                name=name, slug=slug, ats_type=ats_type, ats_identifier=ident,
                industry=industry, hq_country=hq,
                career_page_url=f"https://{slug}.example.com/careers",
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
