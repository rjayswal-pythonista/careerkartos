# CareerKartos — Operating Procedure

How to run, monitor and repair this product. Architecture is in [README.md](README.md);
first-time setup is in [DEPLOY.md](DEPLOY.md). This document is about the running system.

| | |
|---|---|
| **Site** | https://careerkartos.vercel.app |
| **API** | https://careerkartos-api.onrender.com |
| **Repository** | github.com/rjayswal-pythonista/careerkartos |
| **Scrape** | `careerkartos-daily` cron, 02:00 UTC (07:30 IST) |

---

## 1. The daily check — two minutes

```bash
curl https://careerkartos-api.onrender.com/api/health
curl https://careerkartos-api.onrender.com/api/stats
```

Healthy looks like this:

```json
{"status":"ok","runs_last_24h":{"success":73},"failing_sources":[],"search_index":true}
{"total_active_jobs":11878,"total_companies":73,"last_updated":"...today..."}
```

Read it in this order:

1. **`last_updated` within 24h** — if older, the scrape is not running. Everything else is secondary.
2. **`search_index: true`** — if `false`, search still works but scans the whole table and slows as the feed grows.
3. **`failing_sources` empty** — one or two failing is normal attrition; see §4.
4. **`total_companies` matches the registry** — `grep -c "^    ('" scripts/seed.py`. A mismatch means the registry did not reach the database.

---

## 2. Routine operations

### Adding companies

1. Verify the board token returns live listings **before** committing it:

   ```bash
   # Greenhouse
   curl -s "https://boards-api.greenhouse.io/v1/boards/TOKEN/jobs" | head -c 200
   # Lever — tokens are CASE-SENSITIVE ("GoToGroup", not "gotogroup")
   curl -s "https://api.lever.co/v0/postings/TOKEN?mode=json" | head -c 200
   # Ashby
   curl -s "https://api.ashbyhq.com/posting-api/job-board/TOKEN" | head -c 200
   ```

   A wrong Lever case returns `"Document not found"` rather than an error — an easy way
   to add a silently dead source.

2. Check the employer's `robots.txt` and Terms of Service. Set `robots_allowed` and
   `tos_flag` honestly. **The orchestrator refuses to dispatch any source flagged either
   way, and that gate only protects you if the registry tells the truth.**

3. Add the row to `scripts/seed.py:REGISTRY`, commit, push.

4. Trigger the cron. The registry syncs on every run, so new companies are picked up
   automatically — look for `registry synced: N -> M companies` in the log.

**Do not add aggregators or job boards**, even when their ATS token resolves. One such
source alone would contribute several thousand listings, and indexing a reposter
contradicts the product's central claim that every listing comes from the employer.

### Deploying

- **Frontend:** push to `main` — Vercel is Git-connected and builds automatically.
  Confirm this is still true after any Vercel project change; a project created
  via `vercel deploy` is not linked to GitHub by default, and pushes silently
  build nothing when it isn't (see DEPLOY.md § Vercel for how to check).
  `cd frontend && vercel deploy --prod` remains the manual fallback.
- **Backend:** push to `main`. Render redeploys on its own.
- **Schema changes:** run `alembic upgrade head` against the production database
  **before** the new code serves traffic. `init_db()` no longer adds columns — it only
  creates missing tables, the full-text index and backfills.

  > **This step is not automatic on the free plan.** `render.yaml` declares
  > `preDeployCommand: alembic upgrade head`, but Render only runs pre-deploy commands on
  > paid instance types. On `plan: free` it is skipped silently: the new code ships, the
  > migration does not, and every query touching a new column fails with
  > `UndefinedColumn` — a total feed outage while `/api/health` still answers.
  > This happened on 2026-09-13 with `0002_jobs_repost_history`.
  >
  > Until the API is on a paid plan, after any deploy that adds a migration:
  >
  > ```bash
  > # DATABASE_URL = the External Database URL from the Render dashboard
  > DATABASE_URL='postgresql://…' alembic upgrade head
  > ```
  >
  > It is idempotent, takes under a second, and re-running it is safe. Verify with
  > `curl -s .../api/health | grep -c UndefinedColumn` — zero means the schema is current.

### Running the scrape manually

Render dashboard → `careerkartos-daily` → **Trigger Run**. The Shell tab is a paid Render
feature and is unavailable on free-tier services — nothing in this document needs it.

The scrape is fully idempotent. Re-running after a partial failure completes the work
rather than duplicating it.

---

## 3. Analytics

All tools are key-gated and inert until configured. The site runs fully without them, and
local development never reports anywhere.

### What is captured

| Event | Fires when | Answers |
|---|---|---|
| `search` | A search query is entered | Are people searching, or only browsing? |
| `filter_applied` | Sidebar checkbox or quick chip | **Is the filter sidebar discovered at all?** Carries `source` so sidebar and chip usage are distinguishable |
| `job_viewed` | A detail panel opens | Which companies, departments and seniorities attract interest |
| `apply_clicked` | The outbound apply button | **The conversion.** The product exists to produce this |
| `search_no_results` | A query or filter set returns zero | Demand the registry does not cover — direct input for which companies to add |

### Where to look

**PostHog** (app.posthog.com) — the primary tool.
- **Activity** — live event stream; use this to confirm data is arriving.
- **Funnel** — build `$pageview → filter_applied → job_viewed → apply_clicked`. This is the
  single most useful view: it shows exactly where people drop out.
- **Session replay** — watch real sessions.

**Microsoft Clarity** (clarity.microsoft.com) — heatmaps, scroll depth, dead clicks.
Needs roughly 10–20 sessions before heatmaps mean anything.

**Sentry** (sentry.io) — browser and server exceptions. Empty is the correct state.

### Configuration

Frontend keys live in the `ANALYTICS` block at the top of the script in
`frontend/index.html`; backend uses `SENTRY_DSN` on the Render API service.

PostHog runs cookieless (`persistence: 'memory'`) and Clarity sets no first-party cookie,
which keeps the site outside the EU/UK consent-banner requirement. The trade-off is that a
returning visitor counts as new. Within-session funnels, filter usage and drop-off all
still work.

Query strings are stripped from error events on both sides before they leave the process —
they carry visitors' search terms, which do not belong in a third-party error report.

### What analytics will not catch

Every significant bug this product has had returned **HTTP 200 with a well-formed body**:
facets that filtered nothing, a recency filter that matched every row, a company filter
that returned zero. No exception tracker sees those. The contract tests in `tests/` are
what catch that class of defect — run them before every deploy.

### Uptime monitoring

Point a monitor at `GET /api/health`, not `/`. It reports connector health and search-index
state, so it catches silent degradation rather than mere liveness. On a free Render plan the
regular ping also stops the service sleeping, which otherwise gives the first visitor after
a quiet period a ~30 second wait.

---

## 4. Troubleshooting

### Feed is empty or the company count is wrong

Compare the registry against the database:

```bash
grep -c "^    ('" scripts/seed.py     # what the code has
curl -s .../api/stats                 # what the database has
```

If they differ, the registry did not reach the database. Trigger the cron and look for
`registry synced: N -> M companies`. If that line is absent on a run where counts differ,
the deployed commit is older than the registry change.

### Search is slow or times out

Check `"search_index"` in `/api/health`. If `false`, the GIN index is missing and every
search scans the table — the symptom is that *common* terms time out while rare ones
return fine, because cost scales with matches.

Fix: restart the API service, which re-runs `init_db()`. The Render log will carry an
ERROR line explaining the original failure.

### Scrape fails with connection errors

```
SSL SYSCALL error: EOF detected          ← the write that broke the connection
connection ... failed: Connection refused ← every source after it
```

The database was overwhelmed. Rows are flushed in batches of 100 and two sources run in
parallel to stay within what a small instance tolerates. If it recurs, set
`SCRAPE_CONCURRENCY=1` on the cron service and re-run.

### Runs stuck showing "running"

A process killed mid-run — OOM, deploy restart, platform timeout — leaves its status row
marked `running` permanently. The next scrape reaps anything still open after 90 minutes
and records it as failed. No action needed; if the count keeps growing, the scrape is
being killed repeatedly and the database plan is the likely cause.

### A single source stops returning jobs

Expected attrition. A career page redesign or a renamed board token surfaces as that
company dropping to zero. The orchestrator alerts when a source falls below half its recent
typical volume, and again after three consecutive failures. Re-verify the token with the
commands in §2 and update or disable the row.

### Site loads but shows no jobs

The API proxy target is wrong. `frontend/vercel.json` rewrites `/api/*` to the Render
hostname; if that hostname changed, every call 404s while the page itself loads fine.

```bash
curl https://careerkartos.vercel.app/api/stats   # should match the Render URL directly
```

---

## 5. Thresholds and limits

| Setting | Value | Where |
|---|---|---|
| Scrape schedule | 02:00 UTC daily | `render.yaml` |
| Scrape concurrency | 2 sources | `SCRAPE_CONCURRENCY` |
| Insert batch size | 100 rows | `Orchestrator(batch_size=)` |
| Anomaly alert | source drops below 50% of typical volume | `anomaly_drop_threshold` |
| Failure-streak alert | 3 consecutive failures | `_check_failure_streak` |
| Run marked abandoned | still `running` after 90 minutes | `reap_orphaned_runs` |
| Stale listing sweep | unseen for 60 days | `purge_stale` |
| Non-zero exit | more than 25% of sources fail | `daily_run.py` |
| Session lifetime | 30 days | `JWT_TTL_DAYS` |

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string; defaults to local SQLite |
| `JWT_SECRET` | yes in prod | Signs session tokens. Without it one is generated per process, so sessions break across workers and die on restart |
| `CORS_ORIGINS` | recommended | Not needed for the frontend, which proxies same-origin, but closes the API to other origins |
| `SCRAPE_CONCURRENCY` | no | Sources scraped in parallel, default 2 |
| `ANTHROPIC_API_KEY` | no | Enables the LLM normalisation fallback. Omit to run rules-only, which is a valid production mode |
| `ALERT_WEBHOOK_URL` | no | Slack/Discord webhook for scrape alerts |
| `SENTRY_DSN` · `SENTRY_ENV` · `SENTRY_TRACES_RATE` | no | Server-side error tracking |

---

## 6. Before every deploy

```bash
python tests/test_e2e.py     # 21 checks — pipeline, idempotency, expiry, alerting
python tests/test_api.py     # 57 checks — API surface, filters, auth, contracts
```

Both run offline against recorded fixtures. **69 checks total.**

Regression tests here are written to fail against the original defect, not merely to pass
against the fix. When fixing a bug, add a test and confirm it fails on the unfixed code —
otherwise it pins nothing.

---

## 7. Known limits

- **Free tiers.** The free Postgres expires and caps at 1 GB; the free web service sleeps
  after inactivity, giving the first visitor a ~30 second wait. Both need paid plans before
  real traffic.
- **Coverage ceiling.** Roughly 28% of candidate ATS tokens resolve. Many large employers
  use Workday or similar platforms that publish no open job-board API, so this approach
  tops out in the low hundreds of companies, not thousands.
- **No server-side rendering.** Job pages are client-rendered and invisible to crawlers.
  Organic search is the main acquisition channel for this category, which makes a Next.js
  rewrite with `JobPosting` structured data the highest-value work outstanding.
- **No rate limiting.** Every endpoint is unmetered; a single script can exhaust the
  database connections.

---

## 8. The product boundary

This is a discovery and indexing layer. It does not accept applications, host application
forms, or submit anything on a user's behalf. Every listing links out to the employer's own
posting, and applying happens there.

That boundary is what keeps the product clear of employer Terms of Service restrictions,
and it is enforced structurally — there is no submit endpoint that could accidentally grow
one. A test asserts its absence. **Do not add one.**
