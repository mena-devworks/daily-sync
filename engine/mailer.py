"""Application emails over Gmail SMTP: the subscriber's own App Password, or the central mailbox."""
import os, smtplib, ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

COUNTRY_NAME = {"AE": "the UAE", "EG": "Egypt", "SA": "Saudi Arabia"}
VISA = {
    "AE": "I can travel to the UAE on a visit visa for interviews and join as soon as the employment visa is processed.",
    "SA": "I am ready to relocate to Saudi Arabia and can join as soon as the work visa is issued.",
    "EG": "I am ready to relocate to Egypt and can join at short notice.",
}


def relocation_line(sub, job):
    cur = sub.get("current_country")
    if not cur or cur == job["country"]:
        return ""
    where = sub.get("current_city") or COUNTRY_NAME.get(cur, "abroad")
    return f"I am currently based in {where} and ready to relocate to {job['city']}. " + VISA.get(job["country"], "")


def compose(sub, profile, job, pitch):
    at = f" at {job['company']}" if job.get("company") else ""
    lines = [f"Dear Hiring Team{at},", "",
             f"I am writing to apply for the {job['title']} position{at} in {job['city']}. "
             + (pitch.strip() + " " if pitch else "") + "Please find my CV attached.", ""]
    rel = relocation_line(sub, job)
    if rel:
        lines += [rel, ""]
    lines += ["I would welcome the opportunity to discuss how I can contribute to your team.", "", "Best regards,",
              sub["name"], " | ".join(v for v in [profile.get("phone"), sub["email"]] if v)]
    return f"Application for {job['title']} – {sub['name']}", "\n".join(lines)


class Mailer:
    def __init__(self):
        self.central = (os.environ.get("CENTRAL_EMAIL") or "").strip()
        self.central_pw = (os.environ.get("CENTRAL_APP_PASSWORD") or "").replace(" ", "")
        self.conns = {}

    def _conn(self, user, pw):
        c = self.conns.get(user)
        if c:
            try:
                c.noop(); return c
            except Exception:
                pass
        c = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60)
        c.login(user, pw)
        self.conns[user] = c
        return c

    def send(self, sub, app_pw, to, subject, body, pdf, pdf_name, allow_central=True):
        """Returns the mode used ('app_password' | 'central'). Raises on failure."""
        modes = []
        if app_pw:
            modes.append(("app_password", sub["email"], app_pw))
        if allow_central and self.central and self.central_pw:
            modes.append(("central", self.central, self.central_pw))
        if not modes:
            raise RuntimeError("no sending method configured")
        last = None
        for mode, user, pw in modes:
            m = EmailMessage()
            m["Subject"], m["To"], m["Message-ID"] = subject, to, make_msgid()
            if mode == "app_password":
                m["From"] = formataddr((sub["name"], user))
            else:
                m["From"] = formataddr((f"{sub['name']} (via Job Hunter)", user))
                m["Reply-To"] = sub["email"]
                m["Cc"] = sub["email"]
            m.set_content(body)
            m.add_attachment(pdf, maintype="application", subtype="pdf", filename=pdf_name)
            try:
                self._conn(user, pw).send_message(m)
                return mode
            except smtplib.SMTPAuthenticationError as e:
                last = f"{mode} login failed"; self.conns.pop(user, None)
            except Exception as e:
                last = f"{mode}: {str(e)[:150]}"; self.conns.pop(user, None)
        raise RuntimeError(last)

    def close(self):
        for c in self.conns.values():
            try:
                c.quit()
            except Exception:
                pass
