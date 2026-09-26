"""Daily engine: collect once per (field x city) -> distribute -> AI score -> tailor CV -> apply -> record.

Usage: python -m engine.main [--dry-run] [--place cloud|device] [--subscriber ID]
"""
import argparse, json, os, re, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from .db import D1
from .llm import Gemini, LLMError
from . import sources, cv as cvmod, secrets_box, emailfind, alerts
from .mailer import Mailer, compose

DEFAULTS = {"daily_apply_limit": "20", "central_daily_limit": "5", "min_match_score": "60", "email_cooldown_days": "14",
            "auto_fields_count": "5", "engine_live": "0", "min_auto_pct": "80"}
FIELDS = list(sources.QUERY)
BACKLOG_DAYS, MAX_COMBOS, SCORE_BATCH, MAX_CANDIDATES, MAX_EMAIL_CANDIDATES, WORKERS = 14, 60, 12, 100, 150, 4
OUT = os.environ.get("ENGINE_OUT", "out")
SCORE_PROMPT_V = "2"  # bump when the scoring prompt changes: low-score jobs get scored again once


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
                      "low_score": 0, "filtered": 0, "failed": 0, "held": 0, "errors": [], "preview": []}
        self.llm = None
        self.mailer = Mailer()
        self.finder = None
        self.db.q("CREATE TABLE IF NOT EXISTS company_contacts (ckey TEXT PRIMARY KEY, site TEXT, email TEXT, checked_at TEXT)")

    # ---------- helpers ----------
    def err(self, msg):
        log("! " + msg)
        self.stats["errors"].append(msg[:300])

    def progress(self, phase):
        """Live status for the dashboard while the run is going."""
        log(f"[{phase}]")
        self.stats["phase"] = phase
        if getattr(self, "run_id", None):
            try:
                self.db.q("UPDATE runs SET stats_json = ? WHERE id = ?", json.dumps({k: v for k, v in self.stats.items() if k != "preview"}, ensure_ascii=False), self.run_id)
            except Exception:
                pass

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
            "SELECT DISTINCT field, country, city FROM jobs WHERE fetched_at >= ? AND source IN ('linkedin', 'indeed', 'google')", cairo_midnight_utc())}
        todo = [c for c in combos if c not in recent][:MAX_COMBOS]
        log(f"Collect: {len(combos)} field x city combos, {len(todo)} to fetch")
        self.stats["combos"] = len(todo)
        def fetch(c):
            try:
                return c, sources.collect(*c, log=log)
            except Exception as e:
                return c, e
        done = 0
        with ThreadPoolExecutor(WORKERS) as ex:
            for fut in as_completed([ex.submit(fetch, c) for c in todo]):
                (field, country, city), rows = fut.result()
                done += 1
                if isinstance(rows, Exception):
                    self.err(f"collect {field}/{city}: {rows}"); continue
                self.store(rows)
                hints = {}
                for r in rows:
                    site = next((x for x in map(emailfind.company_site, r.get("_hints") or []) if x), None)
                    if site and r["company"]:
                        hints[company_key(r["company"], country)] = site
                items = list(hints.items())
                for i in range(0, len(items), 40):
                    chunk = items[i:i + 40]
                    self.db.q("INSERT OR IGNORE INTO company_contacts (ckey, site) VALUES " + ",".join(["(?,?)"] * len(chunk)), *[v for kv in chunk for v in kv])
                self.progress(f"collect {done}/{len(todo)}")
        # job-alert emails on the central mailbox (Bayt, Naukrigulf, GulfTalent, Dubizzle, Wuzzuf, Tanqeeb)
        try:
            rows, st = alerts.collect(dry=True, log=log,  # dry = no Gmail label until the per-site parsers are checked on real alerts (7-day window + fp dedupe)
                                      wanted_fields={c[0] for c in combos})
            wanted = {(c[1], c[2]) for c in combos}  # only cities someone targets
            rows = [r for r in rows if (r["country"], r["city"]) in wanted]
            st["kept"] = len(rows)
            self.stats["alerts"] = st
            self.store(rows)
        except Exception as e:
            self.err(f"alerts: {type(e).__name__}: {e}")

    def store(self, rows):
        cols = ["fp", "title", "company", "country", "city", "field", "url", "apply_email", "source", "description", "posted_at"]
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
        self.db.q("DELETE FROM tailored_pdfs WHERE subscriber_id = ?", sub["id"])
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
        if not self.dry or not self.db.one("SELECT 1 x FROM tailored_pdfs WHERE subscriber_id = ? AND field = ?", sub["id"], field):
            import base64
            self.db.q("INSERT INTO tailored_pdfs (subscriber_id, field, data, size) VALUES (?,?,?,?) ON CONFLICT(subscriber_id, field) DO UPDATE SET "
                      "data=excluded.data, size=excluded.size, updated_at=datetime('now')", sub["id"], field, base64.b64encode(pdf).decode(), len(pdf))
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
                      "description": (j["description"] or "")[:1000]} for j in batch]
            self.progress(f"scoring {i + len(batch)}/{len(jobs)}")
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
                "Score 0-100 how worthwhile it is to send this candidate's application to each job. Scale: "
                "80-100 same kind of role and meets the stated requirements; 60-79 related role or clearly transferable "
                "experience (e.g. hotel reservations -> customer service, travel consultant, front desk, call centre), worth applying; "
                "40-59 weak link; 0-39 a different profession or a hard requirement is missing. Score 0 if the job is only "
                "for a gender/nationality the candidate is not, or needs a licence/degree the candidate lacks. "
                "Do not lower the score only because the candidate lives in another country. Also write 'pitch': ONE sentence (max 30 words) for the "
                "application email, truthful and based only on the candidate profile (no numbers that are not in it).\n"
                f"Return JSON {{\"results\":[{{\"id\":0,\"score\":0,\"pitch\":\"\"}}]}}\n"
                f"Candidate: {json.dumps(prof, ensure_ascii=False)[:5000]}\nJobs: {json.dumps(items, ensure_ascii=False)}")

    # ---------- per subscriber ----------
    def process(self, sub):
        log(f"\n== Subscriber #{sub['id']} {sub['name']}")
        self.progress(f"subscriber #{sub['id']}")
        if not sub["cities"]:
            return self.err(f"#{sub['id']}: no enabled cities")
        base = self.base_profile(sub)
        allowed_nums = cvmod.allowed_numbers(sub["cv_text"])
        since = utc(days=-BACKLOG_DAYS)
        sql = """SELECT j.* FROM jobs j
               JOIN subscriber_fields f ON f.subscriber_id = ?1 AND f.enabled = 1 AND f.field = j.field
               JOIN subscriber_cities c ON c.subscriber_id = ?1 AND c.enabled = 1 AND c.country = j.country AND c.city = j.city
               WHERE j.fetched_at >= ?2 AND j.apply_email IS {} NULL
               AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.subscriber_id = ?1 AND a.job_id = j.id)
               ORDER BY j.fetched_at DESC LIMIT ?3"""
        jobs = self.db.q(sql.format("NOT"), sub["id"], since, MAX_EMAIL_CANDIDATES) + self.db.q(sql.format(""), sub["id"], since, MAX_CANDIDATES)
        log(f"  candidates: {len(jobs)} ({sum(1 for j in jobs if j['apply_email'])} with an HR email)")
        if not jobs:
            return
        today = utc()[:10]
        sent_today = self.db.one("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND method = 'email' AND status = 'sent' AND substr(sent_at,1,10) = ?", sub["id"], today)["n"]
        central_today = self.db.one("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND method = 'email' AND status = 'sent' AND recipient LIKE '%[central]' AND substr(sent_at,1,10) = ?", sub["id"], today)["n"]
        total, central_cap = int(self.s["daily_apply_limit"]), int(self.s["central_daily_limit"])
        # daily_apply_limit = applications per subscriber per day (auto + manual); min_auto_pct of them go by email
        pct = max(0, min(100, int(self.s.get("min_auto_pct") or 0)))
        daily = max(1, round(total * pct / 100)) if pct else total
        manual_cap = total - daily if pct else None
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
            if sources.excluded(j["title"] + " " + (j["description"] or "")) or key in seen or off_field(j["title"], sub["fields"]):
                self.record(sub, j, None, "manual", None, "filtered")
                continue
            seen.add(key)
            keep.append(j)
        scores = self.score(base, keep)
        try:
            self.site_hints([j for j in keep if scores.get(j["id"], (0, ""))[0] >= int(self.s["min_match_score"])])
        except Exception as e:
            log(f"  ! site hints: {type(e).__name__}: {e}")
        min_score = int(self.s["min_match_score"])
        cache, sent_to, manual_q = {}, set(), []
        why, hist = {}, {}  # diagnostics: what happened to jobs that have an HR email; score spread
        def note(k):
            why[k] = why.get(k, 0) + 1
        for j in sorted(keep, key=lambda j: -scores.get(j["id"], (0, ""))[0]):
            sc, pitch = scores.get(j["id"], (None, ""))
            if sc is not None:
                b = f"{sc // 20 * 20}-{sc // 20 * 20 + 19}"
                hist[b] = hist.get(b, 0) + 1
            if sc is None:
                if j["apply_email"]:
                    note("not scored")
                continue  # not scored (AI budget) -> retried next run
            if sc < min_score:
                if j["apply_email"]:
                    note(f"low score {sc // 10 * 10}s")
                self.record(sub, j, sc, "manual", None, "low_score"); continue
            pitch = cvmod._guard_numbers(pitch, allowed_nums)
            try:
                prof, pdf = self.tailored(sub, base, j["field"], cache)
            except cvmod.CVNotReady as e:
                self.err(f"#{sub['id']} {j['field']}: {e}"); continue
            to = j["apply_email"]
            checked = False  # did we look for an email for this job today?
            if not to and sent_today < daily and (app_pw or central_today < central_cap):
                checked = True
                try:
                    to = self.find_email(j)
                except Exception as e:
                    log(f"  ! email finder {j['company']}: {type(e).__name__}: {e}")
            if to and (to in sent_to or sources.BAD_EMAIL.search(to)):
                note("same HR this run" if to in sent_to else "blocked address")
                to = None  # one email per HR mailbox per run; re-check filters on stored jobs
            can_central = central_today < central_cap
            if to and self.db.one("SELECT 1 x FROM applications WHERE subscriber_id = ? AND recipient LIKE ? AND sent_at >= ? LIMIT 1",
                                  sub["id"], to + "%", cooldown_since):
                note("same HR inside cooldown")
                to = None  # same HR mailbox inside the cooldown window -> manual
            if to and sent_today < daily and (app_pw or can_central):
                subject, body = compose(sub, prof, j, pitch, to)
                sent_to.add(to)
                if self.dry:
                    self.preview(sub, j, sc, "email", to, subject, body); sent_today += 1; note("sent"); continue
                try:
                    mode = self.mailer.send(sub, app_pw, to, subject, body, pdf, f"{slug(sub['name'])}-cv.pdf", allow_central=can_central)
                    sent_today += 1
                    central_today += mode == "central"
                    self.record(sub, j, sc, "email", to + (" [central]" if mode == "central" else ""), "sent", sent=True)
                    note("sent")
                    time.sleep(3)
                except Exception as e:
                    self.err(f"#{sub['id']} send to {to}: {e}")
                    self.record(sub, j, sc, "email", to, "failed")
            elif to and sent_today >= daily:
                note("quota full")
                continue  # has an email but today's email quota is used -> stays a candidate for tomorrow
            else:
                manual_q.append((j, sc, checked))
        # the rest of the day's quota (about 20%) is the best "apply yourself" jobs; extra ones are held (not shown)
        allowed = len(manual_q)
        if manual_cap is not None:
            manual_today = self.db.one("SELECT COUNT(*) n FROM applications WHERE subscriber_id = ? AND status = 'manual' AND substr(created_at,1,10) = ?", sub["id"], today)["n"]
            allowed = max(0, manual_cap - manual_today)
        for i, (j, sc, checked) in enumerate(manual_q):
            if i >= allowed and not checked:
                continue  # never checked for an email (quota full) -> try again next run
            if i < allowed:
                if self.dry:
                    self.preview(sub, j, sc, "manual", None, None, None)
                else:
                    self.record(sub, j, sc, "manual", None, "manual")
            else:
                self.record(sub, j, sc, "manual", None, "held")
        doms = {}
        for j, _, _ in manual_q:  # good matches with no email: where do they send people to apply?
            d = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", j["url"] or "") or [None, "?"])[1])
            doms[d] = doms.get(d, 0) + 1
        top = dict(sorted(doms.items(), key=lambda kv: -kv[1])[:12])
        log(f"  email jobs: {why} | scores: {dict(sorted(hist.items()))} | no-email apply sites: {top}")
        self.stats.setdefault("diag", {})[sub["id"]] = {"email_jobs": why, "scores": hist, "apply_sites": top}
        if manual_q:
            log(f"  email today: {sent_today}/{daily} | manual listed: {min(allowed, len(manual_q))}, not listed: {max(0, len(manual_q) - allowed)}")

    def site_hints(self, jobs):
        """One AI call: official website of the companies that have no email yet (only ones it is sure about).
        The website is then checked by crawling it; an email is used only if the company publishes it there."""
        comps = {}
        for j in jobs:
            if not j["apply_email"] and j["company"]:
                comps.setdefault(company_key(j["company"], j["country"]), j)
        if not comps:
            return
        keys = list(comps)
        known = {r["ckey"] for r in self.db.q(f"SELECT ckey FROM company_contacts WHERE site IS NOT NULL AND ckey IN ({','.join('?' * len(keys))})", *keys)}
        todo = [comps[k] for k in keys if k not in known][:40]
        if not todo:
            return
        lines = "\n".join(f"{i + 1}. {j['company']} - {j['city']}, {j['country']}" for i, j in enumerate(todo))
        ans = self.ai().json(
            "Companies from job ads in the UAE, Egypt and Saudi Arabia. For each one give its official website domain "
            "(for example \"acme.ae\") ONLY if you are sure it is exactly this company; otherwise null. Never guess.\n"
            "Return a JSON object: {\"1\": \"domain or null\", ...}\n\n" + lines, temperature=0)
        added = 0
        for i, j in enumerate(todo):
            dom = (ans or {}).get(str(i + 1)) if isinstance(ans, dict) else None
            site = emailfind.company_site("https://" + re.sub(r"^https?://", "", dom.strip().lower()).split("/")[0]) if isinstance(dom, str) and "." in dom else None
            if site and emailfind.looks_like(j["company"], site):
                self.db.q("INSERT INTO company_contacts (ckey, site) VALUES (?, ?) ON CONFLICT(ckey) DO UPDATE SET site = excluded.site, checked_at = NULL",
                          company_key(j["company"], j["country"]), site)
                added += 1
        log(f"  website hints: {added}/{len(todo)} companies")

    def find_email(self, j):
        """Published email from the company's own website (cached a month per company). Never guessed."""
        if not j["company"]:
            return None
        key = company_key(j["company"], j["country"])
        row = self.db.one("SELECT site, email, checked_at FROM company_contacts WHERE ckey = ?", key)
        if row and row["checked_at"] and row["checked_at"] >= utc(days=-30) and (row["email"] or row["site"]):
            return row["email"] or None  # website already checked this month
        if self.finder is None:
            self.finder = emailfind.Finder(log=log)
        site, email = self.finder.find(j["company"], j["city"], [row["site"]] if row and row["site"] else [])
        if site is None and email is None and self.finder.left <= 0:
            return None  # budget used up: try again next run, do not cache a miss
        self.db.q("INSERT INTO company_contacts (ckey, site, email, checked_at) VALUES (?,?,?,?) ON CONFLICT(ckey) DO UPDATE SET "
                  "site = COALESCE(excluded.site, company_contacts.site), email = excluded.email, checked_at = excluded.checked_at",
                  key, site, email, utc())
        if email:
            log(f"  + email from {site}: {email} ({j['company']})")
            self.db.q("UPDATE jobs SET apply_email = ? WHERE apply_email IS NULL AND company = ? AND country = ?", email, j["company"], j["country"])
        return email

    def record(self, sub, j, score, method, recipient, status, sent=False):
        self.stats[{"sent": "sent", "manual": "manual", "low_score": "low_score", "filtered": "filtered", "held": "held"}.get(status, "failed")] += 1
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
        # a run that was cancelled or crashed never finished: close it so it does not block "Run now"
        self.db.q("UPDATE runs SET status = 'aborted', finished_at = datetime('now') WHERE status = 'running'")
        run_id = self.run_id = self.db.one("INSERT INTO runs (place, status) VALUES (?, 'running') RETURNING id", self.place)["id"]
        status = "ok"
        try:
            if self.s.get("score_prompt_v") != SCORE_PROMPT_V:
                self.db.q("DELETE FROM applications WHERE status = 'low_score'")
                self.db.set_setting("score_prompt_v", SCORE_PROMPT_V)
                log("scoring prompt changed: old low-score jobs will be scored again")
            subs = [s for s in self.subscribers() if (s.get("cv_text") or "").strip()]
            self.stats["subscribers"] = len(subs)
            log(f"Active subscribers with a CV: {len(subs)} | dry run: {self.dry}")
            for s in subs:
                try:
                    self.auto_fields(s)
                except LLMError as e:
                    self.err(f"#{s['id']} auto fields: {e}")
            self.progress("collect")
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
            if self.finder:
                self.stats["email_finder"] = self.finder.stats
            sj = json.dumps(self.stats, ensure_ascii=False)
            if len(sj) > 90000:  # keep valid JSON: trim the preview instead of cutting the string
                sj = json.dumps({**self.stats, "preview": self.stats["preview"][:5], "errors": self.stats["errors"][:50]}, ensure_ascii=False)
            self.db.q("UPDATE runs SET finished_at = datetime('now'), status = ?, stats_json = ? WHERE id = ?", status, sj, run_id)
            os.makedirs(OUT, exist_ok=True)
            json.dump(self.stats, open(os.path.join(OUT, "stats.json"), "w"), ensure_ascii=False, indent=1)
            log("\nSummary:", json.dumps({k: v for k, v in self.stats.items() if k != "preview"}, ensure_ascii=False))
        return status


def off_field(title, fields):
    """True when the title clearly belongs to fields the subscriber did not pick ("Senior Chemist" from a hotel search)."""
    hits = {f for f, rx in alerts.FIELD_RE if rx.search(title or "")}
    near = set(fields).union(*(RELATED.get(f, ()) for f in fields))
    return bool(hits) and not hits & near


RELATED = {  # a hotel candidate also fits front desk / F&B, etc. -- only clearly other fields are skipped
    "Hospitality & Hotels": {"Reception & Front Office", "Food & Beverage", "Customer Service", "Tourism & Travel"},
    "Customer Service": {"Reception & Front Office", "Sales", "Administration & Secretarial"},
    "Tourism & Travel": {"Reception & Front Office", "Sales", "Customer Service", "Hospitality & Hotels"},
    "Logistics & Supply Chain": {"Driving & Delivery", "Procurement", "Administration & Secretarial"},
    "Driving & Delivery": {"Logistics & Supply Chain"},
    "Reception & Front Office": {"Hospitality & Hotels", "Customer Service", "Administration & Secretarial"},
}


def company_key(company, country):
    return re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip() + "|" + (country or "")


def cairo_midnight_utc():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Africa/Cairo"))
    return utc(now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))


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
