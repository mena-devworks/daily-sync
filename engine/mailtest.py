"""Check the central mailbox: log in with the App Password and send one test email to itself."""
import os, smtplib, ssl, sys
from email.message import EmailMessage

user = (os.environ.get("CENTRAL_EMAIL") or "").strip()
pw = (os.environ.get("CENTRAL_APP_PASSWORD") or "").replace(" ", "")
if not user or not pw:
    sys.exit("CENTRAL_EMAIL / CENTRAL_APP_PASSWORD missing")
try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as c:
        c.login(user, pw)
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = f"Job Hunter <{user}>", user, "Job Hunter — central mailbox test ✅"
        m.set_content("This is an automatic test from the Job Hunter engine. The central mailbox can send emails.")
        c.send_message(m)
    print(f"OK: logged in and sent a test email to {user[:3]}***")
except smtplib.SMTPAuthenticationError as e:
    sys.exit(f"LOGIN FAILED ({e.smtp_code}): check 2-Step Verification is on and the App Password is correct")

# Subscribers' own App Passwords: log in only (nothing is sent)
try:
    from .db import D1
    from . import secrets_box
    for s in D1().q("SELECT id, email, app_password_enc FROM subscribers WHERE app_password_enc IS NOT NULL AND locked = 0"):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as c:
                c.login(s["email"], secrets_box.decrypt(s["app_password_enc"]).replace(" ", ""))
            print(f"OK: subscriber #{s['id']} App Password works ({s['email'][:3]}***)")
        except smtplib.SMTPAuthenticationError as e:
            print(f"FAILED: subscriber #{s['id']} App Password rejected by Gmail ({e.smtp_code}) - re-create it and save again")
        except Exception as e:
            print(f"FAILED: subscriber #{s['id']}: {type(e).__name__}: {e}")
except Exception as e:
    print(f"subscriber check skipped: {e}")
