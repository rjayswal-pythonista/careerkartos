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
cd careerkartos

gh auth login                      # browser flow, one time

gh repo create careerkartos \
  --public \
  --source . \
  --remote origin \
  --description "Indexes job listings from employer career pages and links applicants to the original posting" \
  --push
```

That creates the repo under your account and pushes in one step.

If you'd rather create the repo in the web UI first, then:

```bash
git remote add origin https://github.com/rjayswal-pythonista/careerkartos.git
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

   The cron job runs on the `starter` plan, not free — Render has no free tier
   for cron. It bills per run with a **$1/month minimum**, so a once-daily
   scrape costs about that. The database and API are still free tier.
4. Fill the variables marked `sync: false`:

| Variable | Where | Value |
|---|---|---|
| `CORS_ORIGINS` | API | Leave blank for now — you'll set it in Step 3 |
| `ANTHROPIC_API_KEY` | API + cron | Optional. Omit and normalization runs rules-only |
| `ALERT_WEBHOOK_URL` | cron | Optional. Slack/Discord webhook for scrape alerts |

`DATABASE_URL` and `JWT_SECRET` are handled by the blueprint — Render wires the
database connection string in and generates the JWT secret once, keeping it
stable across deploys and workers.

**The registry seeds itself.** `scripts/daily_run.py` seeds the company table
when it finds it empty, so the first cron run populates the database with no
manual step. Note the Shell tab is a paid Render feature and is not available
on free-tier services — nothing here needs it.

Trigger the cron job manually once from the Render dashboard
(**careerkartos-daily** → **Trigger Run**) rather than waiting for 02:00 UTC to
find out whether it works. The first run seeds 28 companies and ingests several
thousand listings; expect it to take a few minutes.

Edit `scripts/seed.py:REGISTRY` first — the rows in there are placeholders. For
each real company you add, verify `robots.txt` and Terms of Service and set
`robots_allowed` / `tos_flag` honestly. The orchestrator refuses to dispatch a
connector for any source flagged either way, and that gate only works if the
registry tells the truth.

Then trigger the cron job manually once to confirm it works end to end, rather
than waiting until 02:00 UTC to find out.

Note your API URL: `https://careerkartos-api.onrender.com`.

> Free-tier Render web services sleep after inactivity, so the first request
> after a quiet period takes ~30s. Fine for testing, not for real users.

---

## Step 2 — Vercel: frontend

The frontend in this repo is a single static `index.html` that calls the API at
a relative `/api` path. To deploy it separately from the API, it needs to know
the API's absolute URL.

**How this repo is wired** — a rewrite proxy, so no code change is needed.

`frontend/vercel.json` rewrites `/api/*` through to the Render API:

```json
{ "rewrites": [{ "source": "/api/:path*",
                 "destination": "https://careerkartos-api.onrender.com/api/:path*" }] }
```

`index.html` keeps `const API = '/api'` untouched. Because the browser only ever
talks to the Vercel origin, the requests are same-origin and **CORS never enters
the picture** — that removes the most common way this deployment breaks.

If your Render service ends up on a different hostname, update the `destination`
in `frontend/vercel.json` and redeploy.

Project settings: root directory `frontend`, framework preset **Other**.

**The path you actually want** — rewrite the frontend as Next.js.

This isn't polish. Organic search for "[Company] [Role] jobs" is the main
acquisition channel for this kind of product, and the current frontend is
client-rendered: job pages are invisible to crawlers. A Next.js rewrite gives you
server-rendered job and company pages with `JobPosting` structured data, which is
the single highest-value work remaining on this project.

That rewrite is a real chunk of work and isn't in this repo yet.

---

## Step 3 — Connect the two

With the rewrite proxy in Step 2, browser calls are same-origin, so
`CORS_ORIGINS` is **not** required for the frontend to work.

Set it anyway to close the API off from other origins:

```
CORS_ORIGINS = https://your-project.vercel.app
```

Save; Render redeploys. Leaving it blank defaults to `*`, which still works here
but leaves the API callable from any site.

Verify:

```bash
curl https://careerkartos-api.onrender.com/api/health
curl https://careerkartos-api.onrender.com/api/stats
```

---

## If the first scrape fails on the free database

The first run is the heaviest the system ever does: every listing is new, and
each row carries the full description plus `raw_payload`. OpenAI alone is ~9.7MB.
On Render's free Postgres that can surface as

```
SSL SYSCALL error: EOF detected          # the write that broke the connection
connection ... failed: Connection refused # every source after it
```

Rows are flushed in batches of 100 and the scrape runs two sources at a time to
stay inside what a small instance tolerates, so this should not recur. If it
does, lower the concurrency further on the cron service:

```
SCRAPE_CONCURRENCY = 1
```

Re-running is always safe — the pipeline is idempotent, so a partial run
completes rather than duplicating.

---

## Environment variables, full list

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string. Defaults to local SQLite |
| `JWT_SECRET` | yes in prod | Signs session tokens. Without it, one is generated per process — sessions break across workers and die on restart |
| `CORS_ORIGINS` | recommended | Comma-separated allowed origins. Defaults to `*`. Not needed for the frontend, which proxies same-origin via `frontend/vercel.json` |
| `JWT_TTL_DAYS` | no | Session lifetime, default 30 |
| `ANTHROPIC_API_KEY` | no | Enables LLM normalization fallback. Omit for rules-only |
| `ALERT_WEBHOOK_URL` | no | Scrape failure and anomaly alerts |
| `SCRAPE_CONCURRENCY` | no | Sources scraped in parallel, default 2. Raise once the database is on a paid plan |

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
