"""Normalization & enrichment — Phase 3 of the build SOP.

Cost discipline is the design constraint here. The rules engine resolves the large
majority of listings for free; the LLM is a fallback for the residue only, and its
output is constrained to a fixed taxonomy so it can't invent categories.

Order of resolution for every field:
    1. Structured value already supplied by the ATS API  (free, authoritative)
    2. Rules / lookup tables                              (free, deterministic)
    3. LLM fallback                                       (costs money, last resort)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Controlled taxonomies. The LLM is only ever allowed to choose from these.
# --------------------------------------------------------------------------

DEPARTMENTS = [
    "Engineering", "Data & Analytics", "Product", "Design", "Sales",
    "Marketing", "Customer Success", "Finance", "Legal", "People & HR",
    "Operations", "IT & Security", "Research", "Support", "Other",
]

SENIORITY_LEVELS = [
    "Intern", "Entry", "Mid", "Senior", "Staff", "Principal",
    "Manager", "Director", "VP", "Executive",
]

# Ordered: first match wins, so more specific patterns must come first.
SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("Intern",    r"\b(intern|internship|trainee|apprentice|co-?op)\b"),
    ("Executive", r"\b(chief|cto|ceo|cfo|coo|ciso|cpo|c-level|head of|president)\b"),
    ("VP",        r"\b(vp|vice[- ]president|svp|evp)\b"),
    ("Director",  r"\b(director|dir\.)\b"),
    ("Principal", r"\b(principal|distinguished|fellow|architect)\b"),
    ("Staff",     r"\b(staff|lead engineer|tech lead|team lead)\b"),
    ("Manager",   r"\b(manager|mgr|supervisor|people manager)\b"),
    ("Senior",    r"\b(senior|sr\.?|snr)\b|\b(iii|iv|v)\b|\b[3-5]\b"),
    ("Entry",     r"\b(junior|jr\.?|associate|entry|graduate|new grad|campus|fresher|l1|i)\b"),
]

DEPARTMENT_PATTERNS: list[tuple[str, str]] = [
    ("Engineering",       r"\b(engineer|developer|swe|sde|programmer|devops|sre|backend|front[- ]?end|full[- ]?stack|mobile|ios|android|platform|infrastructure|qa|test automation|embedded|firmware|architect)\b"),
    ("Data & Analytics",  r"\b(data scien|data engineer|data analyst|analytics|machine learning|ml engineer|ai engineer|bi |business intelligence|statistician)\b"),
    ("IT & Security",     r"\b(security|infosec|cyber|soc analyst|it support|sysadmin|system administrator|network engineer|identity|iam|grc|compliance engineer)\b"),
    ("Product",           r"\b(product manager|product owner|pm\b|product lead|technical program manager|tpm)\b"),
    ("Design",            r"\b(designer|design|ux|ui\b|user research|creative)\b"),
    ("Research",          r"\b(research scientist|researcher|r&d|scientist)\b"),
    ("Sales",             r"\b(sales|account executive|ae\b|business development|bdr|sdr|partnerships|revenue)\b"),
    ("Marketing",         r"\b(marketing|growth|seo|content|brand|communications|pr\b|demand gen)\b"),
    ("Customer Success",  r"\b(customer success|csm|account manager|onboarding specialist|renewals)\b"),
    ("Support",           r"\b(support engineer|technical support|help ?desk|customer support|service desk)\b"),
    ("Finance",           r"\b(finance|accountant|accounting|controller|fp&a|treasury|audit|tax|payroll)\b"),
    ("Legal",             r"\b(legal|counsel|attorney|paralegal|contracts)\b"),
    ("People & HR",       r"\b(recruit|talent|human resources|hr\b|people ops|people operations|people partner|l&d|learning &)\b"),
    ("Operations",        r"\b(operations|ops\b|supply chain|logistics|procurement|facilities|program manager|project manager)\b"),
]

# Title canonicalization — collapses vendor-specific title noise.
TITLE_CLEANUP = [
    (r"\s*[\(\[][^)\]]*(remote|hybrid|onsite|contract|full[- ]?time|part[- ]?time)[^)\]]*[\)\]]", ""),
    (r"\s*[-–—|,]\s*(remote|hybrid|onsite|wfh)\s*$", ""),
    (r"\s*\b(m/f/d|m/w/d|f/m/d|d/f/m|all genders|w/m/d)\b\s*", " "),
    (r"\s*[-–—|]\s*(req|requisition|job id)\s*#?\s*\w+\s*$", ""),
    (r"\(\s*\)|\[\s*\]", ""),          # empty brackets left by the removals above
    (r"\s*,\s*(?=,|$)", ""),           # dangling commas
    (r"\s{2,}", " "),
]

ABBREV_EXPANSIONS = {
    r"\bsr\.(?=\s)|\bsr\b\.?": "Senior", r"\bjr\.(?=\s)|\bjr\b\.?": "Junior",
    r"\bswe\b": "Software Engineer", r"\bsde\b": "Software Engineer",
    r"\bmgr\b\.?": "Manager", r"\bdir\.(?=\s)|\bdir\b\.?": "Director",
    r"\beng\b\.?": "Engineer", r"\bdev\b\.?": "Developer", r"\bacct\b\.?": "Account",
    r"\bops\b": "Operations", r"\bqa\b": "QA", r"\bui/ux\b": "UI/UX",
}

REMOTE_PATTERN = re.compile(
    r"\b(remote|work from home|wfh|distributed|anywhere|virtual|telecommute)\b", re.I
)
HYBRID_PATTERN = re.compile(r"\b(hybrid|flexible)\b", re.I)

# Multi-country regions are not cities and not countries. "Remote - EMEA" has a
# real meaning we should keep as a remote flag, without inventing a city called Emea.
REGION_TOKENS = {
    "emea", "apac", "amer", "americas", "latam", "eu", "europe", "asia",
    "asia pacific", "north america", "south america", "africa", "middle east",
    "worldwide", "global", "multiple locations", "various", "anywhere",
}

# Country/city resolution. Extend as coverage grows; LLM handles the tail.
COUNTRY_ALIASES = {
    "united states": "United States", "usa": "United States", "u.s.": "United States",
    "us": "United States", "america": "United States",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "india": "India", "germany": "Germany", "deutschland": "Germany",
    "france": "France", "canada": "Canada", "australia": "Australia",
    "singapore": "Singapore", "japan": "Japan", "netherlands": "Netherlands",
    "ireland": "Ireland", "spain": "Spain", "italy": "Italy", "poland": "Poland",
    "brazil": "Brazil", "mexico": "Mexico", "israel": "Israel", "china": "China",
    "south korea": "South Korea", "sweden": "Sweden", "switzerland": "Switzerland",
    "uae": "United Arab Emirates", "philippines": "Philippines",
}

US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks",
    "ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny",
    "nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv",
    "wi","wy","dc",
}

KNOWN_CITY_COUNTRY = {
    "bangalore": "India", "bengaluru": "India", "mumbai": "India", "pune": "India",
    "hyderabad": "India", "chennai": "India", "delhi": "India", "gurgaon": "India",
    "gurugram": "India", "noida": "India", "kolkata": "India", "ahmedabad": "India",
    "london": "United Kingdom", "manchester": "United Kingdom", "edinburgh": "United Kingdom",
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany", "münchen": "Germany",
    "paris": "France", "lyon": "France", "toronto": "Canada", "vancouver": "Canada",
    "montreal": "Canada", "sydney": "Australia", "melbourne": "Australia",
    "tokyo": "Japan", "amsterdam": "Netherlands", "dublin": "Ireland",
    "madrid": "Spain", "barcelona": "Spain", "milan": "Italy", "rome": "Italy",
    "warsaw": "Poland", "krakow": "Poland", "kraków": "Poland",
    "tel aviv": "Israel", "singapore": "Singapore", "dubai": "United Arab Emirates",
    "stockholm": "Sweden", "zurich": "Switzerland", "zürich": "Switzerland",
    "são paulo": "Brazil", "sao paulo": "Brazil", "mexico city": "Mexico",
    "seoul": "South Korea", "shanghai": "China", "beijing": "China",
    "manila": "Philippines", "san francisco": "United States", "new york": "United States",
    "seattle": "United States", "austin": "United States", "boston": "United States",
    "chicago": "United States", "denver": "United States", "atlanta": "United States",
    "los angeles": "United States", "san jose": "United States", "portland": "United States",
}


@dataclass
class NormalizedFields:
    normalized_title: str
    department: str
    seniority_level: str
    location_country: Optional[str]
    location_city: Optional[str]
    is_remote: bool
    used_llm: bool = False


# --------------------------------------------------------------------------
# Rules engine
# --------------------------------------------------------------------------

def canonicalize_title(title: str) -> str:
    t = (title or "").strip()
    for pattern, repl in TITLE_CLEANUP:
        t = re.sub(pattern, repl, t, flags=re.I)
    for pattern, repl in ABBREV_EXPANSIONS.items():
        t = re.sub(pattern, repl, t, flags=re.I)
    t = re.sub(r"\s*[-–—|]\s*$", "", t).strip()

    # Re-case when the source shouted, whispered, or mostly whispered. Acronyms
    # already in caps (QA, UX, IT) are preserved rather than flattened.
    words = t.split()
    if words:
        lower_starts = sum(1 for w in words if w[:1].islower())
        if t.isupper() or lower_starts / len(words) > 0.6:
            t = " ".join(
                w if (w.isupper() and len(w) <= 4) else w.capitalize() for w in words
            )
    return t or (title or "").strip()


LEVEL_MARKERS = {
    "i": "Entry", "1": "Entry",
    "ii": "Mid", "2": "Mid",
    "iii": "Senior", "3": "Senior",
    "iv": "Senior", "4": "Senior",
    "v": "Staff", "5": "Staff",
}


def infer_seniority(title: str, description: Optional[str] = None) -> Optional[str]:
    hay = (title or "").lower()

    # An explicit trailing level marker ("Engineer II", "Analyst 3") is the most
    # reliable signal a company gives us — it outranks both keyword and prose cues.
    marker = re.search(r"\b(i{1,3}|iv|v|[1-5])\b\s*$", hay.strip(" ,-–—"))
    if marker:
        keyword_level = next(
            (lvl for lvl, pat in SENIORITY_PATTERNS
             if lvl in ("Intern", "Manager", "Director", "VP", "Executive")
             and re.search(pat, hay, re.I)),
            None,
        )
        if not keyword_level:
            return LEVEL_MARKERS.get(marker.group(1))

    for level, pattern in SENIORITY_PATTERNS:
        if re.search(pattern, hay, re.I):
            return level
    if description:
        m = re.search(r"(\d+)\+?\s*years?\s+(of\s+)?experience", description, re.I)
        if m:
            yrs = int(m.group(1))
            # Capped at Senior deliberately. Staff and Principal describe scope and
            # influence, not tenure — a job asking for 10 years is not thereby a
            # Staff role, and promoting it would corrupt the level filter.
            if yrs >= 4:
                return "Senior"
            if yrs >= 2:
                return "Mid"
            return "Entry"
    return None


def infer_department(title: str, ats_department: Optional[str] = None) -> Optional[str]:
    # ATS-supplied department first, mapped onto our taxonomy.
    if ats_department:
        d = ats_department.lower()
        for dept, pattern in DEPARTMENT_PATTERNS:
            if re.search(pattern, d, re.I):
                return dept
        for dept in DEPARTMENTS:
            if dept.lower() in d or d in dept.lower():
                return dept
    hay = (title or "").lower()
    for dept, pattern in DEPARTMENT_PATTERNS:
        if re.search(pattern, hay, re.I):
            return dept
    return None


def parse_location(location_raw: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
    """Return (country, city, is_remote)."""
    if not location_raw:
        return None, None, False

    raw = location_raw.strip()
    is_remote = bool(REMOTE_PATTERN.search(raw))

    cleaned = REMOTE_PATTERN.sub(" ", raw)
    cleaned = HYBRID_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"[-–—|/]+", ",", cleaned)
    parts = [p.strip(" ,;()") for p in cleaned.split(",")]
    parts = [p for p in parts if p and len(p) > 1 and p.lower() not in REGION_TOKENS]

    country: Optional[str] = None
    city: Optional[str] = None

    for p in parts:
        pl = p.lower()
        if pl in COUNTRY_ALIASES:
            country = COUNTRY_ALIASES[pl]
            break

    for p in parts:
        pl = p.lower()
        if pl in KNOWN_CITY_COUNTRY:
            city = p.title()
            country = country or KNOWN_CITY_COUNTRY[pl]
            break

    if not country:
        for p in parts:
            if p.lower() in US_STATES or (len(p) == 2 and p.isupper() and p.lower() in US_STATES):
                country = "United States"
                break

    if not city and parts:
        candidate = parts[0]
        if candidate.lower() not in COUNTRY_ALIASES and len(candidate) > 2:
            city = candidate.title()

    return country, city, is_remote


def normalize_rules_only(
    title: str,
    location_raw: Optional[str],
    ats_department: Optional[str],
    description: Optional[str] = None,
) -> tuple[NormalizedFields, list[str]]:
    """Returns normalized fields plus a list of field names the rules couldn't resolve."""
    country, city, is_remote = parse_location(location_raw)
    dept = infer_department(title, ats_department)
    seniority = infer_seniority(title, description)

    gaps: list[str] = []
    if not dept:
        gaps.append("department")
    if not seniority:
        gaps.append("seniority_level")
    if not country and location_raw and not is_remote:
        gaps.append("location_country")

    return (
        NormalizedFields(
            normalized_title=canonicalize_title(title),
            department=dept or "Other",
            seniority_level=seniority or "Mid",
            location_country=country,
            location_city=city,
            is_remote=is_remote,
        ),
        gaps,
    )


# --------------------------------------------------------------------------
# LLM fallback — only invoked for the residue the rules couldn't resolve.
# --------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = f"""You classify job postings into a fixed taxonomy.

Return ONLY a JSON object. No prose, no markdown fences.

Keys and allowed values:
  "department": exactly one of {json.dumps(DEPARTMENTS)}
  "seniority_level": exactly one of {json.dumps(SENIORITY_LEVELS)}
  "location_country": the full country name, or null if it cannot be determined
  "location_city": the city name, or null
  "is_remote": true or false

If a value is genuinely indeterminate, use "Other" for department, "Mid" for
seniority_level, and null for location fields. Never invent a value outside the
allowed lists."""


class LLMNormalizer:
    """Batched, cached LLM fallback.

    Set ANTHROPIC_API_KEY to enable. When unset, the pipeline runs rules-only —
    which is a fully valid production mode, just with more 'Other'/'Mid' defaults.
    """

    def __init__(self, model: str = "claude-haiku-4-5-20251001", enabled: Optional[bool] = None):
        self.model = model
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.enabled = (self.api_key is not None) if enabled is None else enabled
        self._cache: dict[str, dict] = {}
        self.calls_made = 0

    async def classify(self, title: str, location_raw: Optional[str],
                       ats_department: Optional[str]) -> Optional[dict]:
        if not self.enabled:
            return None

        key = f"{title}|{location_raw}|{ats_department}"
        if key in self._cache:
            return self._cache[key]

        import httpx

        user_msg = (
            f"Job title: {title}\n"
            f"Location as listed: {location_raw or 'not specified'}\n"
            f"Department as listed: {ats_department or 'not specified'}"
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 300,
                        "system": LLM_SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": user_msg}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                text = "".join(
                    b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
                )
                text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                parsed = json.loads(text)

                # Hard-constrain the output — never trust the model to stay in taxonomy.
                if parsed.get("department") not in DEPARTMENTS:
                    parsed["department"] = "Other"
                if parsed.get("seniority_level") not in SENIORITY_LEVELS:
                    parsed["seniority_level"] = "Mid"

                self._cache[key] = parsed
                self.calls_made += 1
                return parsed
        except Exception as e:
            log.warning("LLM normalization failed for %r: %s", title[:50], e)
            return None


async def normalize(
    title: str,
    location_raw: Optional[str],
    ats_department: Optional[str],
    description: Optional[str] = None,
    llm: Optional[LLMNormalizer] = None,
) -> NormalizedFields:
    """Full normalization: rules first, LLM only for unresolved fields."""
    fields, gaps = normalize_rules_only(title, location_raw, ats_department, description)

    if gaps and llm and llm.enabled:
        result = await llm.classify(title, location_raw, ats_department)
        if result:
            fields.used_llm = True
            if "department" in gaps and result.get("department"):
                fields.department = result["department"]
            if "seniority_level" in gaps and result.get("seniority_level"):
                fields.seniority_level = result["seniority_level"]
            if "location_country" in gaps and result.get("location_country"):
                fields.location_country = result["location_country"]
                if not fields.location_city and result.get("location_city"):
                    fields.location_city = result["location_city"]
            if result.get("is_remote") and not fields.is_remote:
                fields.is_remote = True

    return fields
