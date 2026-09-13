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

from app.connectors import ats, fixture, workday  # noqa: F401 — registers connectors
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

    print("\n--- Workday connector ---")
    check_workday_connector(check)

    print("\n--- Location parsing ---")
    check_location_ordering(check)

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

        # A name that already exists under a different slug must not abort the
        # batch. companies.name and companies.slug are both UNIQUE, and checking
        # only the slug let the INSERT through — the resulting IntegrityError
        # rolled back every other company and, because daily_run syncs before
        # dispatching connectors, cancelled the entire scrape. Production ran
        # zero sources and the feed went stale with no failing_sources to show.
        with SessionLocal() as s:
            renamed = s.query(Company).filter_by(slug=original[0][1]).first()
            renamed.slug = "legacy-slug-from-an-earlier-registry"
            s.commit()

        seed.REGISTRY = original[:5]
        try:
            seed.seed_registry()
            check("name collision under a different slug does not raise", True, True)
        except Exception:
            check("name collision under a different slug does not raise", False, True)

        with SessionLocal() as s:
            survived = s.query(Company).count()
        check("other companies survive a name collision", survived >= third, True)
    finally:
        seed.REGISTRY = original


def check_location_ordering(check):
    """The city fallback must not assume the city comes first.

    It previously took parts[0], which works for Greenhouse's "San Francisco, CA"
    but silently drops the city on Workday's "US, CA, Santa Clara" — parts[0] is
    "US", a country alias, so the city was discarded. 951 of NVIDIA's 2000 roles
    landed with a null city and no error anywhere.
    """
    from app.pipeline.normalize import parse_location

    def loc(raw):
        country, city, remote = parse_location(raw)
        return (country, city, remote)

    check("country-first keeps the city", loc("US, CA, Santa Clara"),
          ("United States", "Santa Clara", False))
    check("city-first still works", loc("San Francisco, CA"),
          ("United States", "San Francisco", False))
    check("two-part country-first", loc("Israel, Yokneam"),
          ("Israel", "Yokneam", False))
    check("city-first with country name", loc("Bengaluru, India"),
          ("India", "Bengaluru", False))
    check("remote with no city stays city-less", loc("US, CA, Remote"),
          ("United States", None, True))

    # Countries absent from the alias table were being stored as cities.
    check("Taiwan is a country, not a city", loc("Taiwan"), ("Taiwan", None, False))
    check("Vietnam is a country, not a city", loc("Vietnam"), ("Vietnam", None, False))


def check_workday_connector(check):
    """Workday's payload is parsed from a fixture in the real CXS response shape.

    The two things most likely to break silently are covered deliberately:
    recency arrives as prose rather than a timestamp, and the endpoint pages in
    fixed increments of 20 while reporting a `total` that must terminate the loop.
    A listing whose date silently became None would still show up in the feed,
    just never as fresh — exactly the 200-with-a-well-formed-body class of bug
    that no exception tracker catches.
    """
    from datetime import datetime, timezone
    from app.connectors.base import ConnectorError, CompanyConfig
    from app.connectors.workday import WorkdayConnector, _parse_posted_on

    conn = WorkdayConnector()
    now = datetime.now(timezone.utc)

    def age_days(text):
        dt = _parse_posted_on(text)
        return None if dt is None else round((now - dt).total_seconds() / 86400)

    check("'Posted Today' resolves to today", age_days("Posted Today"), 0)
    check("'Posted Yesterday' resolves to 1 day", age_days("Posted Yesterday"), 1)
    check("'Posted 3 Days Ago' resolves to 3 days", age_days("Posted 3 Days Ago"), 3)
    check("'Posted 30+ Days Ago' floors at 30 days", age_days("Posted 30+ Days Ago"), 30)
    check("absolute 'Posted On' date parses",
          _parse_posted_on("Posted On Jan 15, 2025").year, 2025)
    check("unparseable recency degrades to None", _parse_posted_on("Posted Recently"), None)

    # identifier parsing
    cfg = CompanyConfig(1, "Acme", "workday",
                        "acme.wd5.myworkdayjobs.com/AcmeCareers")
    check("identifier splits into host/tenant/site",
          conn._parts(cfg), ("acme.wd5.myworkdayjobs.com", "acme", "AcmeCareers"))

    bad = CompanyConfig(1, "Acme", "workday", "acme.wd5.myworkdayjobs.com")
    try:
        conn._parts(bad)
        check("identifier without a site is rejected", False, True)
    except ConnectorError:
        check("identifier without a site is rejected", True, True)

    # payload shape — mirrors the real CXS /jobs response
    posting = {
        "title": "Senior Software Engineer",
        "externalPath": "/job/US-CA-Santa-Clara/Senior-Software-Engineer_JR1234",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted 5 Days Ago",
        "bulletFields": ["JR1234"],
    }
    job = conn._to_raw_job(posting, "acme.wd5.myworkdayjobs.com", "AcmeCareers")
    check("req ID is used as external_id", job.external_id, "JR1234")
    check("apply_url points at the public career site", job.apply_url,
          "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers"
          "/job/US-CA-Santa-Clara/Senior-Software-Engineer_JR1234")
    check("location carried through", job.location, "US, CA, Santa Clara")
    check("posted date derived from prose", age_days(posting["postedOn"]), 5)

    no_bullets = conn._to_raw_job({**posting, "bulletFields": []},
                                  "acme.wd5.myworkdayjobs.com", "AcmeCareers")
    check("external_id falls back to externalPath when bulletFields is empty",
          no_bullets.external_id, posting["externalPath"])

    check("posting without externalPath is skipped",
          conn._to_raw_job({"title": "X"}, "h", "s"), None)

    multi = conn._to_raw_job({**posting, "locationsText": "6 Locations"},
                             "acme.wd5.myworkdayjobs.com", "AcmeCareers")
    check("'6 Locations' falls back to the path's primary location",
          multi.location, "US, CA, Santa Clara")

    from app.connectors.workday import _location_from_path
    check("path with country and region splits correctly",
          _location_from_path("/job/US-CA-Santa-Clara/X_JR1"), "US, CA, Santa Clara")
    check("path with country and city only",
          _location_from_path("/job/China-Shanghai/X_JR1"), "China, Shanghai")
    check("multi-word city keeps its spaces",
          _location_from_path("/job/Israel-Tel-Aviv/X_JR1"), "Israel, Tel Aviv")
    check("unrecognised path shape yields None",
          _location_from_path("/something-else"), None)

    # Pagination. Workday populates `total` on the first page only and reports 0
    # thereafter, so a connector that re-reads it each page stops after two pages
    # while still reporting success — 40 of 2000 roles, no error anywhere.
    pages = []

    async def fake_post(client, url, **kwargs):
        offset = kwargs["json"]["offset"]
        pages.append(offset)

        class R:
            @staticmethod
            def json():
                return {"total": 45 if offset == 0 else 0,
                        "jobPostings": [dict(posting, bulletFields=[f"JR{offset}-{i}"])
                                        for i in range(min(20, max(0, 45 - offset)))]}
        return R()

    import app.connectors.workday as wd
    original_post = wd.polite_post
    try:
        wd.polite_post = fake_post
        jobs = asyncio.run(conn.fetch(cfg, client=None))
    finally:
        wd.polite_post = original_post

    check("paging walks in increments of 20", pages, [0, 20, 40])
    check("paging survives total=0 on later pages", len(jobs), 45)
    check("external_ids are unique across pages", len({j.external_id for j in jobs}), 45)

if __name__ == "__main__":
    sys.exit(main())
