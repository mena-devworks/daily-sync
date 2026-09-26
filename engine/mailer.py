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
    first = [f"I am currently based in {where} and ready to relocate to {job['city']}.",
             f"I live in {where} at the moment and am fully prepared to move to {job['city']}.",
             f"Although I am based in {where} now, I am ready to relocate to {job['city']}."][int(sub.get("id") or 0) % 3]
    return first + " " + VISA.get(job["country"], "")


# Several wordings: two subscribers writing to the same company never send the same text
# (variant = recipient hash + subscriber id, so up to len(GREET) subscribers all differ for one recipient).
GREET = ["Dear Hiring Team{at},", "Dear Hiring Manager,", "Hello{at_team},", "Dear Recruitment Team{at},",
         "Good day,", "Dear HR Team{at},", "Dear Sir/Madam,", "Hello Hiring Team,"]
OPEN = ["I am writing to apply for the {title} position{at} in {city}.",
        "Please consider my application for the {title} role{at} in {city}.",
        "I would like to be considered for the {title} vacancy{at} ({city}).",
        "I am interested in the {title} opening{at} in {city} and would like to apply.",
        "I am applying for the {title} position{at}, based in {city}.",
        "I was glad to see the {title} opening{at} in {city}, and I am submitting my application.",
        "Kindly accept my application for the {title} position{at} in {city}.",
        "I am reaching out to apply for the {title} job{at} in {city}."]
CV = ["Please find my CV attached.", "My CV is attached for your review.", "I have attached my CV with full details.",
      "You will find my CV attached.", "My resume is attached to this email.", "I have enclosed my CV for your consideration.",
      "Attached is my CV.", "Please see my attached CV."]
CLOSE = ["I would welcome the opportunity to discuss how I can contribute to your team.",
         "I would be glad to have an interview at your convenience.",
         "Thank you for your time; I look forward to hearing from you.",
         "I am available for an interview whenever suits you.",
         "I would appreciate the chance to discuss this role with you.",
         "Thank you for considering my application.",
         "I hope to hear from you soon regarding next steps.",
         "I would be happy to provide any further information you need."]
SIGN = ["Best regards,", "Kind regards,", "Sincerely,", "Many thanks,", "Regards,", "Yours sincerely,", "With thanks,", "Best wishes,"]
SUBJ = ["Application for {title} – {name}", "{title} application – {name}", "Applying for {title} ({city}) – {name}",
        "{name} – {title} position", "Job application: {title}", "{title} role – application from {name}",
        "Application: {title}, {city}", "{name}: application for {title}"]


def variant(sub, to):
    import hashlib
    h = int(hashlib.md5((to or "").lower().encode()).hexdigest(), 16)
    return (h + int(sub.get("id") or 0)) % len(GREET)


def compose(sub, profile, job, pitch, to=None):
    v = variant(sub, to)
    pick = lambda arr, k=0: arr[(v + k) % len(arr)]  # shifted per part: sentence combinations vary too
    co = job.get("company")
    f = dict(at=f" at {co}" if co else "", at_team=f" {co} team" if co else "", title=job["title"], city=job["city"], name=sub["name"])
    lines = [pick(GREET).format(**f), "",
             pick(OPEN, 3).format(**f) + " " + (pitch.strip() + " " if pitch else "") + pick(CV, 5), ""]
    rel = relocation_line(sub, job)
    if rel:
        lines += [rel, ""]
    lines += [pick(CLOSE, 1), "", pick(SIGN, 6),
              sub["name"], " | ".join(x for x in [profile.get("phone"), sub["email"]] if x)]
    return pick(SUBJ, 2).format(**f), "\n".join(lines)


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
