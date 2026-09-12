"""Connectors for ATS platforms that publish documented public job board APIs.

Greenhouse and Lever both expose official, public, unauthenticated job board
endpoints intended for exactly this use — surfacing a company's open roles.
Using the documented API rather than scraping rendered HTML means:
  - no ToS grey area
  - stable structured fields (no LLM extraction needed)
  - immune to career-page redesigns
  - no anti-bot friction

For ATS platforms without a public API, see custom.py (HTML path).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from dateutil import parser as dateparser

from .base import CompanyConfig, Connector, ConnectorError, RawJob, polite_get, register

log = logging.getLogger(__name__)


def _strip_html(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = html.unescape(raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            # Lever uses epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = dateparser.parse(str(value))
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError, TypeError):
        return None


# --------------------------------------------------------------------------


class GreenhouseConnector:
    """Greenhouse public job board API.

    Endpoint: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    `ats_identifier` is the board token (e.g. the slug in boards.greenhouse.io/<token>).
    """

    ats_type = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        url = f"{self.BASE}/{cfg.ats_identifier}/jobs?content=true"
        resp = await polite_get(client, url)
        payload = resp.json()

        jobs_raw = payload.get("jobs", [])
        if not isinstance(jobs_raw, list):
            raise ConnectorError(f"unexpected greenhouse payload shape for {cfg.name}")

        out: list[RawJob] = []
        for j in jobs_raw:
            offices = j.get("offices") or []
            departments = j.get("departments") or []
            location = (j.get("location") or {}).get("name")
            if not location and offices:
                location = ", ".join(o.get("name", "") for o in offices if o.get("name"))

            out.append(
                RawJob(
                    external_id=str(j.get("id")),
                    title=(j.get("title") or "").strip(),
                    apply_url=j.get("absolute_url") or "",
                    location=location,
                    department=departments[0].get("name") if departments else None,
                    posted_date=_parse_date(j.get("updated_at") or j.get("first_published")),
                    description=_strip_html(j.get("content")),
                    raw=j,
                )
            )
        return out


class LeverConnector:
    """Lever public postings API.

    Endpoint: https://api.lever.co/v0/postings/{company}?mode=json
    `ats_identifier` is the company slug used on jobs.lever.co/<slug>.
    """

    ats_type = "lever"
    BASE = "https://api.lever.co/v0/postings"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        url = f"{self.BASE}/{cfg.ats_identifier}?mode=json"
        resp = await polite_get(client, url)
        payload = resp.json()

        if not isinstance(payload, list):
            raise ConnectorError(f"unexpected lever payload shape for {cfg.name}")

        out: list[RawJob] = []
        for j in payload:
            categories = j.get("categories") or {}
            desc = j.get("descriptionPlain") or _strip_html(j.get("description"))
            lists_text = "\n\n".join(
                f"{blk.get('text','')}\n{_strip_html(blk.get('content')) or ''}"
                for blk in (j.get("lists") or [])
            )
            full_desc = "\n\n".join(filter(None, [desc, lists_text])) or None

            out.append(
                RawJob(
                    external_id=str(j.get("id")),
                    title=(j.get("text") or "").strip(),
                    apply_url=j.get("hostedUrl") or j.get("applyUrl") or "",
                    location=categories.get("location"),
                    department=categories.get("department") or categories.get("team"),
                    posted_date=_parse_date(j.get("createdAt")),
                    description=full_desc,
                    raw=j,
                )
            )
        return out


class AshbyConnector:
    """Ashby public job board API (posting-api.ashbyhq.com).

    `ats_identifier` is the job board name.
    """

    ats_type = "ashby"
    BASE = "https://api.ashbyhq.com/posting-api/job-board"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        url = f"{self.BASE}/{cfg.ats_identifier}?includeCompensation=true"
        resp = await polite_get(client, url)
        payload = resp.json()

        out: list[RawJob] = []
        for j in payload.get("jobs", []):
            out.append(
                RawJob(
                    external_id=str(j.get("id")),
                    title=(j.get("title") or "").strip(),
                    apply_url=j.get("jobUrl") or j.get("applyUrl") or "",
                    location=j.get("location"),
                    department=j.get("department") or j.get("team"),
                    posted_date=_parse_date(j.get("publishedAt")),
                    description=_strip_html(j.get("descriptionHtml"))
                    or j.get("descriptionPlain"),
                    raw=j,
                )
            )
        return out


class SmartRecruitersConnector:
    """SmartRecruiters public postings API.

    `ats_identifier` is the company identifier used in their public posting API.
    """

    ats_type = "smartrecruiters"
    BASE = "https://api.smartrecruiters.com/v1/companies"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        out: list[RawJob] = []
        offset, limit = 0, 100

        while True:
            url = f"{self.BASE}/{cfg.ats_identifier}/postings?limit={limit}&offset={offset}"
            resp = await polite_get(client, url)
            payload = resp.json()
            batch = payload.get("content", [])
            if not batch:
                break

            for j in batch:
                loc = j.get("location") or {}
                location = ", ".join(
                    filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])
                )
                out.append(
                    RawJob(
                        external_id=str(j.get("id")),
                        title=(j.get("name") or "").strip(),
                        apply_url=j.get("applyUrl")
                        or f"https://jobs.smartrecruiters.com/{cfg.ats_identifier}/{j.get('id')}",
                        location=location or None,
                        department=(j.get("department") or {}).get("label"),
                        posted_date=_parse_date(j.get("releasedDate")),
                        description=None,  # detail fetch required; skipped to stay polite
                        raw=j,
                    )
                )

            offset += limit
            if offset >= payload.get("totalFound", 0) or offset > 2000:
                break

        return out


class RecruiteeConnector:
    """Recruitee public offers API. `ats_identifier` is the company subdomain."""

    ats_type = "recruitee"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        url = f"https://{cfg.ats_identifier}.recruitee.com/api/offers/"
        resp = await polite_get(client, url)
        payload = resp.json()

        out: list[RawJob] = []
        for j in payload.get("offers", []):
            location = ", ".join(
                filter(None, [j.get("city"), j.get("state_name"), j.get("country_code")])
            )
            out.append(
                RawJob(
                    external_id=str(j.get("id")),
                    title=(j.get("title") or "").strip(),
                    apply_url=j.get("careers_url") or j.get("careers_apply_url") or "",
                    location=location or None,
                    department=j.get("department"),
                    posted_date=_parse_date(j.get("published_at")),
                    description=_strip_html(j.get("description")),
                    raw=j,
                )
            )
        return out


class WorkableConnector:
    """Workable public jobs API. `ats_identifier` is the account subdomain."""

    ats_type = "workable"

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{cfg.ats_identifier}?details=true"
        resp = await polite_get(client, url)
        payload = resp.json()

        out: list[RawJob] = []
        for j in payload.get("jobs", []):
            location = ", ".join(
                filter(None, [j.get("city"), j.get("state"), j.get("country")])
            )
            out.append(
                RawJob(
                    external_id=str(j.get("shortcode") or j.get("id")),
                    title=(j.get("title") or "").strip(),
                    apply_url=j.get("url") or j.get("application_url") or "",
                    location=location or None,
                    department=j.get("department"),
                    posted_date=_parse_date(j.get("published_on")),
                    description=_strip_html(j.get("description")),
                    raw=j,
                )
            )
        return out


register(GreenhouseConnector())
register(LeverConnector())
register(AshbyConnector())
register(SmartRecruitersConnector())
register(RecruiteeConnector())
register(WorkableConnector())
