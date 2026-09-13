"""End-to-end pipeline exercise against recorded fixtures.

Covers the behaviours that are easy to get wrong and expensive to discover in
production: idempotency across runs, expiry when a listing disappears,
reactivation when it returns, compliance gating, and silent-breakage alerting.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Own database, set before app.db is imported. Keeps this suite from clobbering
# the seeded dev database or the API suite's fixtures.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_e2e.db")
logging.basicConfig(level=logging.ERROR)

from sqlalchemy import func, select

from app.connectors import ats, fixture  # noqa: F401 — registers connectors
from app.connectors.fixture import fixture_client
from app.db import SessionLocal, engine, init_db
from app.models.schema import Base, Company, Job, ScrapeRun
from app.pipeline.normalize import LLMNormalizer
from app.pipeline.orchestrator import Orchestrator

PASS, FAIL = "  ok  ", " FAIL "
results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"[{PASS if ok else FAIL}] {label}: got {actual}, expected {expected}")


def counts():
    with SessionLocal() as s:
        return (
            s.scalar(select(func.count()).select_from(Job).where(Job.status == "active")),
            s.scalar(select(func.count()).select_from(Job).where(Job.status == "expired")),
        )


def run(seed=1, count=24, drop=None):
    alerts = []
    orch = Orchestrator(
        SessionLocal,
        llm=LLMNormalizer(enabled=False),
        concurrency=2,
        client_factory=lambda: fixture_client(seed, count, drop),
    )
    orch.on_alert(lambda sev, msg, ctx: alerts.append((sev, msg)))
    stats = asyncio.run(orch.run_daily())
    return stats, alerts


def main():
    Base.metadata.drop_all(engine)
    init_db()

    with SessionLocal() as s:
        s.add_all([
            Company(name="Acme Corp", slug="acme", ats_type="greenhouse",
                    ats_identifier="acme", enabled=True),
            Company(name="Globex", slug="globex", ats_type="lever",
                    ats_identifier="globex", enabled=True),
            Company(name="Blocked Co", slug="blocked", ats_type="greenhouse",
                    ats_identifier="x", tos_flag=True, enabled=True),
            Company(name="Disabled Co", slug="disabled", ats_type="lever",
                    ats_identifier="y", enabled=False),
        ])
        s.commit()

    print("\n--- Day 1: first scrape ---")
    stats, _ = run()
    check("companies attempted (tos_flag + disabled excluded)", stats.companies_attempted, 2)
    check("companies succeeded", stats.companies_succeeded, 2)
    check("new jobs inserted", stats.jobs_new, 48)
    a, e = counts()
    check("active jobs", a, 48)
    check("expired jobs", e, 0)

    print("\n--- Day 2: identical source data (idempotency) ---")
    stats, _ = run()
    check("no duplicate inserts", stats.jobs_new, 0)
    check("no spurious updates", stats.jobs_updated, 0)
    check("no spurious expiries", stats.jobs_expired, 0)
    a, e = counts()
    check("active unchanged", a, 48)

    print("\n--- Day 3: 4 listings removed per source (expiry) ---")
    stats, _ = run(drop={0, 1, 2, 3})
    check("expired count", stats.jobs_expired, 8)
    a, e = counts()
    check("active after expiry", a, 40)
    check("expired retained (not deleted)", e, 8)

    print("\n--- Day 4: listings reappear (reactivation) ---")
    stats, _ = run()
    check("reactivated not re-inserted", stats.jobs_new, 0)
    a, e = counts()
    check("active restored", a, 48)
    check("expired cleared", e, 0)

    print("\n--- Day 5: source collapses to 2 (silent-breakage alert) ---")
    stats, alerts = run(count=2)
    check("anomaly alert raised", len(alerts) >= 1, True)
    if alerts:
        print(f"         alert: {alerts[0][0]} — {alerts[0][1]}")

    print("\n--- Connector failure isolation ---")
    with SessionLocal() as s:
        s.add(Company(name="Broken Co", slug="broken", ats_type="nonexistent_ats",
                      ats_identifier="z", enabled=True))
        s.commit()
    stats, _ = run()
    check("failed source recorded", stats.companies_failed, 1)
    check("healthy sources still succeeded", stats.companies_succeeded, 2)

    with SessionLocal() as s:
        failed = s.scalar(
            select(func.count()).select_from(ScrapeRun).where(ScrapeRun.status == "failed")
        )
        check("failure logged to scrape_runs", failed >= 1, True)

    print("\n--- Registry sync ---")
    check_registry_sync_adds_new_companies(check)

    print("\n--- Normalization spot check ---")
    with SessionLocal() as s:
        rows = s.execute(
            select(Job.normalized_title, Job.department, Job.seniority_level,
                   Job.location_city, Job.location_country, Job.is_remote)
            .where(Job.status == "active").limit(8)
        ).all()
        for r in rows:
            print(f"         {r[0][:42]:44} {r[1][:16]:17} {r[2]:9} "
                  f"{str(r[3])[:14]:15} {str(r[4])[:14]:15} remote={r[5]}")

        unresolved = s.scalar(
            select(func.count()).select_from(Job).where(Job.department == "Other")
        )
        total = s.scalar(select(func.count()).select_from(Job))
        pct = 100 * (1 - unresolved / total) if total else 0
        print(f"\n         rules-only department resolution: {pct:.1f}% "
              f"({total - unresolved}/{total})")

    print(f"\n{'='*60}")
    print(f"  {sum(results)}/{len(results)} checks passed")
    print(f"{'='*60}")
    return 0 if all(results) else 1



def check_registry_sync_adds_new_companies(check):
    """A registry that grows must reach a database that already has rows.

    seed_registry() only inserts absent slugs, so it is safe to run every time.
    It was previously gated on an empty table, which meant production stayed at
    its original company count forever while the repository registry grew —
    and every scrape still reported success.
    """
    from app.models.schema import Company
    import scripts.seed as seed

    original = seed.REGISTRY
    try:
        seed.REGISTRY = original[:2]
        seed.seed_registry()
        with SessionLocal() as s:
            first = s.query(Company).count()

        seed.REGISTRY = original[:5]
        seed.seed_registry()
        with SessionLocal() as s:
            second = s.query(Company).count()

        check("registry sync picks up newly added companies", second, first + 3)

        seed.seed_registry()
        with SessionLocal() as s:
            third = s.query(Company).count()
        check("registry sync is idempotent", third, second)
    finally:
        seed.REGISTRY = original

if __name__ == "__main__":
    sys.exit(main())
