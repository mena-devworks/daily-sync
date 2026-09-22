"""Daily engine: collect once per (field x city) -> distribute -> AI score -> tailor CV -> apply -> record.

Usage: python -m engine.main [--dry-run] [--place cloud|device] [--subscriber ID]
"""
import argparse, json, os, re, sys, time, traceback
from datetime import datetime, timedelta, timezone

from .db import D1
from .llm import Gemini, LLMError
from . import sources, cv as cvmod, secrets_box
from .mailer import Mailer, compose

DEFAULTS = {"daily_apply_limit": "20", "central_daily_limit": "5", "min_match_score": "60", "email_cooldown_days": "14",
            "auto_fields_count": "5", "engine_live": "0"}
FIELDS = list(sources.QUERY)
BACKLOG_DAYS, MAX_COMBOS, SCORE_BATCH, MAX_CANDIDATES = 7, 60, 8, 60
OUT = os.environ.get("ENGINE_OUT", "out")


def utc(dt=None, days=0):
    return ((dt or datetime.now(timezone.utc)) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def log(*a):
    print(*a, flush=True)


class Engine:
    def __init__(self, dry, place, only_sub=None):
        self.db = D1()
        self.s = self.db.settings(DEFAULTS)
        self.dry = dry or self.s.get("engine_live") != "1"
        self.place, self.only_sub = place, only_sub
        self.stats = {"dry_run": self.dry, "combos": 0, "jobs_new": 0, "subscribers": 0, "sent": 0, "manual": 0,
                      "low_score": 0, "filtered": 0, "failed": 0, "errors": [], "preview": []}
        self.llm = None
        self.mailer = Mailer()

    # ---------- helpers ----------
    def err(self, msg):
        log("! " + msg)
        self.stats["errors"].append(msg[:300])

    def ai(self):
        if not self.llm:
            self.llm = Gemini(max_calls=int(os.environ.get("AI_MAX_CALLS", "150")))
            log("AI models:", self.llm.models)
        return self.llm

    def subscribers(self):
        today = utc()[:10]
        sql = "SELECT * FROM subscribers WHERE locked = 0 AND sub_start <= ? AND sub_end >= ?"
        subs = self.db.q(sql + (" AND id = ?" if self.only_sub else ""), today, today, *([self.only_sub] if self.only_sub else []))
        for s in subs:
            s["cities"] = self.db.q("SELECT country, city FROM subscriber_cities WHERE subscriber_id = ? AND enabled = 1", s["id"])
            s["fields"] = [r["field"] for r in self.db.q("SELECT field FROM subscriber_fields WHERE subscriber_id = ? AND enabled = 1", s["id"])]
        return subs

    # ---------- step: AI-picked fields when the list is empty ----------
    def auto_fields(self, sub):
        if sub["fields"] or self.db.one("SELECT 1 x FROM subscriber_fields WHERE subscriber_id = ? LIMIT 1", sub["id"]):
            return
        n = int(self.s["auto_fields_count"])
        r = self.ai().json(f"Pick the {n} job fields from this list that best fit the CV. Use the exact names.\n"
                           f"List: {json.dumps(FIELDS)}\nReturn JSON {{\"fields\":[\"\"]}}\nCV:\n{(sub['cv_text'] or '')[:8000]}")
        picked = [f for f in r.get("fields", []) if f in FIELDS][:n]
        for f in picked:
            self.db.q("INSERT OR IGNORE INTO subscriber_fields (subscriber_id, field, enabled, auto_picked) VALUES (?,?,1,1)", sub["id"], f)
        sub["fields"] = picked
        log(f"  auto-picked fields for #{sub['id']}: {picked}")

    # ---------- step: shared job store ----------
    def collect(self, subs):
        combos = sorted({(f, c["country"], c["city"]) for s in subs for f in s["fields"] for c in s["cities"]})
        recent = {(r["field"], r["country"], r["city"]) for r in self.db.q(
            "SELECT DISTINCT field, country, city FROM jobs WHERE fetched_at >= ?", utc(hours_ago(20)))}
        todo = [c for c in combos if c not in recent][:MAX_COMBOS]
        log(f"Collect: {len(combos)} field x city combos, {len(todo)} to fetch")
        self.stats["combos"] = len(todo)
        cols = ["fp", "title", "company", "country", "city", "field", "url", "apply_email", "source", "description", "posted_at"]
        for field, country, city in todo:
            rows = sources.collect(field, country, city, log=log)
            before = self.db.one("SELECT COUNT(*) n FROM jobs")["n"]
            for i in range(0, len(rows), 9):  # D1 allows 100 bound params per statement
                chunk = rows[i:i + 9]
                self.db.q(f"INSERT OR IGNORE INTO jobs ({','.join(cols)}) VALUES " + ",".join(["(" + ",".join("?" * len(cols)) + ")"] * len(chunk)),
                          *[r[c] for r in chunk for c in cols])
            self.stats["jobs_new"] += self.db.one("SELECT COUNT(*) n FROM jobs")["n"] - before

    # ---------- CV ----------
    def base_profile(self, sub):
        h = cvmod.cv_hash(sub["cv_text"])
        row = self.db.one("SELECT source_hash, content_json FROM tailored_cvs WHERE subscriber_id = ? AND field = '__base__'", sub["id"])
        if row and row["source_hash"] == h:
            return json.loads(row["content_json"])
        base = cvmod.build_base(self.ai(), sub["cv_text"])
        self.db.q("DELETE FROM tailored_cvs WHERE subscriber_id = ?", sub["id"])  # CV changed -> old versions are stale
        self.db.q("INSERT INTO tailored_cvs (subscriber_id, field, source_hash, content_json) VALUES (?,?,?,?)",
                  sub["id"], "__base__", h, json.dumps(base, ensure_ascii=False))
        return base

    def tailored(self, sub, base, field, cache):
        if field in cache:
            return cache[field]
        h = cvmod.cv_hash(sub["cv_text"])
        row = self.db.one("SELECT source_hash, content_json FROM tailored_cvs WHERE subscriber_id = ? AND field = ?", sub["id"], field)
        if row and row["source_hash"] == h:
            prof = json.loads(row["content_json"])
        else:
            prof = cvmod.tailor(self.ai(), base, sub["cv_text"], field)
        pdf = cvmod.render_pdf(prof)
        key = f"tailored/{sub['id']}/{slug(field)}.pdf"
        path = os.path.join(OUT, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(pdf)
        if not row or row["source_hash"] != h:
            self.db.q("INSERT INTO tailored_cvs (subscriber_id, field, source_hash, content_json, pdf_key) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(subscriber_id, field) DO UPDATE SET source_hash=excluded.source_hash, content_json=excluded.content_json, "
                      "pdf_key=excluded.pdf_key, created_at=datetime('now')",
                      sub["id"], field, h, json.dumps(prof, ensure_ascii=False), key)
            log(f"  tailored CV: {field} (difference {prof.get('difference')})")
        cache[field] = (prof, pdf)
        return cache[field]

    # ---------- scoring ----------
    def score(self, base, jobs):
        prof = {k: base.get(k) for k in ("headline", "summary", "skills", "languages", "nationality")}
        prof["experience"] = [{k: e.get(k) for k in ("title", "company", "start", "end")} for e in base.get("experience") or []]
        prof["education"] = base.get("education")
        out = {}
        for i in range(0, len(jobs), SCORE_BATCH):
            batch = jobs[i:i + SCORE_BATCH]
            items = [{"id": j["id"], "title": j["title"], "company": j["company"], "city": j["city"],
                      "description": (j["description"] or "")[:1500]} for j in batch]
            try:
                r = self._score_call(prof, items)
            except LLMError as e:
                self.err(f"scoring stopped: {e}"); break
            for x in r.get("results", []):
                try:
                    out[int(x["id"])] = (int(x.get("score", 0)), str(x.get("pitch") or "")[:300])
                except (ValueError, TypeError, KeyError):
                    pass
        return out

    def _score_call(self, prof, items):
            return self.ai().json(
                "Score how well this candidate fits each job (0-100). Be strict: required experience, field, language and "
                "hard requirements matter. Score 0 if the job is only for a gender/nationality the candidate is not, "
                "or needs a licence/degree the candidate lacks. Also write 'pitch': ONE sentence (max 30 words) for the "
                "application email, truthful and based only on the candidate profile (no numbers that are not in it).\n"
                f"Return JSON {{\"results\":[{{\"id\":0,\"score\":0,\"pitch\":\"\"}}]}}\n"
                f"Candidate: {json.dumps(prof, ensure_ascii=False)[:5000]}\nJobs: {json.dumps(items, ensure_ascii=False)}")

    # ---------- per subscriber ----------
    def process(self, sub):
        log(f"\n== Subscriber #{sub['id']} {sub['name']}")
        if not sub["cities"]:
            return self.err(f"#{sub['id']}: no enabled cities")
        base = self.base_profile(sub)
        allowed_nums = cvmod.allowed_numbers(sub["cv_text"])
        since = utc(days=-BACKLOG_DAYS)
        jobs = self.db.q(
            """SELECT j.* FROM jobs j
               JOIN subscriber_fields f ON f.subscriber_id = ?1 AND f.enabled = 1 AND f.field = j.field
               JOIN subscriber_cities c ON c.subscriber_id = ?1 AND c.enabled = 1 AND c.country = j.country AND c.city = j.city
               WHERE j.fetched_at >= ?2 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.subscriber_id = ?1 AND a.job_id = j.id)
               ORDER BY (j.apply_email IS NOT NULL) DESC, j.fetched_at DESC LIMIT ?3""", sub["id"], since, MAX_CANDIDATES)
        log(f"  candidates: {len(jobs)}")
        if not jobs:
            return
        today = utc()[:10]
        sent_today = self.db.one("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND method = 'email' AND status = 'sent' AND substr(sent_at,1,10) = ?", sub["id"], today)["n"]
        central_today = self.db.one("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND method = 'email' AND status = 'sent' AND recipient LIKE '%[central]' AND substr(sent_at,1,10) = ?", sub["id"], today)["n"]
        daily, central_cap = int(self.s["daily_apply_limit"]), int(self.s["central_daily_limit"])
        cooldown_since = utc(days=-int(self.s["email_cooldown_days"]))
        app_pw = None
        if sub.get("app_password_enc"):
            try:
                app_pw = secrets_box.decrypt(sub["app_password_enc"])
            except Exception as e:
                self.err(f"#{sub['id']}: cannot decrypt App Password ({type(e).__name__}) - using central")

        keep, seen = [], set()
        for j in jobs:
            key = (sources.fingerprint(j["title"], j["company"], ""),)
            if sources.excluded(j["title"] + " " + (j["description"] or "")) or key in seen:
                self.record(sub, j, None, "manual", None, "filtered")
                continue
            seen.add(key)
            keep.append(j)
        scores = self.score(base, keep)
        min_score = int(self.s["min_match_score"])
        cache = {}
        for j in sorted(keep, key=lambda j: -scores.get(j["id"], (0, ""))[0]):
            sc, pitch = scores.get(j["id"], (None, ""))
            if sc is None:
                continue  # not scored (AI budget) -> retried next run
            if sc < min_score:
                self.record(sub, j, sc, "manual", None, "low_score"); continue
            pitch = cvmod._guard_numbers(pitch, allowed_nums)
            try:
                prof, pdf = self.tailored(sub, base, j["field"], cache)
            except cvmod.CVNotReady as e:
                self.err(f"#{sub['id']} {j['field']}: {e}"); continue
            to = j["apply_email"]
            can_central = central_today < central_cap
            if to and self.db.one("SELECT 1 x FROM applications WHERE subscriber_id = ? AND recipient LIKE ? AND sent_at >= ? LIMIT 1",
                                  sub["id"], to + "%", cooldown_since):
                to = None  # same HR mailbox inside the cooldown window -> manual
            if to and sent_today < daily and (app_pw or can_central):
                subject, body = compose(sub, prof, j, pitch)
                if self.dry:
                    self.preview(sub, j, sc, "email", to, subject, body); sent_today += 1; continue
                try:
                    mode = self.mailer.send(sub, app_pw, to, subject, body, pdf, f"{slug(sub['name'])}-cv.pdf", allow_central=can_central)
                    sent_today += 1
                    central_today += mode == "central"
                    self.record(sub, j, sc, "email", to + (" [central]" if mode == "central" else ""), "sent", sent=True)
                    time.sleep(3)
                except Exception as e:
                    self.err(f"#{sub['id']} send to {to}: {e}")
                    self.record(sub, j, sc, "email", to, "failed")
            else:
                if self.dry:
                    self.preview(sub, j, sc, "manual", None, None, None); continue
                self.record(sub, j, sc, "manual", None, "manual")

    def record(self, sub, j, score, method, recipient, status, sent=False):
        self.stats[{"sent": "sent", "manual": "manual", "low_score": "low_score", "filtered": "filtered"}.get(status, "failed")] += 1
        if self.dry:
            return
        self.db.q("INSERT OR IGNORE INTO applications (subscriber_id, job_id, score, field, method, recipient, status, sent_at) VALUES (?,?,?,?,?,?,?,?)",
                  sub["id"], j["id"], score, j["field"], method, recipient, status, utc() if sent else None)

    def preview(self, sub, j, sc, method, to, subject, body):
        self.stats["sent" if method == "email" else "manual"] += 1
        if len(self.stats["preview"]) < 40:
            self.stats["preview"].append({"sub": sub["id"], "job": j["id"], "title": j["title"], "company": j["company"], "city": j["city"],
                                          "score": sc, "method": method, "to": to, "subject": subject, "body": body})

    # ---------- run ----------
    def run(self):
        run_id = self.db.one("INSERT INTO runs (place, status) VALUES (?, 'running') RETURNING id", self.place)["id"]
        status = "ok"
        try:
            subs = [s for s in self.subscribers() if (s.get("cv_text") or "").strip()]
            self.stats["subscribers"] = len(subs)
            log(f"Active subscribers with a CV: {len(subs)} | dry run: {self.dry}")
            for s in subs:
                try:
                    self.auto_fields(s)
                except LLMError as e:
                    self.err(f"#{s['id']} auto fields: {e}")
            self.collect(subs)
            for s in subs:
                try:
                    self.process(s)
                except cvmod.CVNotReady as e:
                    self.err(f"#{s['id']} CV not ready: {e}")
                except LLMError as e:
                    self.err(f"#{s['id']} AI: {e}")
                except Exception as e:
                    traceback.print_exc(); self.err(f"#{s['id']}: {type(e).__name__}: {e}")
            if self.stats["errors"]:
                status = "partial"
        except Exception as e:
            traceback.print_exc(); status = "failed"; self.err(f"run: {type(e).__name__}: {e}")
        finally:
            self.mailer.close()
            if self.llm:
                self.stats["ai_calls"] = self.llm.calls
            sj = json.dumps(self.stats, ensure_ascii=False)
            if len(sj) > 90000:  # keep valid JSON: trim the preview instead of cutting the string
                sj = json.dumps({**self.stats, "preview": self.stats["preview"][:5], "errors": self.stats["errors"][:50]}, ensure_ascii=False)
            self.db.q("UPDATE runs SET finished_at = datetime('now'), status = ?, stats_json = ? WHERE id = ?", status, sj, run_id)
            os.makedirs(OUT, exist_ok=True)
            json.dump(self.stats, open(os.path.join(OUT, "stats.json"), "w"), ensure_ascii=False, indent=1)
            log("\nSummary:", json.dumps({k: v for k, v in self.stats.items() if k != "preview"}, ensure_ascii=False))
        return status


def hours_ago(h):
    return datetime.now(timezone.utc) - timedelta(hours=h)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--place", default="cloud", choices=["cloud", "device"])
    ap.add_argument("--subscriber", type=int)
    a = ap.parse_args()
    st = Engine(a.dry_run, a.place, a.subscriber).run()
    sys.exit(1 if st == "failed" else 0)
