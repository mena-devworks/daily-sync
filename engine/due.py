"""Tiny gate run every 15 min by the workflow (stdlib only): is a run due?
Due when: the dashboard asked for "Run now" after the last run started, or it is past run_time_cloud (Cairo time)
and no cloud run started today (Cairo date). Writes run=true|false to $GITHUB_OUTPUT."""
import json, os, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
acct, tok = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database"


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method="POST" if body else "GET",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


db = [d for d in call(base + "?name=daily-sync-db")["result"] if d["name"] == "daily-sync-db"][0]["uuid"]
q = lambda sql: call(f"{base}/{db}/query", {"sql": sql})["result"][0]["results"]
s = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings WHERE key IN ('run_time_cloud','run_requested')")}
last = q("SELECT started_at FROM runs WHERE place = 'cloud' ORDER BY id DESC LIMIT 1")
last_utc = datetime.strptime(last[0]["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if last else None
now = datetime.now(CAIRO)
reason = ""
req = s.get("run_requested")
if req and (not last_utc or datetime.strptime(req, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) > last_utc):
    reason = "run now requested"
else:
    hh, mm = (s.get("run_time_cloud") or "07:00").split(":")
    due_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if now >= due_at and (not last_utc or last_utc.astimezone(CAIRO).date() < now.date()):
        reason = "daily schedule"
print("due:", reason or "no")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
    f.write(f"run={'true' if reason else 'false'}\n")
