"""Run every 15 min by the workflow (stdlib only): for each pending "forgot password" request, email the
subscriber a one-time set-password link (48 h) from the central mailbox, then clear the request.
Same token format as the Worker's oneTimeLink(): sha256(token) hex in `tokens`, kind='sub'."""
import hashlib, json, os, secrets, smtplib, ssl, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

SITE = "https://daily-sync.pages.dev"
acct, tok = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database"


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method="POST" if body else "GET",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


db = [d for d in call(base + "?name=daily-sync-db")["result"] if d["name"] == "daily-sync-db"][0]["uuid"]
q = lambda sql, *p: call(f"{base}/{db}/query", {"sql": sql, "params": list(p)})["result"][0]["results"]

TEXT = {
    "en": ("Set your Job Hunter password",
           "Hi {name},\n\nYou asked to reset your Job Hunter password. Open this link to choose a new one "
           "(it works once and expires in 48 hours):\n\n{link}\n\nAfter that, sign in at {site}/me with your email and the new password.\n\n"
           "If you did not ask for this, ignore this email.\n\nJob Hunter"),
    "ar": ("تعيين كلمة سر Job Hunter",
           "أهلاً {name}،\n\nطلبت تغيير كلمة السر بتاعتك في Job Hunter. افتح اللينك ده واختار كلمة سر جديدة "
           "(بيشتغل مرة واحدة وبينتهي بعد 48 ساعة):\n\n{link}\n\nبعد كده ادخل من {site}/me بالإيميل وكلمة السر الجديدة.\n\n"
           "لو ما طلبتش ده، تجاهل الإيميل.\n\nJob Hunter"),
}


def main():
    rows = q("SELECT r.subscriber_id id, s.email, s.name, s.lang FROM reset_requests r "
             "JOIN subscribers s ON s.id = r.subscriber_id WHERE s.locked = 0")
    if not rows:
        print("reset requests: none"); return
    user, pw = (os.environ.get("CENTRAL_EMAIL") or "").strip(), (os.environ.get("CENTRAL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("reset requests: central mailbox not configured"); return
    conn = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60)
    conn.login(user, pw)
    for r in rows:
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        q("DELETE FROM tokens WHERE kind = 'sub' AND user_id = ?", r["id"])
        q("INSERT INTO tokens (token_hash, kind, user_id, purpose, expires_at) VALUES (?,?,?,?,?)",
          hashlib.sha256(token.encode()).hexdigest(), "sub", r["id"], "reset", exp)
        subject, body = TEXT["ar" if r.get("lang") == "ar" else "en"]
        m = EmailMessage()
        m["Subject"], m["To"], m["From"], m["Message-ID"] = subject, r["email"], formataddr(("Job Hunter", user)), make_msgid()
        m.set_content(body.format(name=(r["name"] or "").split(" ")[0] or "there", link=f"{SITE}/me#set-password={token}", site=SITE))
        try:
            conn.send_message(m)
            q("DELETE FROM reset_requests WHERE subscriber_id = ?", r["id"])
            print(f"reset link sent to subscriber #{r['id']}")
        except Exception as e:
            print(f"! reset #{r['id']}: {str(e)[:150]}")
    conn.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block the engine run
        print(f"! resets: {type(e).__name__}: {str(e)[:200]}")
