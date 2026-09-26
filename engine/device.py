"""Runs on the owner's Windows PC (home internet is not blocked like cloud servers).

Once a day: companies with good jobs but no HR email -> web search for the company's own site -> email it publishes
-> saved to D1 (jobs.apply_email + company_contacts) -> asks the cloud engine to run, which sends the applications.
Setup: a .env file next to this repo with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (D1 edit).
Run:   python -m engine.device   (Task Scheduler every 15 min; --force runs now)
"""
import os, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else []:
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

from .db import D1  # noqa: E402
from . import emailfind  # noqa: E402

LOOKUPS, SECONDS, DAYS = 150, 1800, 14


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(*a):
    print(now(), *a, flush=True)


def ckey(company, country):
    import re
    return re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip() + "|" + (country or "")


DAILY_HOUR = 12  # PC local time (Cairo): daily run at noon, or at the first check after the PC is switched on


def main():
    db = D1()
    s = {r["key"]: r["value"] for r in db.q("SELECT key, value FROM settings WHERE key LIKE 'device_%'")}
    local = datetime.now()  # Windows clock = Cairo time
    today = local.strftime("%Y-%m-%d")
    requested = (s.get("device_run_requested") or "") > (s.get("device_last_run") or "")  # "Run on device" in the dashboard
    daily = local.hour >= DAILY_HOUR and s.get("device_last_day") != today
    if not (requested or daily or "--force" in sys.argv):
        return  # checked every 15 minutes by Windows Task Scheduler: nothing to do
    db.set_setting("device_last_run", now())
    if daily:
        db.set_setting("device_last_day", today)
    db.set_setting("device_last_result", "running…")
    try:
        work(db)
    except Exception as e:
        db.set_setting("device_last_result", f"error: {type(e).__name__}: {str(e)[:200]}")
        raise


def work(db):
    # companies whose matching jobs have no email yet and were not rejected (not scored yet, or held for lack of email)
    rows = db.q(f"""
        SELECT j.company, j.country, MIN(j.city) city, COUNT(*) n
        FROM jobs j
        JOIN subscriber_fields f ON f.enabled = 1 AND f.field = j.field
        JOIN subscriber_cities c ON c.enabled = 1 AND c.country = j.country AND c.city = j.city AND c.subscriber_id = f.subscriber_id
        WHERE j.fetched_at >= datetime('now', '-{DAYS} days') AND j.apply_email IS NULL AND j.company IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.status IN ('sent','manual','low_score','filtered'))
        GROUP BY j.company, j.country
        ORDER BY (j.country = 'AE') DESC, (j.country = 'SA') DESC, n DESC""")
    finder = emailfind.Finder(log=log, max_lookups=LOOKUPS, max_seconds=SECONDS)
    found = checked = 0
    for r in rows:
        key = ckey(r["company"], r["country"])
        c = db.one("SELECT site, email, checked_at FROM company_contacts WHERE ckey = ?", key) or {}
        if c.get("email"):
            email, site = c["email"], c.get("site")
        else:
            if c.get("checked_at") and c.get("site"):
                continue  # its website was already read and has no email
            if finder.left <= 0 or time.time() > finder.deadline:
                break
            site, email = finder.find(r["company"], r["city"], [c["site"]] if c.get("site") else [])
            checked += 1
            db.q("INSERT INTO company_contacts (ckey, site, email, checked_at) VALUES (?,?,?,?) ON CONFLICT(ckey) DO UPDATE SET "
                 "site = COALESCE(excluded.site, company_contacts.site), email = excluded.email, checked_at = excluded.checked_at",
                 key, site, email, now())
        if email:
            found += 1
            log(f"+ {r['company']} ({r['city']}): {email}  [{r['n']} jobs]")
            db.q("UPDATE jobs SET apply_email = ? WHERE apply_email IS NULL AND company = ? AND country = ?", email, r["company"], r["country"])
            # jobs held back only for lack of an email go back to the queue
            db.q("DELETE FROM applications WHERE status = 'held' AND job_id IN (SELECT id FROM jobs WHERE company = ? AND country = ?)",
                 r["company"], r["country"])
    log(f"companies: {len(rows)} | looked up: {checked} | emails: {found} | search {finder.stats}")
    db.set_setting("device_last_result", f"{found} emails / {checked} companies checked")
    if found:
        db.set_setting("run_requested", now())  # the cloud engine picks this up within 15 minutes
        log("cloud engine asked to run")


if __name__ == "__main__":
    main()
