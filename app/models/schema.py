"""Database schema — implements Phase 1 of the build SOP.

Design notes:
  - (company_id, external_job_id) is the dedup key. Never match on title/location.
  - raw_payload is always retained so normalization can be reprocessed without re-scraping.
  - Jobs are never deleted; they transition to status='expired'. Deletion loses the
    time-to-fill analytics that V2/V3 features depend on.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Boolean, ForeignKey,
    UniqueConstraint, Index, JSON, Float,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    slug = Column(String(200), nullable=False, unique=True, index=True)
    career_page_url = Column(Text)
    ats_type = Column(String(50), index=True)   # greenhouse | lever | workday | custom ...
    ats_identifier = Column(String(200))        # board token / tenant slug used by the connector
    logo_url = Column(Text)
    industry = Column(String(120))
    hq_country = Column(String(80))

    # Phase 0 compliance registry fields — checked before a connector is ever enabled.
    robots_allowed = Column(Boolean, default=True)
    tos_flag = Column(Boolean, default=False)   # True = ToS explicitly restricts automated access
    rate_limit_notes = Column(Text)
    enabled = Column(Boolean, default=True, index=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)

    jobs = relationship("Job", back_populates="company", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    external_job_id = Column(String(200), nullable=False)

    title = Column(Text, nullable=False)
    normalized_title = Column(Text, index=True)
    department = Column(String(80), index=True)
    seniority_level = Column(String(40), index=True)

    location_raw = Column(Text)
    # Text rather than a bounded String: these are parsed out of free-form
    # location strings that employers write however they like ("Remote - US,
    # Canada, UK, Germany, ..."), and a length cap turns an unusual posting
    # into a hard DataError that fails the whole company's batch.
    location_country = Column(Text, index=True)
    location_city = Column(Text, index=True)
    is_remote = Column(Boolean, default=False, index=True)

    # Denormalised from companies.name so it can sit inside the full-text
    # vector. Searching the joined column instead forces a sequential scan,
    # because a GIN lookup cannot be OR'd with a predicate on another table.
    # Kept in sync by the orchestrator on insert and on company rename.
    company_name = Column(String(200), index=True)
    description_raw = Column(Text)
    description_summary = Column(Text)

    apply_url = Column(Text, nullable=False)
    posted_date = Column(DateTime(timezone=True), index=True)

    first_seen_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    status = Column(String(20), default="active", index=True)   # active | expired

    raw_payload = Column(JSON)
    content_hash = Column(String(64))  # detects in-place edits to a listing

    __table_args__ = (
        UniqueConstraint("company_id", "external_job_id", name="uq_company_external_job"),
        Index("ix_jobs_status_posted", "status", "posted_date"),
        Index("ix_jobs_search", "normalized_title", "department", "seniority_level"),
    )

    company = relationship("Company", back_populates="jobs")


class ScrapeRun(Base):
    """One row per connector execution. This table is the operational nervous system —
    silent breakage (a redesign that yields 0 jobs without erroring) is detected here."""
    __tablename__ = "scrape_runs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    started_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at = Column(DateTime(timezone=True))
    status = Column(String(20), index=True)   # success | failed | partial
    jobs_found = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    jobs_updated = Column(Integer, default=0)
    jobs_expired = Column(Integer, default=0)
    duration_ms = Column(Integer)
    error_message = Column(Text)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=utcnow)
    digest_frequency = Column(String(20), default="daily")  # daily | weekly | off

    # Apply Assist profile — reusable answers the user reviews before submitting
    # on the employer's own site. Never auto-submitted.
    profile_data = Column(JSON, default=dict)


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120))
    criteria = Column(JSON)   # mirrors the /jobs filter params
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_notified_at = Column(DateTime(timezone=True))


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    saved_at = Column(DateTime(timezone=True), default=utcnow)
    application_status = Column(String(30), default="saved")  # saved|applied|interviewing|rejected|offer
    notes = Column(Text)

    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_user_job"),)


class OutboundClick(Base):
    """Tracked before redirecting to the employer. Feeds 'most viewed' and conversion analytics."""
    __tablename__ = "outbound_clicks"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    clicked_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    referrer = Column(Text)
