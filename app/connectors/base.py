"""Connector layer — Phase 2 of the build SOP.

Core principle: one connector per ATS platform, not one per company. A single
Greenhouse connector serves every company on Greenhouse by swapping the board token.

Every connector emits RawJob regardless of how it obtained the data, so the
normalization and orchestration layers treat all sources identically.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional, Protocol

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "CareerKartosBot/1.0 (+https://careerkartos.io/bot; job listing indexer; "
    "contact: ops@example.com)"
)


@dataclass
class RawJob:
    """Uniform output of every connector, pre-normalization."""
    external_id: str
    title: str
    apply_url: str
    location: Optional[str] = None
    department: Optional[str] = None
    posted_date: Optional[datetime] = None
    description: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        basis = f"{self.title}|{self.location}|{self.department}|{self.description or ''}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass
class CompanyConfig:
    company_id: int
    name: str
    ats_type: str
    ats_identifier: str
    career_page_url: Optional[str] = None


class Connector(Protocol):
    ats_type: str

    async def fetch(self, cfg: CompanyConfig, client: httpx.AsyncClient) -> list[RawJob]:
        ...


class ConnectorError(Exception):
    pass


# --------------------------------------------------------------------------
# Politeness controls. Applies to every outbound request regardless of source.
# --------------------------------------------------------------------------

class RateLimiter:
    """Per-domain throttle so we never hammer a single host, even when many
    companies in the registry share one ATS domain."""

    def __init__(self, min_interval_s: float = 1.5):
        self.min_interval = min_interval_s
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, domain: str):
        async with self._lock(domain):
            loop = asyncio.get_event_loop()
            now = loop.time()
            last = self._last.get(domain, 0.0)
            wait = self.min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[domain] = asyncio.get_event_loop().time()


RATE_LIMITER = RateLimiter()


async def polite_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 20.0,
    **kwargs,
) -> httpx.Response:
    """Request with per-domain throttling, retry-with-backoff, and 429 respect."""
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    last_exc: Optional[Exception] = None

    for attempt in range(retries):
        await RATE_LIMITER.acquire(domain)
        try:
            resp = await client.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                log.warning("429 from %s, backing off %.1fs", domain, retry_after)
                await asyncio.sleep(min(retry_after, 60))
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            log.warning("transient error on %s (attempt %d/%d): %s", url, attempt + 1, retries, e)
            await asyncio.sleep(2 ** attempt)

    raise ConnectorError(f"failed after {retries} attempts: {url} ({last_exc})")


async def polite_get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await polite_request(client, "GET", url, **kwargs)


async def polite_post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await polite_request(client, "POST", url, **kwargs)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _REGISTRY[connector.ats_type] = connector
    return connector


def get_connector(ats_type: str) -> Connector:
    if ats_type not in _REGISTRY:
        raise ConnectorError(
            f"no connector registered for ats_type={ats_type!r}. "
            f"available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[ats_type]


def available_connectors() -> list[str]:
    return sorted(_REGISTRY)
