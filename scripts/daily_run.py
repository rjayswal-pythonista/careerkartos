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
from app.pipeline.orchestrator import Orchestrator, purge_stale
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

    # Seed the registry if it is empty. The scrape is driven entirely by the
    # company table, so on a fresh database this would otherwise attempt zero
    # sources and report a clean run — the most misleading possible outcome.
    # seed_registry() skips companies that already exist, so this is a no-op
    # on every subsequent run.
    with SessionLocal() as s:
        if not s.query(Company).first():
            log.info("registry empty — seeding before first scrape")
            seed_registry()

    orch = Orchestrator(SessionLocal, llm=LLMNormalizer(), concurrency=4)
    orch.on_alert(send_alert)

    log.info("starting daily run")
    stats = asyncio.run(orch.run_daily())

    with SessionLocal() as s:
        swept = purge_stale(s, days=60)
    if swept:
        log.info("swept %d listings unseen for 60+ days", swept)

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
