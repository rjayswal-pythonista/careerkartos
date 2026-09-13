"""Probe candidate companies across every supported ATS and report which resolve.

SOP §2 describes verifying one token at a time with curl. That is correct and
does not scale to the registry size this product needs, so this does the same
checks concurrently and prints registry-ready rows for the ones that answer.

    python scripts/discover.py --names "Airbnb,Coinbase,Figma"
    python scripts/discover.py --file candidates.txt

It deliberately stops short of writing to the registry. Every row still needs a
human to set robots_allowed/tos_flag, because the orchestrator trusts those
flags and a token resolving says nothing about whether we are welcome to index
it. Where a robots.txt is available this prints what it says, so that judgement
is informed rather than assumed.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.base import USER_AGENT  # noqa: E402

# Workday tenants are spread across numbered datacenters with no way to tell
# which from the company name, so each is tried.
WORKDAY_DCS = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd103"]
_SITEMAP = re.compile(r"^Sitemap:\s*https?://[^/]+/([^/]+)/", re.I | re.M)

TIMEOUT = httpx.Timeout(10.0)

# Workday tenants expose boards that are not public job boards: affinity-group
# sites, contingent-worker and recall pools, internal transfer boards, and some
# explicitly named private. They resolve exactly like a real board, so they have
# to be excluded by name.
_PRIVATE_SITE = re.compile(
    r"private|internal|contingent|contractor|recall|conversion|_only\b"
    r"|transfer|alumni|rehire|referral",
    re.I,
)

# A board token is a claim, not proof. Anyone can register the Recruitee
# subdomain "google". The Workday host is different — it is the employer's own
# subdomain, so the tenant itself is the evidence.
SELF_HOSTED = {"workday"}


@dataclass
class Hit:
    name: str
    ats_type: str
    identifier: str
    job_count: int
    robots: str = "not checked"
    slug: str = ""
    sample: str = ""

    @property
    def identity_verified(self) -> bool:
        return self.ats_type in SELF_HOSTED

    def registry_row(self) -> str:
        name, slug = f"{self.name!r},", f"{self.slug!r},"
        ats, ident = f"{self.ats_type!r},", f"{self.identifier!r},"
        return (f"    ({name:<26}{slug:<26}{ats:<18}{ident} "
                f"'TODO industry', 'TODO country'),")


def assign_slugs(hits: list[Hit]) -> None:
    """Large employers run several boards (Salesforce exposes Slack, Tableau and
    Mulesoft separately). They all slugify to the same string, and companies.name
    and companies.slug are both UNIQUE — so without distinct values here, the
    batch insert in seed_registry raises IntegrityError and takes the whole
    registry sync down.
    """
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    for h in hits:
        base = slugify(h.name)
        site = h.identifier.rsplit("/", 1)[-1] if "/" in h.identifier else h.ats_type

        slug = base if base not in seen_slugs else f"{base}-{slugify(site)}"
        n = 2
        while slug in seen_slugs:
            slug = f"{base}-{slugify(site)}-{n}"
            n += 1
        seen_slugs.add(slug)
        h.slug = slug

        name = h.name if h.name not in seen_names else f"{h.name} — {site}"
        n = 2
        while name in seen_names:
            name = f"{h.name} — {site} ({n})"
            n += 1
        seen_names.add(name)
        h.name = name


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def token_variants(name: str) -> list[str]:
    """Board tokens are usually the company name stripped of punctuation, but
    Lever is case-sensitive and several boards keep hyphens, so try both."""
    base = re.sub(r"[^a-z0-9]+", "", name.lower())
    hyphen = slugify(name)
    return list(dict.fromkeys([base, hyphen]))


async def _json(client: httpx.AsyncClient, url: str) -> Optional[object]:
    try:
        r = await client.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def probe_api_boards(client: httpx.AsyncClient, name: str) -> list[Hit]:
    """The six platforms with documented public job-board APIs."""
    hits: list[Hit] = []
    for tok in token_variants(name):
        checks = [
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs",
             lambda p: p.get("jobs", []) if isinstance(p, dict) else [],
             lambda j: f"{j.get('title','?')} — {j.get('absolute_url','')}"),
            ("lever", f"https://api.lever.co/v0/postings/{tok}?mode=json",
             lambda p: p if isinstance(p, list) else [],
             lambda j: f"{j.get('text','?')} — {j.get('hostedUrl','')}"),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{tok}",
             lambda p: p.get("jobs", []) if isinstance(p, dict) else [],
             lambda j: f"{j.get('title','?')} — {j.get('jobUrl','')}"),
            ("smartrecruiters",
             f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=1",
             lambda p: p.get("content", []) if isinstance(p, dict) else [],
             lambda j: f"{j.get('name','?')} — {(j.get('company') or {}).get('name','')}"),
            ("recruitee", f"https://{tok}.recruitee.com/api/offers/",
             lambda p: p.get("offers", []) if isinstance(p, dict) else [],
             lambda j: f"{j.get('title','?')} — {j.get('careers_url','')}"),
            ("workable",
             f"https://apply.workable.com/api/v1/widget/accounts/{tok}",
             lambda p: p.get("jobs", []) if isinstance(p, dict) else [],
             lambda j: f"{j.get('title','?')} — {j.get('url','')}"),
        ]
        for ats_type, url, lister, sampler in checks:
            payload = await _json(client, url)
            if payload is None:
                continue
            try:
                jobs = lister(payload)
            except (AttributeError, TypeError):
                continue
            # A board that resolves but is empty is indistinguishable from a
            # wrong token, and committing it adds a silently dead source.
            if not jobs:
                continue
            try:
                sample = sampler(jobs[0])[:96]
            except (AttributeError, TypeError):
                sample = ""
            hits.append(Hit(name, ats_type, tok, len(jobs), sample=sample))
    return hits


async def probe_workday(client: httpx.AsyncClient, name: str) -> list[Hit]:
    """Workday needs a site name as well as a tenant, and neither is guessable.

    robots.txt gives it away: every Workday career site advertises its sitemap,
    and the first path segment of that URL is the site name.
    """
    hits: list[Hit] = []
    for tok in token_variants(name):
        for dc in WORKDAY_DCS:
            host = f"{tok}.{dc}.myworkdayjobs.com"
            try:
                r = await client.get(f"https://{host}/robots.txt", timeout=TIMEOUT)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue

            sites = dict.fromkeys(_SITEMAP.findall(r.text))
            for site in sites:
                if _PRIVATE_SITE.search(site):
                    print(f"  [ skip] {name:22} {site[:44]:46} looks non-public")
                    continue
                url = f"https://{host}/wday/cxs/{tok}/{site}/jobs"
                try:
                    jr = await client.post(
                        url, json={"appliedFacets": {}, "limit": 1,
                                   "offset": 0, "searchText": ""},
                        timeout=TIMEOUT,
                    )
                    total = jr.json().get("total", 0) if jr.status_code == 200 else 0
                except (httpx.HTTPError, ValueError, AttributeError):
                    continue
                if total > 0:
                    hits.append(Hit(name, "workday", f"{host}/{site}", total,
                                    robots=_summarise_robots(r.text, site)))
            if sites:
                break   # tenant found; no need to try further datacenters
    return hits


def _summarise_robots(text: str, site: str) -> str:
    allows = re.findall(r"^Allow:\s*(\S+)", text, re.I | re.M)
    disallows = re.findall(r"^Disallow:\s*(\S+)", text, re.I | re.M)
    if any(d.strip() == "/" for d in disallows):
        return "DISALLOW ALL — do not index"
    site_allowed = any(site in a for a in allows)
    return (f"{'allows ' + site if site_allowed else 'no explicit allow'}; "
            f"disallows: {', '.join(disallows) or 'none'}")


async def discover(names: list[str], concurrency: int) -> list[Hit]:
    sem = asyncio.Semaphore(concurrency)
    results: list[Hit] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:

        async def one(name: str):
            async with sem:
                found = await probe_api_boards(client, name)
                if not found:
                    found = await probe_workday(client, name)
                if found:
                    for h in found:
                        print(f"  [ hit ] {h.name:22} {h.ats_type:16} "
                              f"{h.identifier[:48]:50} {h.job_count} roles")
                        if h.robots != "not checked":
                            print(f"          robots: {h.robots}")
                else:
                    print(f"  [ --- ] {name:22} no supported board found")
                results.extend(found)

        await asyncio.gather(*(one(n) for n in names))

    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--names", help="comma-separated company names")
    ap.add_argument("--file", help="file with one company name per line")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--all", action="store_true",
                    help="include secondary boards on an already-matched tenant")
    args = ap.parse_args()

    names: list[str] = []
    if args.names:
        names += [n.strip() for n in args.names.split(",") if n.strip()]
    if args.file:
        names += [l.strip() for l in Path(args.file).read_text().splitlines()
                  if l.strip() and not l.startswith("#")]
    if not names:
        ap.error("provide --names or --file")

    print(f"probing {len(names)} companies across "
          f"{6 + len(WORKDAY_DCS)} endpoints each\n")
    hits = asyncio.run(discover(names, args.concurrency))

    print(f"\n{'='*70}")
    print(f"  {len(hits)} sources found across {len({h.name for h in hits})} "
          f"of {len(names)} companies")
    print(f"{'='*70}\n")

    if hits:
        assign_slugs(hits)
        verified = [h for h in hits if h.identity_verified]
        unverified = [h for h in hits if not h.identity_verified]

        if verified:
            # One tenant often exposes a dozen boards: the main careers site plus
            # event, early-careers and affinity-group sites that mostly re-list
            # the same roles. The largest is the real one; the rest need a look
            # before they are committed, or the feed doubles up on one employer.
            primary: dict[str, Hit] = {}
            for h in verified:
                host = h.identifier.split("/", 1)[0]
                if h.job_count > primary.get(host, h).job_count or host not in primary:
                    primary[host] = h

            mains = [h for h in verified if primary.get(h.identifier.split("/", 1)[0]) is h]
            extras = [h for h in verified if h not in mains]

            print("Identity confirmed by the employer's own domain — set "
                  "robots_allowed/tos_flag, then commit:\n")
            for h in sorted(mains, key=lambda h: -h.job_count):
                print(h.registry_row())

            if extras:
                if args.all:
                    print("\n  Secondary boards on the same tenants:\n")
                    for h in sorted(extras, key=lambda h: -h.job_count):
                        print(h.registry_row())
                else:
                    print(f"\n  ({len(extras)} secondary boards on the same tenants "
                          f"omitted — mostly event, early-careers and affinity")
                    print("   sites that re-list the main board. Pass --all to see them.)")

        if unverified:
            print("\n" + "-" * 70)
            print("NEEDS A HUMAN. These are board tokens on a shared host, which")
            print("anyone can register — resolving proves the token exists, not")
            print("that it belongs to this company. Check the sample posting.\n")
            for h in sorted(unverified, key=lambda h: -h.job_count):
                print(h.registry_row())
                print(f"        sample: {h.sample or '(none)'}\n")


if __name__ == "__main__":
    main()
