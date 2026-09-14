"""Scheduling, diffing & reliability — Phase 4 of the build SOP.

Guarantees:
  - One connector failing never blocks the rest of the run (per-source isolation).
  - Jobs are never deleted, only transitioned to status='expired'.
  - Every run is recorded in scrape_runs, including failures, so silent breakage
    (a redesign yielding 0 jobs without raising) is detectable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..connectors.base import CompanyConfig, ConnectorError, RawJob, USER_AGENT, get_connector
from ..models.schema import Company, Job, ScrapeRun, utcnow
from .normalize import LLMNormalizer, normalize

log = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Make a datetime safe to subtract from utcnow().

    SQLite has no native timestamp type and hands back naive datetimes even for
    DateTime(timezone=True) columns, while Postgres returns aware ones. Mixing
    the two in arithmetic raises TypeError, so every stored timestamp is
    normalised here before it is compared. Naive values are assumed UTC, which
    is what the writer stored.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RunStats:
    def __init__(self):
        self.companies_attempted = 0
        self.companies_succeeded = 0
        self.companies_failed = 0
        self.jobs_new = 0
        self.jobs_updated = 0
        self.jobs_expired = 0
        self.llm_calls = 0
        self.failures: list[tuple[str, str]] = []

    def as_dict(self) -> dict:
        return {
            "companies_attempted": self.companies_attempted,
            "companies_succeeded": self.companies_succeeded,
            "companies_failed": self.companies_failed,
            "jobs_new": self.jobs_new,
            "jobs_updated": self.jobs_updated,
            "jobs_expired": self.jobs_expired,
            "llm_calls": self.llm_calls,
            "failures": self.failures,
        }


class Orchestrator:
    def __init__(
        self,
        session_factory,
        llm: Optional[LLMNormalizer] = None,
        concurrency: int = 4,
        batch_size: int = 100,
        anomaly_drop_threshold: float = 0.5,
        client_factory=None,
    ):
        self.session_factory = session_factory
        self.llm = llm or LLMNormalizer()
        self.concurrency = concurrency
        self.batch_size = batch_size
        # Injectable so the pipeline can be exercised against recorded fixtures.
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
            )
        )
        # If a source returns less than this fraction of its recent typical volume,
        # treat it as suspicious rather than as a genuine hiring freeze.
        self.anomaly_drop_threshold = anomaly_drop_threshold
        self.alert_hooks: list = []

    def on_alert(self, fn):
        self.alert_hooks.append(fn)
        return fn

    async def _emit_alert(self, severity: str, message: str, context: dict):
        log.warning("ALERT [%s] %s | %s", severity, message, context)
        for hook in self.alert_hooks:
            try:
                result = hook(severity, message, context)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error("alert hook failed: %s", e)

    # ------------------------------------------------------------------
    async def run_daily(self, only_companies: Optional[Sequence[str]] = None) -> RunStats:
        stats = RunStats()

        with self.session_factory() as session:
            q = select(Company).where(Company.enabled.is_(True))
            if only_companies:
                q = q.where(Company.slug.in_(only_companies))
            companies = list(session.scalars(q))

        if not companies:
            log.warning("no enabled companies in registry")
            return stats

        # Compliance gate: never dispatch a connector for a source we've flagged.
        runnable = []
        for c in companies:
            if not c.robots_allowed or c.tos_flag:
                log.info("skipping %s — compliance flag (robots=%s tos_flag=%s)",
                         c.slug, c.robots_allowed, c.tos_flag)
                continue
            runnable.append(
                CompanyConfig(
                    company_id=c.id, name=c.name, ats_type=c.ats_type,
                    ats_identifier=c.ats_identifier, career_page_url=c.career_page_url,
                )
            )

        sem = asyncio.Semaphore(self.concurrency)
        async with self.client_factory() as client:
            async def guarded(cfg: CompanyConfig):
                async with sem:
                    return await self._process_company(cfg, client, stats)

            await asyncio.gather(*(guarded(c) for c in runnable), return_exceptions=True)

        stats.llm_calls = self.llm.calls_made
        log.info("run complete: %s", stats.as_dict())
        return stats

    # ------------------------------------------------------------------
    async def _process_company(self, cfg: CompanyConfig, client: httpx.AsyncClient,
                               stats: RunStats):
        """Fetch → normalize → diff for one company. Never raises to the caller."""
        started = time.monotonic()
        stats.companies_attempted += 1

        run = ScrapeRun(company_id=cfg.company_id, started_at=utcnow(), status="running")
        with self.session_factory() as session:
            session.add(run)
            session.commit()
            run_id = run.id

        try:
            connector = get_connector(cfg.ats_type)
            raw_jobs = await connector.fetch(cfg, client)

            normalized = []
            for rj in raw_jobs:
                if not rj.external_id or not rj.title or not rj.apply_url:
                    log.debug("skipping malformed job from %s: %r", cfg.name, rj.title)
                    continue
                fields = await normalize(
                    rj.title, rj.location, rj.department, rj.description, llm=self.llm
                )
                normalized.append((rj, fields))

            with self.session_factory() as session:
                new_c, upd_c, exp_c = self._diff_and_persist(session, cfg, normalized)
                session.commit()

            stats.companies_succeeded += 1
            stats.jobs_new += new_c
            stats.jobs_updated += upd_c
            stats.jobs_expired += exp_c

            duration_ms = int((time.monotonic() - started) * 1000)
            with self.session_factory() as session:
                session.execute(
                    update(ScrapeRun).where(ScrapeRun.id == run_id).values(
                        finished_at=utcnow(), status="success",
                        jobs_found=len(normalized), jobs_new=new_c,
                        jobs_updated=upd_c, jobs_expired=exp_c, duration_ms=duration_ms,
                    )
                )
                session.commit()

            await self._check_anomaly(cfg, len(normalized))

        except Exception as e:
            stats.companies_failed += 1
            stats.failures.append((cfg.name, f"{type(e).__name__}: {e}"))
            log.exception("connector failed for %s", cfg.name)

            duration_ms = int((time.monotonic() - started) * 1000)
            with self.session_factory() as session:
                session.execute(
                    update(ScrapeRun).where(ScrapeRun.id == run_id).values(
                        finished_at=utcnow(), status="failed",
                        error_message=f"{type(e).__name__}: {e}"[:2000],
                        duration_ms=duration_ms,
                    )
                )
                session.commit()

            await self._check_failure_streak(cfg)

    # ------------------------------------------------------------------
    def _diff_and_persist(self, session: Session, cfg: CompanyConfig,
                          normalized: list) -> tuple[int, int, int]:
        """Insert new, update seen, expire absent. Returns (new, updated, expired)."""
        existing = {
            j.external_job_id: j
            for j in session.scalars(
                select(Job).where(Job.company_id == cfg.company_id)
            )
        }
        seen_ids: set[str] = set()
        new_count = upd_count = 0
        pending = 0
        now = utcnow()

        for rj, fields in normalized:
            seen_ids.add(rj.external_id)
            content_hash = rj.content_hash()
            job = existing.get(rj.external_id)

            if job is None:
                session.add(Job(
                    company_id=cfg.company_id,
                    company_name=cfg.name,
                    external_job_id=rj.external_id,
                    title=rj.title,
                    normalized_title=fields.normalized_title,
                    department=fields.department,
                    seniority_level=fields.seniority_level,
                    location_raw=rj.location,
                    location_country=fields.location_country,
                    location_city=fields.location_city,
                    is_remote=fields.is_remote,
                    description_raw=rj.description,
                    apply_url=rj.apply_url,
                    posted_date=rj.posted_date or now,
                    first_seen_at=now,
                    last_seen_at=now,
                    status="active",
                    raw_payload=rj.raw,
                    content_hash=content_hash,
                ))
                new_count += 1
                pending += 1
                # Flush in batches. A large employer's first run is hundreds of
                # rows carrying full descriptions and raw_payload; accumulating
                # them into one INSERT produced a multi-hundred-KB statement
                # that a small Postgres instance drops mid-write ("SSL SYSCALL
                # error: EOF detected"), taking the connection with it.
                if pending >= self.batch_size:
                    session.flush()
                    pending = 0
            else:
                # A previously-expired listing that reappears is reactivated, and
                # keeps its original first_seen_at so tenure analytics stay honest.
                if job.status != "active":
                    # Record the repost before last_seen_at is overwritten: the
                    # gap between the last sighting and now is exactly how long
                    # the role was off the employer's board, and it is the only
                    # moment that interval is still recoverable.
                    prev_seen = job.last_seen_at
                    if prev_seen is not None:
                        gap_days = (_as_utc(now) - _as_utc(prev_seen)).total_seconds() / 86400.0
                        # Guard against a clock skew or a backfill producing a
                        # negative gap that would silently shrink the total.
                        if gap_days > 0:
                            job.days_unlisted = (job.days_unlisted or 0.0) + gap_days
                    job.repost_count = (job.repost_count or 0) + 1
                    job.last_reposted_at = now
                    job.status = "active"
                job.last_seen_at = now
                if job.company_name != cfg.name:
                    job.company_name = cfg.name
                if job.content_hash != content_hash:
                    job.title = rj.title
                    job.normalized_title = fields.normalized_title
                    job.department = fields.department
                    job.seniority_level = fields.seniority_level
                    job.location_raw = rj.location
                    job.location_country = fields.location_country
                    job.location_city = fields.location_city
                    job.is_remote = fields.is_remote
                    job.description_raw = rj.description
                    job.apply_url = rj.apply_url
                    job.raw_payload = rj.raw
                    job.content_hash = content_hash
                    upd_count += 1

        # Expire anything active that this run didn't see.
        expired = 0
        for ext_id, job in existing.items():
            if ext_id not in seen_ids and job.status == "active":
                job.status = "expired"
                expired += 1

        return new_count, upd_count, expired

    # ------------------------------------------------------------------
    async def _check_anomaly(self, cfg: CompanyConfig, jobs_found: int):
        """Detect silent breakage: a healthy source suddenly returning far fewer jobs."""
        with self.session_factory() as session:
            recent = list(session.scalars(
                select(ScrapeRun)
                .where(ScrapeRun.company_id == cfg.company_id,
                       ScrapeRun.status == "success")
                .order_by(ScrapeRun.started_at.desc())
                .limit(8)
            ))

        prior = [r.jobs_found for r in recent[1:] if r.jobs_found is not None]
        if len(prior) < 3:
            return

        typical = sum(prior) / len(prior)
        if typical >= 5 and jobs_found < typical * self.anomaly_drop_threshold:
            await self._emit_alert(
                "warning",
                f"{cfg.name}: job count dropped to {jobs_found} from a typical {typical:.0f}",
                {"company": cfg.name, "ats_type": cfg.ats_type,
                 "found": jobs_found, "typical": round(typical, 1),
                 "likely_cause": "career page redesign or API change"},
            )

    async def _check_failure_streak(self, cfg: CompanyConfig, threshold: int = 3):
        with self.session_factory() as session:
            recent = list(session.scalars(
                select(ScrapeRun)
                .where(ScrapeRun.company_id == cfg.company_id)
                .order_by(ScrapeRun.started_at.desc())
                .limit(threshold)
            ))
        if len(recent) >= threshold and all(r.status == "failed" for r in recent):
            await self._emit_alert(
                "critical",
                f"{cfg.name}: connector has failed {threshold} runs in a row",
                {"company": cfg.name, "ats_type": cfg.ats_type,
                 "last_error": recent[0].error_message},
            )


def reap_orphaned_runs(session, older_than_minutes: int = 90) -> int:
    """Mark long-abandoned 'running' rows as failed.

    A run is set to 'running' before work starts and updated when it finishes.
    If the process dies in between — OOM, deploy restart, platform timeout —
    nothing ever reconciles the row, so it stays 'running' forever. Those
    records then misreport the system as busy in /api/health and skew the
    anomaly detection, which compares against recent successful runs.

    Anything still 'running' well past a plausible duration was not survived by
    its process, so it is recorded as failed rather than left ambiguous.
    """
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    result = session.execute(
        update(ScrapeRun)
        .where(ScrapeRun.status == "running", ScrapeRun.started_at < cutoff)
        .values(status="failed", finished_at=utcnow(),
                error_message="abandoned — process did not finish")
    )
    session.commit()
    return result.rowcount or 0


def purge_stale(session: Session, days: int = 60) -> int:
    """Expire listings we haven't seen in N days, even if a connector has been
    silently failing. Belt-and-braces against showing stale roles."""
    cutoff = utcnow() - timedelta(days=days)
    result = session.execute(
        update(Job)
        .where(Job.status == "active", Job.last_seen_at < cutoff)
        .values(status="expired")
    )
    session.commit()
    return result.rowcount
