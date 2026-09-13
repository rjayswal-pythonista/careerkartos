"""Daily scrape entry point. Schedule this once a day.

    0 6 * * *  cd /srv/careerkartos && python scripts/daily_run.py >> logs/daily.log 2>&1

Exits non-zero if more than a quarter of sources failed, so a supervisor or CI
scheduler can surface a bad run rather than letting it pass silently.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors import ats  # noqa: F401 — registers connectors
from app.db import SessionLocal, init_db
from app.models.schema import utcnow
from app.pipeline.normalize import LLMNormalizer
from app.models.schema import Company
from app.pipeline.orchestrator import Orchestrator, purge_stale, reap_orphaned_runs
from scripts.seed import seed_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("daily")


def send_alert(severity: str, message: str, context: dict):
    """Route alerts wherever you watch them. Stdout by default so a cron mail or
    log tail picks them up with no extra setup."""
    print(f"[{severity.upper()}] {message}")
    for k, v in context.items():
        print(f"    {k}: {v}")

    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        try:
            import httpx
            httpx.post(webhook, json={"text": f"[{severity}] {message}", **context},
                       timeout=10)
        except Exception as e:
            log.error("alert webhook failed: %s", e)


def main() -> int:
    init_db()

    # Sync the registry on every run, not only when it is empty.
    #
    # seed_registry() inserts only companies whose slug is absent, so running it
    # unconditionally is already idempotent. Gating it on an empty table meant a
    # database that already held companies never picked up newly added ones:
    # the registry grew from 28 to 73 in the repository while production stayed
    # at 28 indefinitely, with the scrape reporting a clean run every time.
    with SessionLocal() as s:
        before = s.query(Company).count()
    seed_registry()
    with SessionLocal() as s:
        after = s.query(Company).count()
    if after != before:
        log.info("registry synced: %d -> %d companies", before, after)

    # Concurrency is deliberately low. The bottleneck is the database, not the
    # remote APIs: four parallel writers streaming full descriptions and
    # raw_payload into a small Postgres instance is what exhausted it. Override
    # with SCRAPE_CONCURRENCY once the database is on a larger plan.
    concurrency = int(os.environ.get("SCRAPE_CONCURRENCY", "2"))
    # Reconcile rows left 'running' by a process that was killed mid-run. Left
    # alone they accumulate, misreport the system as permanently busy, and skew
    # the anomaly check that compares against recent successful runs.
    with SessionLocal() as s:
        reaped = reap_orphaned_runs(s)
    if reaped:
        log.warning("reaped %d abandoned run(s) from a previous process", reaped)

    orch = Orchestrator(SessionLocal, llm=LLMNormalizer(), concurrency=concurrency)
    orch.on_alert(send_alert)

    log.info("starting daily run")
    stats = asyncio.run(orch.run_daily())

    # Housekeeping, deliberately non-fatal: the scrape above has already been
    # committed, and losing the whole run's exit status to a sweep failure
    # would misreport a successful ingest as a total failure.
    try:
        with SessionLocal() as s:
            swept = purge_stale(s, days=60)
        if swept:
            log.info("swept %d listings unseen for 60+ days", swept)
    except Exception as e:
        log.error("stale sweep failed (scrape results are unaffected): %s", e)

    d = stats.as_dict()
    log.info(
        "done — %d/%d sources ok | +%d new, ~%d updated, -%d expired | %d LLM calls",
        d["companies_succeeded"], d["companies_attempted"],
        d["jobs_new"], d["jobs_updated"], d["jobs_expired"], d["llm_calls"],
    )

    for name, err in d["failures"]:
        log.error("failed: %s — %s", name, err)

    attempted = d["companies_attempted"]
    if attempted and d["companies_failed"] / attempted > 0.25:
        log.error("more than a quarter of sources failed — exiting non-zero")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
