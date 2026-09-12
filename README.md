# Job Aggregator

Indexes job listings from employer career pages daily and publishes them on a
searchable feed. Every listing links out to the employer's own posting.

**The product boundary, which everything else follows from:** this is a discovery
and indexing layer. It does not accept applications, host application forms, or
submit anything on a user's behalf. That boundary is what keeps the product on
the right side of employer Terms of Service, and it's enforced in the code —
there is no submit endpoint to accidentally grow one.

---

## Quick start

```bash
pip install fastapi uvicorn sqlalchemy pydantic httpx python-dateutil

python scripts/seed.py                      # registry + fixture data
uvicorn app.api.main:app --reload           # http://127.0.0.1:8000
```

`scripts/seed.py` loads fixture data so you can see the whole system working
before pointing it at live sources.

### Tests

```bash
python tests/test_e2e.py    # pipeline: insert, idempotency, expiry, alerting
python tests/test_api.py    # API surface
```

Both run offline against recorded fixtures. 67 checks total.

### Going live

1. Replace the placeholder rows in `scripts/seed.py:REGISTRY` with real companies
   and their board tokens.
2. Verify `robots.txt` and ToS for each, and set `robots_allowed` / `tos_flag`
   accordingly. The orchestrator refuses to dispatch a connector for any source
   flagged either way — that gate is not advisory.
3. Drop the fixture client so the orchestrator uses the real one:
   ```python
   Orchestrator(SessionLocal, llm=LLMNormalizer())   # no client_factory
   ```
4. Point `DATABASE_URL` at Postgres.
5. Schedule `scripts/daily_run.py` once a day.

---

## Architecture

```
career pages → connectors → normalization → diff/persist → API → frontend
                    ↑             ↑              ↑
              one per ATS    rules first,   never deletes,
              not per company  LLM last     only expires
```

### Connectors (`app/connectors/`)

One connector per ATS platform, not per company — a single Greenhouse connector
serves every company on Greenhouse by swapping the board token. Adding a company
on an already-supported ATS is a registry row, not code.

Six platforms ship working, all against **documented public job-board APIs**:
Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable. Using the
published API rather than scraping rendered HTML means no ToS grey area, stable
structured fields, immunity to career-page redesigns, and no anti-bot friction.

Politeness lives in the shared request path (`base.polite_get`), not in each
connector: per-domain throttling, `Retry-After` respect on 429, backoff on 5xx,
and a descriptive User-Agent.

To add an ATS: implement `fetch(cfg, client) -> list[RawJob]` and `register()` it.

### Normalization (`app/pipeline/normalize.py`)

Resolution order for every field, cheapest first:

1. Structured value the ATS already gave us — free, authoritative
2. Rules and lookup tables — free, deterministic
3. LLM — costs money, last resort

On fixture data the rules resolve 100% of departments with zero LLM calls. The
LLM path is opt-in via `ANTHROPIC_API_KEY`; without it the pipeline runs
rules-only, which is a valid production mode.

When the LLM is used its output is hard-constrained to the fixed taxonomy — a
returned value outside `DEPARTMENTS`/`SENIORITY_LEVELS` is discarded, not stored.
The model picks from a list; it never invents a category.

Two deliberate calibration choices worth knowing about:

- **Explicit level markers outrank everything.** "Engineer II" is Mid even if the
  description asks for 8 years. But "Manager II" stays Manager and "Intern II"
  stays Intern — a role marker beats a level marker.
- **Description-derived seniority is capped at Senior.** Staff and Principal
  describe scope and influence, not tenure. A posting asking for 10 years is not
  thereby a Staff role, and promoting it would corrupt the level filter.

### Orchestration (`app/pipeline/orchestrator.py`)

- **Per-source isolation.** One connector failing never blocks the run.
- **Jobs are never deleted.** Absent listings become `status='expired'`. Deleting
  would destroy the time-to-fill and hiring-velocity data that later analytics
  features depend on.
- **Reactivation preserves history.** A listing that returns keeps its original
  `first_seen_at`, so tenure stays honest.
- **Silent-breakage detection.** A redesign usually doesn't raise an error — it
  quietly returns zero jobs. If a source drops below half its recent typical
  volume, that fires an alert. This is the failure mode that actually bites in
  production, and it's the one a plain try/catch misses.
- **Failure streaks** alert after three consecutive failed runs.

Register alert handlers with `orch.on_alert(fn)` to route to email or Slack.

### API (`app/api/main.py`)

| Route | Purpose |
|---|---|
| `GET /api/jobs` | Filter by q, company, department, seniority, country, city, remote, posted_within_days; sort; paginate |
| `GET /api/jobs/facets` | Live filter counts for the sidebar |
| `GET /api/jobs/{id}` | Detail with full description |
| `GET /api/jobs/{id}/similar` | Ranked by how many axes match |
| `GET /api/jobs/{id}/apply` | Records the click, 302s to the employer |
| `GET /api/companies` | Companies with active-role counts |
| `GET /api/stats` | Feed-wide counts and freshness |
| `GET /api/health` | Connector health, not just process liveness |
| `POST /api/auth/signup` · `login` | PBKDF2, 200k iterations |
| `/api/me/saved-jobs` · `saved-searches` | User collections |
| `/api/me/apply-profile` | Apply Assist profile |
| `GET /api/jobs/{id}/apply-assist` | Prefill values for review |

### Frontend (`frontend/index.html`)

A scanning tool, not a marketing page — dense rows rather than cards, because the
job is triaging a lot of listings quickly.

Recency is the one place the design spends boldness, since "these roles are real
and current" is the entire product claim: every row carries a coloured edge tick
that shifts green → amber → grey with age, so the freshness of the whole feed is
readable at a glance without reading a single date.

Filters sync to the URL, so a filtered view is shareable and survives reload.

---

## Apply Assist

The profile store and prefill endpoint exist. Programmatic submission does not,
and shouldn't be added.

`GET /api/jobs/{id}/apply-assist` returns values for a browser extension to
prefill on the employer's form. The applicant reviews every field and submits it
themselves, on the employer's site. The endpoint holds no employer credentials
and has no submit path.

This is a deliberate limit, not an unfinished feature. Auto-submitting
applications means writing to employer systems on a user's behalf: it violates
most ATS Terms of Service, triggers anti-bot defences that get the *applicant*
flagged rather than considered, and produces generic applications that hurt
outcomes. Autofill-and-review keeps the speed and leaves accountability with the
person whose name is on the application.

---

## Status

**Working and tested (67 checks passing):** schema, six ATS connectors,
normalization, orchestration with diffing and alerting, full API, frontend feed
with faceted filtering and detail panel, Apply Assist profile and prefill.

**Not built** — real V2/V3 work, not oversights:
email digests · browser extension · semantic and resume-based matching ·
server-rendered SEO pages · payment tiers · mobile app · public API tiers

**Known gaps to close first:**

1. **Connectors are untested against live APIs.** They were developed in a sandbox
   that blocks those hosts, so parsing is validated against fixtures matching the
   real payload shapes. Run each connector against one real board before trusting
   it, and expect small field-level surprises.
2. **SEO.** The frontend is client-rendered. Given that organic search for
   "[Company] [Role] jobs" is the main acquisition channel for this kind of
   product, job and company pages need server-rendering plus `JobPosting`
   structured data before launch. This is the highest-value remaining work.
3. **Auth tokens are in-process.** Fine for development, but they vanish on
   restart and won't survive more than one server process. Move to signed JWTs or
   Redis before deploying.
4. **Search is `LIKE`-based.** Fine to a few thousand listings. Move to Postgres
   `tsvector` and then a dedicated search service as the corpus grows.

---

## Layout

```
app/
  models/schema.py           tables and indexes
  connectors/
    base.py                  RawJob, rate limiting, retries, registry
    ats.py                   six working ATS connectors
    fixture.py               recorded payloads for offline runs
  pipeline/
    normalize.py             taxonomy, rules engine, constrained LLM fallback
    orchestrator.py          scheduling, diffing, expiry, alerting
  api/main.py                FastAPI app
  db.py                      engine and sessions
frontend/index.html          the feed
scripts/seed.py              registry + fixture population
scripts/daily_run.py         cron entry point
tests/                       pipeline and API suites
```
