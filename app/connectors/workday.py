"""Workday connector.

Workday is the reason the registry misses most large US employers: it powers a
large share of the Fortune 500 and, unlike Greenhouse or Lever, publishes no
documented job-board API.

What it does have is the unauthenticated JSON endpoint that every Workday career
site calls from the browser to render its own public listing page:

    POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

This connector uses that endpoint. Read honestly, that is a different posture
from the connectors in ats.py, which is why it lives in its own module:

  - It is public, unauthenticated and read-only. There is no access control here
    to circumvent, and it returns exactly the postings the employer publishes
    for the public to read.
  - It is *undocumented*. Workday does not commit to its shape, so it can change
    without notice. Expect this connector to need maintenance that the
    documented-API connectors do not.

Set `robots_allowed` / `tos_flag` per employer in the registry as usual. The
orchestrator gate is what enforces the decision; this module does not override it.

`ats_identifier` is the career-site host and site name, taken straight from the
public career URL:

    https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
                    ->  "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"

The tenant is the first label of the host, so it does not need to be repeated.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .base import CompanyConfig, ConnectorError, RawJob, polite_post, register
from .ats import _parse_date, _strip_html

log = logging.getLogger(__name__)

_RELATIVE_DAYS = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)
_RELATIVE_MONTHS = re.compile(r"(\d+)\+?\s*months?\s*ago", re.I)

# Multi-site roles report a count instead of a place ("6 Locations").
_LOCATION_COUNT = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)

_PATH_LOCATION = re.compile(r"^/job/([^/]+)/")
_REGION_CODE = re.compile(r"^[A-Z]{2}$")


def _location_from_path(path: Optional[str]) -> Optional[str]:
    """Recover the primary location from externalPath.

    Multi-site roles give a count rather than a place, but the path still names
    the primary one: /job/US-CA-Santa-Clara/Senior-Engineer_JR1234. Dashes stand
    in for both the comma separators and the spaces inside a city name, so the
    split is ambiguous on its own. Reading the leading country and an optional
    two-letter region, then treating the remainder as the city, reproduces the
    same "US, CA, Santa Clara" shape the single-site rows already use — which is
    what parse_location expects.

    This is the primary site only. A role listed in six places resolves to one,
    so it is recoverable-but-incomplete rather than exact.
    """
    m = _PATH_LOCATION.match(path or "")
    if not m:
        return None

    tokens = [t for t in m.group(1).split("-") if t]
    if not tokens:
        return None

    parts, rest = [tokens[0]], tokens[1:]
    if rest and _REGION_CODE.match(rest[0]):
        parts.append(rest[0])
        rest = rest[1:]
    if rest:
        parts.append(" ".join(rest))
    return ", ".join(parts)


def _parse_posted_on(value: Optional[str]) -> Optional[datetime]:
    """Workday reports recency as prose ('Posted 3 Days Ago'), not a timestamp.

    Recency drives the feed's central claim, so this resolves to a real date
    rather than being dropped. '30+ Days Ago' is a floor, not an exact age —
    it maps to exactly 30 days, which is the most recent date consistent with
    the string and so never overstates freshness.
    """
    if not value:
        return None
    text = value.strip()
    now = datetime.now(timezone.utc)

    lowered = text.lower()
    if "today" in lowered:
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)

    if m := _RELATIVE_DAYS.search(lowered):
        return now - timedelta(days=int(m.group(1)))
    if m := _RELATIVE_MONTHS.search(lowered):
        return now - timedelta(days=30 * int(m.group(1)))

    # Some sites render an absolute date instead ("Posted On Jan 15, 2025").
    return _parse_date(re.sub(r"^posted\s+on\s+", "", text, flags=re.I))


class WorkdayConnector:
    """Workday CXS public career-site endpoint.

    `ats_identifier` is "{host}/{site}", e.g.
    "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite".
    """

    ats_type = "workday"
    PAGE_SIZE = 20          # the endpoint caps at 20 regardless of what we ask for
    MAX_JOBS = 2000         # matches the SmartRecruiters ceiling

    # Descriptions require one extra request per posting. At the shared 1.5s
    # per-domain throttle a 500-role employer would hold the run for 12 minutes,
    # so this is off by default — the same trade-off SmartRecruiters makes.
    # Rules-based normalization resolves department and seniority from the title.
    fetch_descriptions = False

    def _parts(self, cfg: CompanyConfig) -> tuple[str, str, str]:
        ident = (cfg.ats_identifier or "").strip().strip("/")
        if "/" not in ident:
            raise ConnectorError(
                f"workday ats_identifier for {cfg.name} must be '{{host}}/{{site}}', "
                f"got {ident!r}"
            )
        host, site = ident.split("/", 1)
        tenant = host.split(".")[0]
        if not tenant or not site:
            raise ConnectorError(f"unparseable workday identifier for {cfg.name}: {ident!r}")
        return host, tenant, site

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        host, tenant, site = self._parts(cfg)
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

        out: list[RawJob] = []
        offset = 0
        total = None
        while True:
            resp = await polite_post(
                client,
                url,
                json={"appliedFacets": {}, "limit": self.PAGE_SIZE,
                      "offset": offset, "searchText": ""},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ConnectorError(f"unexpected workday payload shape for {cfg.name}")

            # `total` is only populated on the first page; later pages report 0.
            # Re-reading it per page silently truncates the run to two pages.
            if total is None:
                total = payload.get("total") or 0

            batch = payload.get("jobPostings") or []
            if not batch:
                break

            for j in batch:
                if job := self._to_raw_job(j, host, site):
                    out.append(job)

            offset += self.PAGE_SIZE
            if offset >= total or offset >= self.MAX_JOBS:
                break

        if self.fetch_descriptions:
            for job in out:
                job.description = await self._fetch_description(client, job, host, tenant, site)

        return out

    def _to_raw_job(self, j: dict[str, Any], host: str, site: str) -> Optional[RawJob]:
        path = j.get("externalPath") or ""
        if not path:
            return None

        # bulletFields carries the employer's own req ID; it is the only stable
        # identifier Workday returns. externalPath is the fallback because it
        # embeds the req ID for tenants that leave bulletFields empty.
        bullets = [b for b in (j.get("bulletFields") or []) if b]
        external_id = str(bullets[0]) if bullets else path

        location = j.get("locationsText") or None
        if not location or _LOCATION_COUNT.match(location):
            location = _location_from_path(path)

        return RawJob(
            external_id=external_id,
            title=(j.get("title") or "").strip(),
            apply_url=f"https://{host}/en-US/{site}{path}",
            location=location,
            department=None,          # Workday does not expose this on the list endpoint
            posted_date=_parse_posted_on(j.get("postedOn")),
            description=None,
            raw=j,
        )

    async def _fetch_description(
        self, client: httpx.AsyncClient, job: RawJob, host: str, tenant: str, site: str
    ) -> Optional[str]:
        path = (job.raw or {}).get("externalPath") or ""
        detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{path}"
        try:
            from .base import polite_get
            resp = await polite_get(client, detail_url,
                                    headers={"Accept": "application/json"})
            info = (resp.json() or {}).get("jobPostingInfo") or {}
            return _strip_html(info.get("jobDescription"))
        except (ConnectorError, ValueError) as e:
            # A missing description must not cost us the listing itself.
            log.warning("workday detail fetch failed for %s: %s", detail_url, e)
            return None


register(WorkdayConnector())
