"""API surface tests using FastAPI's TestClient (no server needed)."""

import os
import sys
from urllib.parse import quote
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Own database, set before app.db is imported, and rebuilt every run so the
# suite is repeatable rather than passing only on a fresh checkout.
DB_PATH = ROOT / "test_api.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
if DB_PATH.exists():
    DB_PATH.unlink()

from fastapi.testclient import TestClient  # noqa: E402

import scripts.seed as seeder  # noqa: E402
from app.api.main import app  # noqa: E402

seeder.main(reset=True)

client = TestClient(app)
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'  ok  ' if cond else ' FAIL '}] {label}{(' — ' + str(detail)) if detail else ''}")


def main():
    print("\n--- Jobs listing & pagination ---")
    r = client.get("/api/jobs?per_page=10")
    d = r.json()
    check("200 on /api/jobs", r.status_code == 200)
    check("returns items", len(d["items"]) == 10, f"{len(d['items'])} items")
    check("reports total", d["total"] > 100, f"total={d['total']}")
    check("computes page count", d["pages"] == -(-d["total"] // 10))

    p1 = client.get("/api/jobs?per_page=5&page=1").json()["items"]
    p2 = client.get("/api/jobs?per_page=5&page=2").json()["items"]
    check("pages don't overlap", {j["id"] for j in p1}.isdisjoint({j["id"] for j in p2}))

    print("\n--- Sorting ---")
    newest = client.get("/api/jobs?sort=newest&per_page=20").json()["items"]
    dates = [j["posted_date"] for j in newest if j["posted_date"]]
    check("newest is descending", dates == sorted(dates, reverse=True))

    print("\n--- Filters ---")
    r = client.get("/api/jobs?department=Engineering&per_page=50").json()
    check("department filter", all(j["department"] == "Engineering" for j in r["items"]),
          f"{r['total']} eng jobs")

    r = client.get("/api/jobs?remote=true&per_page=50").json()
    check("remote filter", all(j["is_remote"] for j in r["items"]), f"{r['total']} remote")

    r = client.get("/api/jobs?seniority=Senior&seniority=Staff&per_page=50").json()
    check("multi-value seniority filter",
          all(j["seniority_level"] in ("Senior", "Staff") for j in r["items"]),
          f"{r['total']} senior/staff")

    r = client.get("/api/jobs?country=India&per_page=50").json()
    check("country filter", all(j["location_country"] == "India" for j in r["items"]),
          f"{r['total']} in India")

    r = client.get("/api/jobs?q=engineer&per_page=50").json()
    check("free-text search", r["total"] > 0, f"{r['total']} hits for 'engineer'")

    r = client.get("/api/jobs?posted_within_days=7&per_page=100").json()
    all_jobs = client.get("/api/jobs?per_page=1").json()["total"]
    check("recency filter narrows results", r["total"] <= all_jobs,
          f"{r['total']} of {all_jobs} in last 7d")

    # Pick a combination that actually has rows, so this can't pass vacuously.
    eng_remote = client.get("/api/jobs?department=Engineering&remote=true&per_page=100").json()
    lvl = eng_remote["items"][0]["seniority_level"]
    combo = client.get(
        f"/api/jobs?department=Engineering&remote=true&seniority={lvl}&per_page=50"
    ).json()
    check("combined filters compose",
          combo["total"] > 0
          and all(j["department"] == "Engineering" and j["is_remote"]
                  and j["seniority_level"] == lvl for j in combo["items"]),
          f"{combo['total']} matches for Engineering+remote+{lvl}")

    print("\n--- Facets ---")
    f = client.get("/api/jobs/facets").json()
    check("departments faceted", len(f["departments"]) > 3, f"{len(f['departments'])} depts")
    check("countries faceted", len(f["countries"]) > 3, f"{len(f['countries'])} countries")
    check("facet counts sum sanely",
          sum(x["count"] for x in f["departments"]) <= f["total_active"])
    check("remote count present", f["remote_count"] > 0, f["remote_count"])

    # Every facet value must be directly usable as a filter argument. The
    # companies facet once emitted display names while /api/jobs filtered on
    # slug, so selecting a company silently returned zero. Round-trip each
    # facet family through the filter it feeds rather than trusting the shape.
    for family, param in (("departments", "department"),
                          ("seniority_levels", "seniority"),
                          ("countries", "country"),
                          ("companies", "company")):
        top = f[family][0]
        got = client.get(f"/api/jobs?{param}={quote(str(top['value']))}").json()["total"]
        check(f"{family} facet value filters",
              got == top["count"],
              f"{top['value']!r} facet says {top['count']}, filter returns {got}")

    check("companies facet carries a display label",
          all("label" in c for c in f["companies"]),
          "company facets need label for the UI, value stays the slug")

    print("\n--- Job detail, similar, apply redirect ---")
    jid = client.get("/api/jobs?per_page=1").json()["items"][0]["id"]
    det = client.get(f"/api/jobs/{jid}").json()
    check("detail includes description", det.get("description_raw") is not None)
    check("detail has apply_url", det["apply_url"].startswith("http"))

    sim = client.get(f"/api/jobs/{jid}/similar").json()
    check("similar jobs returned", len(sim) > 0, f"{len(sim)}")
    check("similar excludes self", all(j["id"] != jid for j in sim))

    r = client.get(f"/api/jobs/{jid}/apply", follow_redirects=False)
    check("apply redirects to employer", r.status_code == 302)
    check("redirect points off-site", r.headers.get("location", "").startswith("http"))

    check("404 on missing job", client.get("/api/jobs/99999999").status_code == 404)

    print("\n--- Companies ---")
    cs = client.get("/api/companies").json()
    check("companies listed", len(cs) == len(seeder.REGISTRY), f"{len(cs)}")
    check("active_jobs counted", all(c["active_jobs"] > 0 for c in cs))
    check("sorted by volume", cs == sorted(cs, key=lambda c: -c["active_jobs"]))
    one = client.get(f"/api/companies/{cs[0]['slug']}").json()
    check("company detail", one["slug"] == cs[0]["slug"])

    print("\n--- Stats & health ---")
    st = client.get("/api/stats").json()
    check("stats populated",
          st["total_active_jobs"] > 0
          and st["total_companies"] == len(seeder.REGISTRY))
    h = client.get("/api/health").json()
    check("health reports status", h["status"] in ("ok", "degraded"), h["status"])

    print("\n--- Auth ---")
    email = "test@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": "hunter2hunter2"})
    check("signup succeeds", r.status_code == 200, r.status_code)
    token = r.json()["token"]
    check("duplicate signup rejected",
          client.post("/api/auth/signup",
                      json={"email": email, "password": "hunter2hunter2"}).status_code == 409)
    check("bad password rejected",
          client.post("/api/auth/login",
                      json={"email": email, "password": "wrongwrongwrong"}).status_code == 401)
    check("login works",
          client.post("/api/auth/login",
                      json={"email": email, "password": "hunter2hunter2"}).status_code == 200)
    check("unauth blocked on protected route",
          client.get("/api/me/saved-jobs").status_code == 401)

    H = {"Authorization": f"Bearer {token}"}

    print("\n--- Saved jobs ---")
    check("save job", client.post("/api/me/saved-jobs", json={"job_id": jid}, headers=H).status_code == 200)
    saved = client.get("/api/me/saved-jobs", headers=H).json()
    check("saved list returns job", len(saved) == 1 and saved[0]["job"]["id"] == jid)
    client.post("/api/me/saved-jobs",
                json={"job_id": jid, "application_status": "applied", "notes": "referred"},
                headers=H)
    saved = client.get("/api/me/saved-jobs", headers=H).json()
    check("status update, no duplicate", len(saved) == 1 and saved[0]["application_status"] == "applied")
    check("unsave", client.delete(f"/api/me/saved-jobs/{jid}", headers=H).status_code == 200)
    check("saved list now empty", len(client.get("/api/me/saved-jobs", headers=H).json()) == 0)

    print("\n--- Saved searches ---")
    r = client.post("/api/me/saved-searches",
                    json={"name": "Remote senior eng",
                          "criteria": {"department": ["Engineering"], "remote": True}},
                    headers=H)
    check("create saved search", r.status_code == 200)
    sid = r.json()["id"]
    check("list saved searches", len(client.get("/api/me/saved-searches", headers=H).json()) == 1)
    check("delete saved search",
          client.delete(f"/api/me/saved-searches/{sid}", headers=H).status_code == 200)

    print("\n--- Apply Assist ---")
    r = client.put("/api/me/apply-profile",
                   json={"full_name": "Test User", "phone": "+91 90000 00000",
                         "notice_period": "60 days", "years_experience": 8},
                   headers=H)
    check("profile saved", r.status_code == 200 and r.json()["full_name"] == "Test User")
    aa = client.get(f"/api/jobs/{jid}/apply-assist", headers=H).json()
    check("prefill returned", aa["prefill"]["full_name"] == "Test User")
    check("email included in prefill", aa["prefill"]["email"] == email)
    check("policy is review-and-submit-manually",
          aa["submission_policy"] == "review_and_submit_manually")
    check("no submit endpoint exists",
          client.post(f"/api/jobs/{jid}/submit-application", headers=H).status_code == 405
          or client.post(f"/api/jobs/{jid}/submit-application", headers=H).status_code == 404)

    print(f"\n{'='*62}")
    print(f"  {sum(results)}/{len(results)} API checks passed")
    print(f"{'='*62}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
