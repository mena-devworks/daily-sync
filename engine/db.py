"""D1 access over the Cloudflare REST API (same token the deploy workflow uses)."""
import os, time, requests

API = "https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database"
DB_NAME = "daily-sync-db"


class D1:
    def __init__(self):
        self.acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"]
        r = self.s.get(API.format(acct=self.acct), params={"name": DB_NAME}, timeout=30).json()
        dbs = [d for d in r.get("result") or [] if d.get("name") == DB_NAME]
        if not dbs:
            raise SystemExit("D1 database not found: " + str(r.get("errors")))
        self.url = API.format(acct=self.acct) + "/" + dbs[0]["uuid"] + "/query"

    def q(self, sql, *params):
        for attempt in range(4):
            try:
                r = self.s.post(self.url, json={"sql": sql, "params": list(params)}, timeout=60)
                j = r.json()
                if j.get("success"):
                    return j["result"][0].get("results") or []
                err = str(j.get("errors"))
            except Exception as e:  # network hiccup
                err = str(e)
            if attempt == 3 or "SQLITE" in err or "constraint" in err.lower():
                raise RuntimeError(f"D1 error: {err} | {sql[:120]}")
            time.sleep(2 * (attempt + 1))

    def one(self, sql, *params):
        r = self.q(sql, *params)
        return r[0] if r else None

    def settings(self, defaults):
        s = dict(defaults)
        for row in self.q("SELECT key, value FROM settings"):
            s[row["key"]] = row["value"]
        return s

    def set_setting(self, key, value):
        self.q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, str(value))
