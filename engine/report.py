"""One-off report (workflow input "report"): where do jobs without an HR email send people to apply?
Prints counts per apply-site domain for the last 14 days: all jobs, and good matches (status manual/held)."""
import re
from collections import Counter

from .db import D1


def dom(u):
    m = re.match(r"https?://([^/]+)", u or "")
    return re.sub(r"^www\.", "", m.group(1).lower()) if m else "?"


def main():
    db = D1()
    rows = db.q("SELECT j.id, j.url, j.source, j.apply_email FROM jobs j WHERE j.fetched_at >= datetime('now','-14 days')")
    good = {r["job_id"] for r in db.q("SELECT DISTINCT job_id FROM applications WHERE status IN ('manual','held','sent')")}
    all_c, good_c, src = Counter(), Counter(), Counter()
    for r in rows:
        if r["apply_email"]:
            continue
        d = dom(r["url"])
        all_c[d] += 1
        src[r["source"]] += 1
        if r["id"] in good:
            good_c[d] += 1
    print(f"jobs 14d: {len(rows)} | with email: {sum(1 for r in rows if r['apply_email'])} | without: {sum(all_c.values())} by source {dict(src)}")
    print("good matches without email, by apply site:", dict(good_c.most_common(25)))
    print("all without email, by apply site:", dict(all_c.most_common(40)))


if __name__ == "__main__":
    main()
