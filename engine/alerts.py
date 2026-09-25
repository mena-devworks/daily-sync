"""Job-alert emails on the central mailbox -> jobs rows.

IMAP (Gmail, same CENTRAL_APP_PASSWORD) -> one parser per site -> (title, company, city, url, HR email)
-> same fp as JobSpy rows -> Gmail label "jh-processed" so a message is read once.
"""
import email, imaplib, os, re
from email.header import decode_header, make_header
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .sources import fingerprint, pick_email, excluded

LABEL = "jh-processed"
DAYS = 7
# site -> (sender domain, job-link regex)
SITES = {
    "bayt": ("bayt.com", r"bayt\.com/(?:en|ar)/[a-z-]+/jobs/[\w%-]+-\d+"),
    "naukrigulf": ("naukrigulf.com", r"naukrigulf\.com/[\w%-]+-jid-\d+"),
    "gulftalent": ("gulftalent.com", r"gulftalent\.com/(?:[\w-]+/)*jobs/[\w%-]+-\d+"),
    "dubizzle": ("dubizzle.com", r"dubizzle\.com/jobs/[\w%/-]+"),
    "wuzzuf": ("wuzzuf.net", r"wuzzuf\.net/jobs/p/[\w%-]+"),
    "tanqeeb": ("tanqeeb.com", r"tanqeeb\.com/(?:[\w-]+/)*jobs?/[\w%/-]*\d+"),
}

CITIES = {
    "AE": ["Abu Dhabi", "Dubai", "Sharjah", "Ajman", "Umm Al Quwain", "Ras Al Khaimah", "Fujairah"],
    "SA": ["Riyadh", "Jeddah", "Mecca", "Medina", "Dammam", "Khobar", "Dhahran", "Jubail", "Al Ahsa", "Taif", "Tabuk",
           "Abha", "Khamis Mushait", "Buraidah", "Hail", "Yanbu", "Jazan", "Najran"],
    "EG": ["Cairo", "Giza", "Alexandria", "Port Said", "Ismailia", "Suez", "Luxor", "Aswan", "Hurghada", "Sharm El Sheikh"],
}
ALIAS = {"makkah": "Mecca", "madinah": "Medina", "al khobar": "Khobar", "alkhobar": "Khobar", "abudhabi": "Abu Dhabi",
         "rak": "Ras Al Khaimah", "alexandria": "Alexandria", "alex": "Alexandria", "new cairo": "Cairo", "6th of october": "Giza",
         "sheikh zayed": "Giza", "nasr city": "Cairo", "heliopolis": "Cairo", "maadi": "Cairo",
         "hurghada": "Red Sea", "sharm el sheikh": "South Sinai", "دبي": "Dubai", "أبوظبي": "Abu Dhabi", "الرياض": "Riyadh",
         "القاهرة": "Cairo", "الإسكندرية": "Alexandria", "جدة": "Jeddah", "الشارقة": "Sharjah"}
CITY_COUNTRY = {c: k for k, v in CITIES.items() for c in v}
CITY_COUNTRY.update({"Red Sea": "EG", "South Sinai": "EG"})

# title keywords -> field (first match wins; order = most specific first)
FIELD_RE = [
    ("Driving & Delivery", r"driver|delivery|courier|chauffeur|rider"),
    ("Reception & Front Office", r"reception|front (office|desk)|guest relation|concierge"),
    ("Tourism & Travel", r"travel|tour|ticket|reservation|visa (officer|executive)|airline|cabin crew"),
    ("Hospitality & Hotels", r"hotel|hospitality|housekeep|room attendant|guest service|bell ?(boy|man)|night auditor|resort"),
    ("Customer Service", r"customer|call cent|contact cent|client service|help ?desk|support (agent|representative|executive)|cashier"),
    ("Logistics & Supply Chain", r"logistic|supply chain|warehouse|store ?keeper|shipping|freight|inventory|dispatch|import|export"),
    ("Food & Beverage", r"waiter|waitress|chef|cook|barista|restaurant|f ?& ?b|kitchen|steward"),
    ("Sales", r"sales|business development|account (manager|executive)"),
    ("Accounting & Finance", r"account|finance|audit|payroll|bookkeep"),
    ("Administration & Secretarial", r"admin|secretar|office (assistant|manager)|data entry|coordinator"),
    ("Human Resources", r"\bhr\b|human resource|recruit|talent acq"),
    ("Marketing", r"marketing|social media|content|seo|digital"),
    ("IT & Software", r"developer|software|programmer|\bit\b|devops|engineer.*(software|web)"),
    ("Security", r"security|guard"),
    ("Retail", r"retail|shop|store (assistant|associate)|merchandis"),
    ("Healthcare & Nursing", r"nurse|nursing|doctor|physician"),
    ("Education & Teaching", r"teacher|tutor|lecturer"),
    ("Procurement", r"procure|purchas|buyer"),
    ("Real Estate", r"real estate|property|leasing"),
]
FIELD_RE = [(f, re.compile(p, re.I)) for f, p in FIELD_RE]


def field_of(title, hint="", wanted=None):
    hits = [f for f, rx in FIELD_RE if rx.search(title or "")]
    if hits:  # "Hotel Receptionist": prefer a field someone actually targets
        return next((f for f in hits if not wanted or f in wanted), hits[0])
    for f, rx in FIELD_RE:  # the alert's own name ("Job Alert Hotel ...")
        if rx.search(hint or ""):
            return f
    return None


def city_of(text):
    t = (text or "").lower()
    for a, c in ALIAS.items():
        if re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", t):
            return c
    for c in CITY_COUNTRY:
        if c.lower() in t:
            return c
    return None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def unwrap(href):
    """Tracking links often carry the real target in a query parameter."""
    for _ in range(3):
        q = parse_qs(urlparse(href).query)
        nxt = next((unquote(v[0]) for k, v in q.items() if k.lower() in ("url", "u", "redirect", "target", "dest", "link", "r") and v and v[0].startswith("http")), None)
        if not nxt:
            break
        href = nxt
    return href


def resolve(href, session=None):
    """Follow a redirect-only tracking link (network) when the target is not in the URL."""
    try:
        import requests
        r = (session or requests).head(href, allow_redirects=True, timeout=10)
        return r.url
    except Exception:
        return href


class Parser:
    """Generic: find job links for the site, then read the smallest block around each one."""
    def __init__(self, site, follow=True):
        self.site, (self.domain, rx) = site, SITES[site]
        self.rx, self.follow, self._cache = re.compile(rx, re.I), follow, {}

    def job_url(self, href):
        if not href or not href.startswith("http"):
            return None
        u = unwrap(href)
        if not self.rx.search(u) and self.follow and "unsubscribe" not in u.lower() and (href in self._cache or len(self._cache) < 80):
            u = self._cache.get(href) or self._cache.setdefault(href, resolve(href))
        m = self.rx.search(u)
        return ("https://" + (urlparse(u).netloc or "") + urlparse(u).path).rstrip("/") if m else None

    def parse(self, html, subject=""):
        soup = BeautifulSoup(html, "html.parser")
        seen, out = set(), []
        anchors = [(a, self.job_url(a.get("href", ""))) for a in soup.find_all("a")]
        for a, url in anchors:
            if not url or url in seen:
                continue
            title = clean(a.get_text(" "))
            if len(title) < 3 or re.search(r"^(apply|view|see|more|details|عرض|قدم)", title, re.I):
                # button-style link: take the title from a sibling link to the same job
                title = next((clean(b.get_text(" ")) for b, u in anchors if u == url and len(clean(b.get_text(" "))) >= 3
                              and not re.search(r"^(apply|view|see|more|details)", clean(b.get_text(" ")), re.I)), "")
            if title.startswith("http"):  # plain-text email: title from the link slug
                slug = urlparse(url).path.rstrip("/").split("/")[-1]
                title = clean(re.sub(r"-(jid-)?\d+$|-jobs?-in-.*$", "", slug).replace("-", " ")).title()
            if not title:
                continue
            seen.add(url)
            block = a
            while block.parent is not None and block.parent.name not in ("body", "[document]"):
                links = {u for b, u in ((b, self.job_url(b.get("href", ""))) for b in block.parent.find_all("a")) if u}
                if len(links) > 1:
                    break
                block = block.parent
            lines = [clean(x) for x in block.get_text("\n").split("\n")]
            lines = [x for x in lines if x and x != title and not re.match(r"^(apply|view|see|more|details|save|share)\b", x, re.I)]
            out.append(self.fields(title, lines, url, subject))
        return out

    def fields(self, title, lines, url, subject):
        text = " \n".join(lines)
        city = city_of(" ".join(lines)) or city_of(title) or city_of(subject)
        company = next((x for x in lines if len(x) < 80 and not city_of(x) and not re.search(
            r"\d+\s*(day|hour|week|month)s? ago|posted|salary|aed|sar|egp|experience|years?|yrs|\d{3,}|http", x, re.I)), None)
        return {"title": title[:200], "company": company, "city": city, "url": url, "text": text[:1500],
                "email": pick_email(text), "hint": subject}


def site_of(sender):
    s = (sender or "").lower()
    return next((k for k, (d, _) in SITES.items() if d in s), None)


def body_html(msg):
    html = text = None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/html", "text/plain") and not part.get_filename():
            data = part.get_payload(decode=True) or b""
            s = data.decode(part.get_content_charset() or "utf-8", "replace")
            if ct == "text/html" and html is None:
                html = s
            elif ct == "text/plain" and text is None:
                text = s
    if html:
        return html
    return "".join(f'<p><a href="{u}">{u}</a></p>' for u in re.findall(r"https?://\S+", text or ""))


def to_rows(items, site, wanted=None):
    rows, skipped = [], {"no_city": 0, "no_field": 0, "excluded": 0}
    for it in items:
        city = it["city"]
        if not city:
            skipped["no_city"] += 1; continue
        field = field_of(it["title"], it.get("hint"), wanted)
        if not field:
            skipped["no_field"] += 1; continue
        if excluded(it["title"] + " " + it["text"]):
            skipped["excluded"] += 1; continue
        rows.append({"fp": fingerprint(it["title"], it["company"], city), "title": it["title"], "company": it["company"],
                     "country": CITY_COUNTRY[city], "city": city, "field": field, "url": it["url"], "apply_email": it["email"],
                     "source": site, "description": it["text"], "posted_at": None})
    return rows, skipped


def collect(dry=False, log=print, user=None, password=None, wanted_fields=None):
    """Read unprocessed alert emails; return (rows, stats). Labels messages unless dry."""
    user = user or (os.environ.get("CENTRAL_EMAIL") or "").strip()
    password = (password or os.environ.get("CENTRAL_APP_PASSWORD") or "").replace(" ", "")
    stats = {"messages": 0, "jobs": 0, "by_site": {}, "skipped": {}}
    if not user or not password:
        log("  alerts: no central mailbox credentials"); return [], stats
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, password)
    rows = []
    try:
        M.select('"[Gmail]/All Mail"')
        senders = " OR ".join(d for d, _ in SITES.values())
        typ, data = M.uid("SEARCH", "X-GM-RAW", f'"from:({senders}) -label:{LABEL} newer_than:{DAYS}d"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        log(f"  alerts: {len(uids)} new messages")
        parsers = {}
        for uid in uids:
            typ, d = M.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            site = site_of(msg.get("From"))
            subject = str(make_header(decode_header(msg.get("Subject") or "")))
            try:
                items = parsers.setdefault(site, Parser(site)).parse(body_html(msg), subject) if site else []
            except Exception as e:
                log(f"  ! alert parse {site}: {type(e).__name__}: {e}"); continue  # left unlabelled -> retried next run
            r, sk = to_rows(items, site, wanted_fields)
            rows += r
            stats["messages"] += 1
            stats["by_site"][site] = stats["by_site"].get(site, 0) + len(r)
            for k, v in sk.items():
                stats["skipped"][k] = stats["skipped"].get(k, 0) + v
            log(f"  {site}: '{subject[:60]}' -> {len(items)} links, {len(r)} jobs")
            if not dry:
                M.uid("STORE", uid, "+X-GM-LABELS", LABEL)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    stats["jobs"] = len(rows)
    return rows, stats
