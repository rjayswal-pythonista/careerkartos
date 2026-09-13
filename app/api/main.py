"""Public API — Phase 5 of the build SOP."""

from __future__ import annotations

import hashlib
import os
import time
import warnings
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Integer, String, and_, cast, desc, func, or_, select
from sqlalchemy.orm import Session, joinedload

from sqlalchemy import text as sa_text

from ..db import SessionLocal, engine, init_db
from ..models.schema import (
    Company, Job, OutboundClick, SavedJob, SavedSearch, ScrapeRun, User, utcnow,
)
from ..pipeline.normalize import DEPARTMENTS, SENIORITY_LEVELS


# ---------------------------------------------------------------- error tracking
# Optional and key-gated: absent SENTRY_DSN the SDK is never imported, so the
# dependency stays optional and local runs never report anywhere. Initialised
# before the app is constructed so startup failures are captured too.
def _init_sentry() -> bool:
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        warnings.warn("SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    def _scrub(event, hint):
        """Drop the query string before an event leaves the process.

        It carries the visitor's search terms, which are not ours to ship to a
        third party attached to a stack trace.
        """
        try:
            req = event.get("request") or {}
            if req.get("query_string"):
                req["query_string"] = ""
            if req.get("url"):
                req["url"] = str(req["url"]).split("?")[0]
        except Exception:
            pass
        return event

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENV", "production"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.1")),
        send_default_pii=False,
        before_send=_scrub,
    )
    return True


SENTRY_ENABLED = _init_sentry()


app = FastAPI(
    title="CareerKartos API",
    version="1.0.0",
    description=(
        "Indexes job listings published on employer career pages and links "
        "applicants to the employer's own posting. Applications are always "
        "submitted on the employer's site, never here."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Job descriptions are long and highly compressible.
app.add_middleware(GZipMiddleware, minimum_size=500)


# Responses safe to serve from a shared cache. The feed changes once a day, so
# the origin should not be answering the same query thousands of times.
# Everything else — and anything carrying an Authorization header — is
# explicitly marked private, because a CDN caching a user's saved jobs and
# serving them to the next visitor is the failure mode here.
_PUBLIC_CACHEABLE = ("/api/jobs", "/api/companies", "/api/stats")
_CACHE_TTL = int(os.environ.get("PUBLIC_CACHE_SECONDS", "600"))


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    cacheable = (
        request.method == "GET"
        and not request.headers.get("authorization")
        and path.startswith(_PUBLIC_CACHEABLE)
        and not path.startswith("/api/me")
        and "/apply" not in path          # records a click; must reach the origin
        and response.status_code == 200
    )
    if cacheable:
        response.headers["Cache-Control"] = (
            f"public, s-maxage={_CACHE_TTL}, stale-while-revalidate=86400"
        )
        # Same URL must not serve a logged-in body to an anonymous visitor.
        response.headers["Vary"] = "Accept-Encoding, Authorization"
    elif path.startswith("/api"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


# Per-IP request ceiling. Deliberately modest scope: this is in-process, so with
# multiple workers or instances the effective limit is the value times the
# process count, and X-Forwarded-For can be spoofed by the client. It stops a
# single careless script from exhausting the connection pool — it is not a
# security boundary. Put real limiting at the edge (Cloudflare/Vercel) before
# taking serious traffic, and move this to Redis if it needs to be exact.
_RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "180"))
_hits: dict[str, tuple[int, float]] = {}


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if _RATE_LIMIT <= 0 or not request.url.path.startswith("/api"):
        return await call_next(request)
    if request.url.path == "/api/health":
        return await call_next(request)     # uptime monitors poll this by design

    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "?")

    now = time.monotonic()
    count, window_start = _hits.get(ip, (0, now))
    if now - window_start >= 60:
        count, window_start = 0, now
    count += 1
    _hits[ip] = (count, window_start)

    if len(_hits) > 10_000:     # bound memory; the map is a cache, not a ledger
        for stale in [k for k, (_, t0) in _hits.items() if now - t0 >= 120]:
            _hits.pop(stale, None)

    if count > _RATE_LIMIT:
        retry = max(1, int(60 - (now - window_start)))
        return JSONResponse(
            {"detail": "Too many requests"},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    return await call_next(request)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------- schemas

class CompanyOut(BaseModel):
    id: int
    name: str
    slug: str
    logo_url: Optional[str] = None
    career_page_url: Optional[str] = None
    industry: Optional[str] = None
    hq_country: Optional[str] = None
    active_jobs: Optional[int] = None

    class Config:
        from_attributes = True


class JobOut(BaseModel):
    id: int
    title: str
    normalized_title: Optional[str]
    company: str
    company_slug: str
    company_logo: Optional[str] = None
    department: Optional[str]
    seniority_level: Optional[str]
    location_raw: Optional[str]
    location_city: Optional[str]
    location_country: Optional[str]
    is_remote: bool
    apply_url: str
    posted_date: Optional[datetime]
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    status: str
    description_summary: Optional[str] = None


class JobDetailOut(JobOut):
    description_raw: Optional[str] = None


class Paged(BaseModel):
    items: list
    total: int
    page: int
    per_page: int
    pages: int


class FacetsOut(BaseModel):
    departments: list[dict]
    seniority_levels: list[dict]
    countries: list[dict]
    companies: list[dict]
    remote_count: int
    total_active: int


# ---------------------------------------------------------------- helpers

def _job_to_out(j: Job, detail: bool = False):
    base = dict(
        id=j.id, title=j.title, normalized_title=j.normalized_title,
        company=j.company.name, company_slug=j.company.slug,
        company_logo=j.company.logo_url,
        department=j.department, seniority_level=j.seniority_level,
        location_raw=j.location_raw, location_city=j.location_city,
        location_country=j.location_country, is_remote=bool(j.is_remote),
        apply_url=j.apply_url, posted_date=j.posted_date,
        first_seen_at=j.first_seen_at, last_seen_at=j.last_seen_at,
        status=j.status, description_summary=j.description_summary,
    )
    if detail:
        return JobDetailOut(**base, description_raw=j.description_raw)
    return JobOut(**base)



# Search. On Postgres this is a full-text query against a GIN index; the
# equivalent ILIKE over description_raw is a sequential scan of every
# description in the table — measured at 292ms on 6,400 rows against 0.96ms
# for the indexed form, and it degrades linearly from there.
#
# SQLite keeps the LIKE path so local development needs no extra setup. It is
# only ever run against small fixture data, where the scan is free.
IS_POSTGRES = engine.dialect.name == "postgresql"

# The vector must match ix_jobs_fts in db.py *exactly*. An expression index is
# only used when the query expression is character-for-character the indexed
# one; a single extra column here silently drops every search back to a
# sequential scan (measured: 0.98ms indexed vs 2,720ms scanned).
#
# company_name is denormalised onto jobs specifically so it can live inside
# this vector. Matching it as `OR lower(companies.name) LIKE ...` against the
# joined table also defeats the index — Postgres cannot combine a GIN lookup
# with an OR against another relation, and falls back to scanning.
_FTS_VECTOR = func.to_tsvector(
    "english",
    func.coalesce(Job.title, "") + " "
    + func.coalesce(Job.normalized_title, "") + " "
    + func.coalesce(Job.department, "") + " "
    + func.coalesce(Job.company_name, "") + " "
    + func.coalesce(Job.description_raw, ""),
)


def _search_clause(q: str):
    """Free-text match across title, department, company and description."""
    if IS_POSTGRES:
        # websearch_to_tsquery takes human syntax (quoted phrases,
        # -exclusions) and never raises on malformed input, unlike to_tsquery.
        return _FTS_VECTOR.op("@@")(func.websearch_to_tsquery("english", q))
    like = f"%{q.lower()}%"
    return or_(
        func.lower(Job.title).like(like),
        func.lower(Job.normalized_title).like(like),
        func.lower(Company.name).like(like),
        func.lower(func.coalesce(Job.description_raw, "")).like(like),
    )


def _apply_filters(stmt, *, q, company, department, seniority, country, city,
                   remote, posted_within_days, status):
    stmt = stmt.where(Job.status == status)

    if q:
        stmt = stmt.where(_search_clause(q))
    if company:
        stmt = stmt.where(Company.slug.in_(company))
    if department:
        stmt = stmt.where(Job.department.in_(department))
    if seniority:
        stmt = stmt.where(Job.seniority_level.in_(seniority))
    if country:
        stmt = stmt.where(Job.location_country.in_(country))
    if city:
        stmt = stmt.where(func.lower(Job.location_city).in_([c.lower() for c in city]))
    if remote is not None:
        stmt = stmt.where(Job.is_remote.is_(remote))
    if posted_within_days:
        cutoff = utcnow() - timedelta(days=posted_within_days)
        # COALESCE, not OR. first_seen_at is when *we* scraped the row, so on a
        # fresh ingest every job has first_seen_at = today and an OR matches the
        # entire table — "posted within 24h" returned all 11,878 rows instead of
        # 314. first_seen_at is only a fallback for sources that publish no
        # date, which is exactly what COALESCE expresses. /api/stats and the
        # feed's own sort already use this form; this brings the filter in line.
        stmt = stmt.where(
            func.coalesce(Job.posted_date, Job.first_seen_at) >= cutoff
        )
    return stmt


# ---------------------------------------------------------------- jobs

@app.get("/api/jobs", response_model=Paged)
def list_jobs(
    db: DB,
    q: Optional[str] = Query(None, description="Free-text search"),
    company: Optional[list[str]] = Query(None),
    department: Optional[list[str]] = Query(None),
    seniority: Optional[list[str]] = Query(None),
    country: Optional[list[str]] = Query(None),
    city: Optional[list[str]] = Query(None),
    remote: Optional[bool] = Query(None),
    posted_within_days: Optional[int] = Query(None, ge=1, le=365),
    status: str = Query("active", pattern="^(active|expired)$"),
    sort: str = Query("newest", pattern="^(newest|oldest|company|title)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    base = select(Job).join(Company).options(joinedload(Job.company))
    base = _apply_filters(
        base, q=q, company=company, department=department, seniority=seniority,
        country=country, city=city, remote=remote,
        posted_within_days=posted_within_days, status=status,
    )

    count_stmt = select(func.count()).select_from(
        _apply_filters(
            select(Job.id).join(Company), q=q, company=company, department=department,
            seniority=seniority, country=country, city=city, remote=remote,
            posted_within_days=posted_within_days, status=status,
        ).subquery()
    )
    total = db.scalar(count_stmt) or 0

    order = {
        "newest": desc(func.coalesce(Job.posted_date, Job.first_seen_at)),
        "oldest": func.coalesce(Job.posted_date, Job.first_seen_at),
        "company": Company.name,
        "title": Job.normalized_title,
    }[sort]

    rows = db.scalars(
        base.order_by(order).offset((page - 1) * per_page).limit(per_page)
    ).unique().all()

    return Paged(
        items=[_job_to_out(j) for j in rows],
        total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@app.get("/api/jobs/facets", response_model=FacetsOut)
def job_facets(db: DB):
    """Filter counts for the sidebar. Computed live so empty filters can be hidden."""
    def grouped(col):
        stmt = (
            select(col, func.count())
            .select_from(Job).join(Company)
            .where(Job.status == "active", col.isnot(None))
            .group_by(col).order_by(desc(func.count()))
        )
        return [{"value": v, "count": c} for v, c in db.execute(stmt).all()]

    def grouped_companies():
        """Companies facet carries both slug and name.

        `value` must be the slug, because /api/jobs filters on Company.slug —
        emitting the display name here produced a facet the filter endpoint
        could never match, so every company filter silently returned zero.
        `label` keeps the human-readable name for the UI.
        """
        stmt = (
            select(Company.slug, Company.name, func.count())
            .select_from(Job).join(Company)
            .where(Job.status == "active")
            .group_by(Company.slug, Company.name).order_by(desc(func.count()))
        )
        return [
            {"value": slug, "label": name, "count": c}
            for slug, name, c in db.execute(stmt).all()
        ]

    return FacetsOut(
        departments=grouped(Job.department),
        seniority_levels=grouped(Job.seniority_level),
        countries=grouped(Job.location_country)[:30],
        companies=grouped_companies()[:50],
        remote_count=db.scalar(
            select(func.count()).select_from(Job)
            .where(Job.status == "active", Job.is_remote.is_(True))
        ) or 0,
        total_active=db.scalar(
            select(func.count()).select_from(Job).where(Job.status == "active")
        ) or 0,
    )


@app.get("/api/jobs/{job_id}", response_model=JobDetailOut)
def get_job(job_id: int, db: DB):
    job = db.scalars(
        select(Job).options(joinedload(Job.company)).where(Job.id == job_id)
    ).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_to_out(job, detail=True)


@app.get("/api/jobs/{job_id}/similar", response_model=list[JobOut])
def similar_jobs(job_id: int, db: DB, limit: int = Query(6, ge=1, le=20)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    stmt = (
        select(Job).join(Company).options(joinedload(Job.company))
        .where(
            Job.id != job_id, Job.status == "active",
            or_(Job.department == job.department,
                Job.seniority_level == job.seniority_level),
        )
        .order_by(
            # Matching on more axes ranks higher than a single-axis match.
            desc(
                cast(Job.department == job.department, Integer)
                + cast(Job.seniority_level == job.seniority_level, Integer)
                + cast(Job.location_country == job.location_country, Integer)
            ),
            desc(func.coalesce(Job.posted_date, Job.first_seen_at)),
        )
        .limit(limit)
    )
    return [_job_to_out(j) for j in db.scalars(stmt).unique().all()]


@app.get("/api/jobs/{job_id}/apply")
def track_and_redirect(job_id: int, request: Request, db: DB):
    """Records the outbound click, then sends the applicant to the employer's
    own posting. We never host or proxy the application itself."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    db.add(OutboundClick(
        job_id=job_id,
        referrer=request.headers.get("referer"),
        clicked_at=utcnow(),
    ))
    db.commit()
    return RedirectResponse(job.apply_url, status_code=302)


# ---------------------------------------------------------------- companies

@app.get("/api/companies", response_model=list[CompanyOut])
def list_companies(db: DB, q: Optional[str] = None):
    counts = dict(
        db.execute(
            select(Job.company_id, func.count())
            .where(Job.status == "active").group_by(Job.company_id)
        ).all()
    )
    stmt = select(Company).where(Company.enabled.is_(True))
    if q:
        stmt = stmt.where(func.lower(Company.name).like(f"%{q.lower()}%"))

    out = []
    for c in db.scalars(stmt.order_by(Company.name)):
        out.append(CompanyOut(
            id=c.id, name=c.name, slug=c.slug, logo_url=c.logo_url,
            career_page_url=c.career_page_url, industry=c.industry,
            hq_country=c.hq_country, active_jobs=counts.get(c.id, 0),
        ))
    out.sort(key=lambda c: -(c.active_jobs or 0))
    return out


@app.get("/api/companies/{slug}", response_model=CompanyOut)
def get_company(slug: str, db: DB):
    c = db.scalars(select(Company).where(Company.slug == slug)).first()
    if not c:
        raise HTTPException(404, "Company not found")
    n = db.scalar(
        select(func.count()).select_from(Job)
        .where(Job.company_id == c.id, Job.status == "active")
    )
    return CompanyOut(
        id=c.id, name=c.name, slug=c.slug, logo_url=c.logo_url,
        career_page_url=c.career_page_url, industry=c.industry,
        hq_country=c.hq_country, active_jobs=n or 0,
    )


# ---------------------------------------------------------------- stats

@app.get("/api/stats")
def stats(db: DB):
    now = utcnow()
    return {
        "total_active_jobs": db.scalar(
            select(func.count()).select_from(Job).where(Job.status == "active")) or 0,
        "total_companies": db.scalar(
            select(func.count()).select_from(Company).where(Company.enabled.is_(True))) or 0,
        "posted_last_24h": db.scalar(
            select(func.count()).select_from(Job).where(
                Job.status == "active",
                func.coalesce(Job.posted_date, Job.first_seen_at) >= now - timedelta(days=1),
            )) or 0,
        "posted_last_7d": db.scalar(
            select(func.count()).select_from(Job).where(
                Job.status == "active",
                func.coalesce(Job.posted_date, Job.first_seen_at) >= now - timedelta(days=7),
            )) or 0,
        # Null when no scrape has ever succeeded. Defaulting to now() claimed
        # the data was fresh on an empty database, which is the one moment the
        # claim is most misleading — the UI needs to be able to say "never".
        "last_updated": db.scalar(
            select(func.max(ScrapeRun.finished_at)).where(ScrapeRun.status == "success")
        ),
    }


def _search_index_present() -> bool | None:
    """Whether the full-text index exists. None on SQLite, which does not use one.

    Surfaced in /api/health because its absence is invisible from the outside —
    search still returns correct results, just by scanning the whole table, so
    the only symptom is that high-match queries get slower as the feed grows.
    """
    if not IS_POSTGRES:
        return None
    try:
        with engine.connect() as conn:
            return bool(conn.execute(sa_text(
                "SELECT 1 FROM pg_indexes WHERE tablename='jobs' AND indexname='ix_jobs_fts'"
            )).first())
    except Exception:
        return None


@app.get("/api/health")
def health(db: DB):
    """Operational health — surfaces connector breakage, not just process liveness."""
    since = utcnow() - timedelta(days=1)
    runs = db.execute(
        select(ScrapeRun.status, func.count())
        .where(ScrapeRun.started_at >= since).group_by(ScrapeRun.status)
    ).all()
    by_status = {s: c for s, c in runs}
    failing = db.execute(
        select(Company.name, ScrapeRun.error_message, func.max(ScrapeRun.started_at))
        .join(ScrapeRun, ScrapeRun.company_id == Company.id)
        .where(ScrapeRun.status == "failed", ScrapeRun.started_at >= since)
        .group_by(Company.name, ScrapeRun.error_message)
    ).all()
    total = sum(by_status.values()) or 1
    return {
        "status": "ok" if by_status.get("failed", 0) / total < 0.2 else "degraded",
        "runs_last_24h": by_status,
        "failing_sources": [
            {"company": n, "error": (e or "")[:200], "last_attempt": t} for n, e, t in failing
        ],
        "search_index": _search_index_present(),
    }


# ---------------------------------------------------------------- users

def _hash_pw(pw: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def _verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(_hash_pw(pw, salt), stored)


JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_TTL_DAYS = int(os.environ.get("JWT_TTL_DAYS", "30"))

if not JWT_SECRET:
    # Dev convenience only. A generated secret means tokens die on restart and
    # differ per worker, which is exactly the bug JWT_SECRET exists to prevent —
    # so this must be set in any deployed environment.
    JWT_SECRET = secrets.token_urlsafe(48)
    warnings.warn(
        "JWT_SECRET is not set — using an ephemeral secret. Sessions will not "
        "survive a restart and will break across multiple workers. Set JWT_SECRET "
        "in production.",
        RuntimeWarning,
    )


def _issue_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + timedelta(days=JWT_TTL_DAYS)},
        JWT_SECRET,
        algorithm="HS256",
    )


def _read_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


class SignupIn(BaseModel):
    email: str
    password: str = Field(min_length=8)


@app.post("/api/auth/signup")
def signup(body: SignupIn, db: DB):
    if db.scalars(select(User).where(User.email == body.email)).first():
        raise HTTPException(409, "An account with this email already exists")
    u = User(email=body.email, password_hash=_hash_pw(body.password))
    db.add(u)
    db.commit()
    return {"token": _issue_token(u.id), "user_id": u.id, "email": u.email}


@app.post("/api/auth/login")
def login(body: SignupIn, db: DB):
    u = db.scalars(select(User).where(User.email == body.email)).first()
    if not u or not _verify_pw(body.password, u.password_hash or ""):
        raise HTTPException(401, "Email or password is incorrect")
    return {"token": _issue_token(u.id), "user_id": u.id, "email": u.email}


def current_user(db: DB, authorization: Optional[str] = Header(None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue")
    uid = _read_token(authorization.removeprefix("Bearer "))
    if not uid:
        raise HTTPException(401, "Session expired — sign in again")
    u = db.get(User, uid)
    if not u:
        raise HTTPException(401, "Session expired — sign in again")
    return u


CurrentUser = Annotated[User, Depends(current_user)]


# ---------------------------------------------------------------- saved jobs

class SaveJobIn(BaseModel):
    job_id: int
    application_status: str = "saved"
    notes: Optional[str] = None


@app.get("/api/me/saved-jobs")
def get_saved(user: CurrentUser, db: DB):
    rows = db.execute(
        select(SavedJob, Job)
        .join(Job, Job.id == SavedJob.job_id)
        .options(joinedload(Job.company))
        .where(SavedJob.user_id == user.id)
        .order_by(desc(SavedJob.saved_at))
    ).unique().all()
    return [
        {
            "saved_at": sj.saved_at,
            "application_status": sj.application_status,
            "notes": sj.notes,
            "job": _job_to_out(j),
        }
        for sj, j in rows
    ]


@app.post("/api/me/saved-jobs")
def save_job(body: SaveJobIn, user: CurrentUser, db: DB):
    if not db.get(Job, body.job_id):
        raise HTTPException(404, "Job not found")
    existing = db.scalars(
        select(SavedJob).where(SavedJob.user_id == user.id, SavedJob.job_id == body.job_id)
    ).first()
    if existing:
        existing.application_status = body.application_status
        if body.notes is not None:
            existing.notes = body.notes
    else:
        db.add(SavedJob(user_id=user.id, job_id=body.job_id,
                        application_status=body.application_status, notes=body.notes))
    db.commit()
    return {"saved": True, "job_id": body.job_id,
            "application_status": body.application_status}


@app.delete("/api/me/saved-jobs/{job_id}")
def unsave_job(job_id: int, user: CurrentUser, db: DB):
    sj = db.scalars(
        select(SavedJob).where(SavedJob.user_id == user.id, SavedJob.job_id == job_id)
    ).first()
    if not sj:
        raise HTTPException(404, "This job isn't in your saved list")
    db.delete(sj)
    db.commit()
    return {"removed": True}


# ---------------------------------------------------------------- saved searches

class SavedSearchIn(BaseModel):
    name: str
    criteria: dict


@app.get("/api/me/saved-searches")
def list_saved_searches(user: CurrentUser, db: DB):
    rows = db.scalars(
        select(SavedSearch).where(SavedSearch.user_id == user.id)
        .order_by(desc(SavedSearch.created_at))
    ).all()
    return [
        {"id": s.id, "name": s.name, "criteria": s.criteria,
         "created_at": s.created_at, "last_notified_at": s.last_notified_at}
        for s in rows
    ]


@app.post("/api/me/saved-searches")
def create_saved_search(body: SavedSearchIn, user: CurrentUser, db: DB):
    s = SavedSearch(user_id=user.id, name=body.name, criteria=body.criteria)
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name, "criteria": s.criteria}


@app.delete("/api/me/saved-searches/{search_id}")
def delete_saved_search(search_id: int, user: CurrentUser, db: DB):
    s = db.scalars(
        select(SavedSearch).where(SavedSearch.id == search_id,
                                  SavedSearch.user_id == user.id)
    ).first()
    if not s:
        raise HTTPException(404, "Saved search not found")
    db.delete(s)
    db.commit()
    return {"removed": True}


# ---------------------------------------------------------------- apply assist

class ApplyProfileIn(BaseModel):
    """Reusable answers the applicant reviews before submitting on the employer's site."""
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    work_authorization: Optional[str] = None
    requires_sponsorship: Optional[bool] = None
    notice_period: Optional[str] = None
    years_experience: Optional[int] = None
    resume_filename: Optional[str] = None


@app.get("/api/me/apply-profile")
def get_apply_profile(user: CurrentUser):
    return user.profile_data or {}


@app.put("/api/me/apply-profile")
def update_apply_profile(body: ApplyProfileIn, user: CurrentUser, db: DB):
    user.profile_data = {**(user.profile_data or {}),
                         **body.model_dump(exclude_none=True)}
    db.commit()
    return user.profile_data


@app.get("/api/jobs/{job_id}/apply-assist")
def apply_assist(job_id: int, user: CurrentUser, db: DB):
    """Returns field values for the browser extension to prefill on the employer's
    form. The applicant reviews every field and submits it themselves — this
    endpoint never submits anything and holds no employer credentials."""
    job = db.scalars(
        select(Job).options(joinedload(Job.company)).where(Job.id == job_id)
    ).first()
    if not job:
        raise HTTPException(404, "Job not found")

    profile = user.profile_data or {}
    return {
        "job": {"id": job.id, "title": job.title, "company": job.company.name,
                "apply_url": job.apply_url, "ats_type": job.company.ats_type},
        "prefill": {
            "email": user.email,
            **{k: v for k, v in profile.items() if k != "resume_filename"},
        },
        "resume_filename": profile.get("resume_filename"),
        "submission_policy": "review_and_submit_manually",
        "notice": (
            "These values are suggestions to review before you submit. "
            "Submission happens on the employer's site, by you."
        ),
    }


@app.on_event("startup")
def _startup():
    init_db()


# Frontend is mounted last so it never shadows an /api route.
_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(_FRONTEND):
    app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
