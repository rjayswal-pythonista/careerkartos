# Deploying (Option B: Vercel frontend + Render backend)

Three things get deployed, in this order. Do them in order — Vercel needs the
API's URL, and the API needs Vercel's URL for CORS, so there's one deliberate
back-and-forth at the end.

```
  Render Postgres  ←  Render API (web)  ←  Vercel (frontend)
                   ←  Render Cron (daily scrape)
```

---

## Step 0 — Push to GitHub

The repo is committed and ready. Both platforms deploy from GitHub, so this comes
first.

**You need to run these yourself** — they authenticate as you, and I can't hold
your credentials.

Install `gh` if you don't have it (`brew install gh`, or see cli.github.com),
then:

```bash
cd jobaggregator

gh auth login                      # browser flow, one time

gh repo create job-aggregator \
  --public \
  --source . \
  --remote origin \
  --description "Indexes job listings from employer career pages and links applicants to the original posting" \
  --push
```

That creates the repo under your account and pushes in one step.

If you'd rather create the repo in the web UI first, then:

```bash
git remote add origin https://github.com/rjayswal-pythonista/job-aggregator.git
git branch -M main
git push -u origin main
```

Check nothing sensitive went up before you make it public:

```bash
git log --stat        # should be one commit, ~24 files, no .db and no .env
```

---

## Step 1 — Render: database, API, cron

Render reads `render.yaml` and provisions all three together.

1. Render dashboard → **New** → **Blueprint**
2. Connect the GitHub repo you just pushed
3. Render shows a plan: one Postgres, one web service, one cron job. Approve it.
4. Fill the variables marked `sync: false`:

| Variable | Where | Value |
|---|---|---|
| `CORS_ORIGINS` | API | Leave blank for now — you'll set it in Step 3 |
| `ANTHROPIC_API_KEY` | API + cron | Optional. Omit and normalization runs rules-only |
| `ALERT_WEBHOOK_URL` | cron | Optional. Slack/Discord webhook for scrape alerts |

`DATABASE_URL` and `JWT_SECRET` are handled by the blueprint — Render wires the
database connection string in and generates the JWT secret once, keeping it
stable across deploys and workers.

**Before the first scrape, seed the company registry.** Render dashboard → API
service → **Shell**:

```bash
python -c "from scripts.seed import seed_registry; seed_registry()"
```

Edit `scripts/seed.py:REGISTRY` first — the rows in there are placeholders. For
each real company you add, verify `robots.txt` and Terms of Service and set
`robots_allowed` / `tos_flag` honestly. The orchestrator refuses to dispatch a
connector for any source flagged either way, and that gate only works if the
registry tells the truth.

Then trigger the cron job manually once to confirm it works end to end, rather
than waiting until 02:00 UTC to find out.

Note your API URL: `https://jobaggregator-api.onrender.com`.

> Free-tier Render web services sleep after inactivity, so the first request
> after a quiet period takes ~30s. Fine for testing, not for real users.

---

## Step 2 — Vercel: frontend

The frontend in this repo is a single static `index.html` that calls the API at
a relative `/api` path. To deploy it separately from the API, it needs to know
the API's absolute URL.

**Quickest path** — deploy the static file as-is:

1. Vercel → **Add New** → **Project** → import the repo
2. Root directory: `frontend`
3. Framework preset: **Other**
4. Add environment variable `NEXT_PUBLIC_API_URL` = your Render API URL
5. In `frontend/index.html`, change `const API = '/api'` to point at the Render
   URL.

**The path you actually want** — rewrite the frontend as Next.js.

This isn't polish. Organic search for "[Company] [Role] jobs" is the main
acquisition channel for this kind of product, and the current frontend is
client-rendered: job pages are invisible to crawlers. A Next.js rewrite gives you
server-rendered job and company pages with `JobPosting` structured data, which is
the single highest-value work remaining on this project.

That rewrite is a real chunk of work and isn't in this repo yet.

---

## Step 3 — Connect the two

Back in Render → API service → Environment:

```
CORS_ORIGINS = https://your-project.vercel.app
```

Save; Render redeploys. Without this the browser blocks every API call from the
Vercel domain, and the symptom looks like a broken frontend rather than a CORS
problem.

Verify:

```bash
curl https://jobaggregator-api.onrender.com/api/health
curl https://jobaggregator-api.onrender.com/api/stats
```

---

## Environment variables, full list

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string. Defaults to local SQLite |
| `JWT_SECRET` | yes in prod | Signs session tokens. Without it, one is generated per process — sessions break across workers and die on restart |
| `CORS_ORIGINS` | yes in prod | Comma-separated allowed origins. Defaults to `*` |
| `JWT_TTL_DAYS` | no | Session lifetime, default 30 |
| `ANTHROPIC_API_KEY` | no | Enables LLM normalization fallback. Omit for rules-only |
| `ALERT_WEBHOOK_URL` | no | Scrape failure and anomaly alerts |

---

## After it's live

**Watch the cron job, not the web service.** The web service failing is loud. A
connector silently returning zero jobs after a career-page redesign is quiet, and
it's the failure that actually degrades the product. `GET /api/health` reports
connector health for exactly this reason — point an uptime monitor at it rather
than at `/`.

**Move off free tiers before real traffic.** The free Postgres expires, and the
sleeping web service makes first-load latency bad enough to lose users.

**Back up the database** before you have data worth losing. `raw_payload` is
retained on every job specifically so normalization can be reprocessed without
re-scraping — that safety net is worth nothing if the database isn't backed up.
