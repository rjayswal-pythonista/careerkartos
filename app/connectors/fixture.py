"""Fixture connector — replays recorded payloads in the exact shape the real
Greenhouse and Lever APIs return.

Purpose: run and test the full pipeline (diffing, expiry, normalization, API,
frontend) with no network access, and reproduce specific edge cases on demand.
The payload shapes here mirror the real API contracts, so a fixture that passes
through the parsers proves the parsers handle the real thing.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import CompanyConfig, RawJob, register
from .ats import GreenhouseConnector, LeverConnector

_TITLES = [
    ("Senior Software Engineer, Platform", "Engineering"),
    ("Staff Backend Engineer", "Engineering"),
    ("Sr. Site Reliability Engineer", "Engineering"),
    ("Software Development Engineer II", "Engineering"),
    ("Principal Security Architect", "Security"),
    ("Data Scientist, Growth", "Data"),
    ("Senior Machine Learning Engineer", "Data"),
    ("Analytics Engineer", "Data"),
    ("Product Manager, Payments", "Product"),
    ("Senior Product Designer", "Design"),
    ("UX Researcher", "Design"),
    ("Enterprise Account Executive (m/f/d)", "Sales"),
    ("Sales Development Representative", "Sales"),
    ("Director of Demand Generation", "Marketing"),
    ("Content Marketing Manager", "Marketing"),
    ("Customer Success Manager, EMEA", "Customer Success"),
    ("Technical Support Engineer", "Support"),
    ("Senior Financial Analyst", "Finance"),
    ("Corporate Counsel", "Legal"),
    ("Technical Recruiter", "People"),
    ("Head of People Operations", "People"),
    ("IT Systems Administrator", "IT"),
    ("Engineering Manager, Infrastructure", "Engineering"),
    ("VP of Engineering", "Engineering"),
    ("Software Engineer Intern - Summer 2026", "Engineering"),
    ("junior qa automation analyst", "Engineering"),
    ("DevOps Engineer (Remote)", "Engineering"),
    ("Solutions Architect, Public Sector", "Engineering"),
]

_LOCATIONS = [
    "Bengaluru, India", "Pune, India", "Hyderabad, India", "Mumbai, India",
    "San Francisco, CA", "New York, NY, United States", "Seattle, WA",
    "Austin, TX", "Remote - US", "Remote", "London, UK", "Dublin, Ireland",
    "Berlin, Germany", "Munich, Germany", "Paris, France", "Amsterdam, Netherlands",
    "Toronto, Canada", "Sydney, Australia", "Singapore", "Tokyo, Japan",
    "Tel Aviv, Israel", "Warsaw, Poland", "Remote - EMEA", "Barcelona, Spain",
]

_DESC = (
    "We are looking for an experienced professional to join our team. "
    "You will work with cross-functional partners to design, build and ship "
    "features used by millions. Requires {yrs}+ years of experience in a "
    "similar role, strong communication skills, and a track record of "
    "delivering complex projects end to end."
)


def _role_mix(seed: int, count: int) -> list[tuple[str, str]]:
    """Each company draws its own shuffled slice of the role catalogue, so two
    companies don't produce identical listings the way a naive modulo would."""
    rng = random.Random(seed * 977)
    pool = _TITLES[:]
    rng.shuffle(pool)
    return [pool[i % len(pool)] for i in range(count)]


def _make_greenhouse_payload(seed: int, count: int, drop: set[int] | None = None) -> dict:
    rng = random.Random(seed)
    drop = drop or set()
    mix = _role_mix(seed, count)
    jobs: list[dict[str, Any]] = []
    for i in range(count):
        if i in drop:
            continue
        title, dept = mix[i]
        loc = _LOCATIONS[(i * 7 + seed * 13) % len(_LOCATIONS)]
        posted = datetime.now(timezone.utc) - timedelta(
            days=rng.randint(0, 45), hours=rng.randint(0, 23)
        )
        jobs.append({
            "id": 4000000 + seed * 1000 + i,
            "title": title,
            "absolute_url": f"https://boards.greenhouse.io/fixture/jobs/{4000000 + seed*1000 + i}",
            "updated_at": posted.isoformat(),
            "location": {"name": loc},
            "departments": [{"id": 1, "name": dept}],
            "offices": [{"id": 1, "name": loc}],
            "content": f"<p>{_DESC.format(yrs=rng.choice([2,3,5,7,10]))}</p>",
        })
    return {"jobs": jobs, "meta": {"total": len(jobs)}}


def _make_lever_payload(seed: int, count: int, drop: set[int] | None = None) -> list[dict]:
    rng = random.Random(seed + 500)
    drop = drop or set()
    mix = _role_mix(seed + 500, count)
    out = []
    for i in range(count):
        if i in drop:
            continue
        title, dept = mix[i]
        loc = _LOCATIONS[(i * 3 + seed * 29) % len(_LOCATIONS)]
        created = int(
            (datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 40))).timestamp() * 1000
        )
        out.append({
            "id": f"fixture-lever-{seed}-{i}",
            "text": title,
            "hostedUrl": f"https://jobs.lever.co/fixture/{seed}-{i}",
            "applyUrl": f"https://jobs.lever.co/fixture/{seed}-{i}/apply",
            "createdAt": created,
            "categories": {"location": loc, "department": dept, "team": dept},
            "descriptionPlain": _DESC.format(yrs=rng.choice([1, 3, 4, 8])),
            "lists": [{"text": "Requirements", "content": "<li>Strong fundamentals</li>"}],
        })
    return out


class FixtureTransport(httpx.AsyncBaseTransport):
    """Intercepts requests and returns recorded payloads, so the real connector
    parsers are exercised end to end without touching the network."""

    def __init__(self, seed: int = 1, count: int = 24, drop: set[int] | None = None,
                 vary_by_company: bool = True):
        self.seed = seed
        self.count = count
        self.drop = drop or set()
        self.vary_by_company = vary_by_company

    def _seed_for(self, url: str) -> int:
        """Derive a stable per-company seed from the board token in the URL, so
        each company yields a different role mix — as real career pages do —
        while staying deterministic across runs."""
        if not self.vary_by_company:
            return self.seed
        token = url.rstrip("/").split("?")[0].rsplit("/", 1)[-1]
        return self.seed + (int(hashlib.sha1(token.encode()).hexdigest()[:6], 16) % 9973)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seed = self._seed_for(url)
        if "greenhouse" in url:
            payload = _make_greenhouse_payload(seed, self.count, self.drop)
        elif "lever" in url:
            payload = _make_lever_payload(seed, self.count, self.drop)
        else:
            return httpx.Response(404, json={"error": "no fixture for this host"})
        return httpx.Response(200, json=payload, request=request)


def fixture_client(seed: int = 1, count: int = 24, drop: set[int] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=FixtureTransport(seed, count, drop))
