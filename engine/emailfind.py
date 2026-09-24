"""Find the email a company publishes on its OWN website (careers / contact pages). Never guessed.

Used only for matched jobs that would otherwise be "apply yourself". Results are cached per company
in the `company_contacts` table, so each company website is checked at most once a month.
"""
import html, re, time
from urllib.parse import urlparse, urljoin, parse_qs, unquote

import requests

from .sources import EMAIL_RE, BAD_EMAIL

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# job boards, ATS, social, directories: never "the company's own site"
NOT_COMPANY = re.compile(
    r"(linkedin|indeed|bayt|glassdoor|naukri|gulftalent|wuzzuf|forasna|tanqeeb|dubizzle|google|facebook|instagram|"
    r"twitter|x\.com|youtube|tiktok|wikipedia|crunchbase|zawya|bloomberg|yellowpages|yello|kompass|dnb\.com|"
    r"myworkdayjobs|workday|greenhouse|lever\.co|smartrecruiters|icims|taleo|successfactors|oraclecloud|bamboohr|"
    r"zohorecruit|zoho\.|jobvite|breezy|recruitee|workable|teamtailor|ashbyhq|jazzhr|recruiterbox|hire\.|"
    r"careers-page|jobs\.|monster|jooble|jobrapido|laimoon|drjobpro|expatriates|gulfnews|khaleejtimes|"
    r"duckduckgo|bing\.com|microsoft|apple\.com|amazon\.|github|medium\.com|blogspot|wordpress\.com|wixsite)", re.I)
HR_LOCAL = re.compile(r"(^|[._-])(hr|career|careers|job|jobs|recruit|recruitment|recruiting|talent|hiring|cv|cvs|resume|"
                      r"people|employment|vacanc|hrd|humanresources|human\.resources|apply)", re.I)
GENERIC_LOCAL = re.compile(r"^(info|contact|contactus|hello|admin|office|enquiry|enquiries|inquiry|inquiries|mail|general|"
                           r"reception|management|uae|dubai|egypt|cairo|ksa|riyadh)([._-]?\w{0,4})?$", re.I)
CO_STOP = {"llc", "l.l.c", "fze", "fzco", "fz", "dmcc", "co", "company", "group", "the", "and", "for", "of", "services",
           "service", "trading", "general", "international", "intl", "middle", "east", "est", "establishment", "ltd",
           "limited", "inc", "corp", "corporation", "holding", "holdings", "solutions", "uae", "egypt", "ksa", "saudi",
           "arabia", "dubai", "cairo", "abu", "dhabi", "riyadh", "jeddah", "sharjah", "emirates", "gulf", "global",
           "industries", "industry", "contracting", "technical", "center", "centre", "llp", "plc", "sae", "s.a.e"}
PAGES = ["", "/careers", "/career", "/contact", "/contact-us", "/jobs", "/join-us"]
MAX_PAGES, MAX_BYTES, TIMEOUT = 6, 900_000, 8

_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|@)\s*"
_DOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\.)\s*"
OBFUSCATED = re.compile(r"([A-Za-z0-9._%+-]{2,40})" + _AT + r"([A-Za-z0-9-]{2,40}(?:" + _DOT + r"[A-Za-z]{2,10}){1,3})\b", re.I)


TLDS = {"com", "net", "org", "ae", "eg", "sa", "co", "io", "info", "biz", "me", "qa", "kw", "bh", "om", "jo", "lb", "uk", "us",
        "in", "pk", "ph", "de", "fr", "it", "es", "ca", "au", "ai", "app", "tech", "online", "site", "store", "travel", "edu", "gov"}


def deobfuscate(text):
    """'hr [at] acme [dot] com' / 'hr(at)acme.com' / 'hr at acme dot com' -> 'hr@acme.com' (added next to the text)."""
    text = html.unescape(text or "")
    found = []
    for m in OBFUSCATED.finditer(text):
        raw = m.group(0)
        # plain 'at'/'dot' words need BOTH to be obfuscated, so ordinary sentences ("look at acme. Com...") never match
        plain = "@" not in raw and not re.search(r"[\[\(\{]\s*at", raw, re.I)
        if plain and not re.search(r"\s+dot\s+|[\[\(\{]\s*dot", raw, re.I):
            continue
        dom = re.sub(_DOT, ".", m.group(2), flags=re.I)
        if dom.rsplit(".", 1)[-1].lower() not in TLDS:
            continue
        found.append(f"{m.group(1)}@{dom}".lower())
    return text + ("\n" + " ".join(found) if found else "")


def emails_in(text):
    out = []
    for e in EMAIL_RE.findall(deobfuscate(text)):
        e = e.strip(".").lower()
        if e not in out and not BAD_EMAIL.search(e) and len(e) <= 80 and not re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", e):
            out.append(e)
    return out


def base_domain(host):
    host = (host or "").lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "co", "net", "org", "gov", "edu", "ac", "gob") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def company_site(url):
    """Company website root from a URL, or None if it is a job board / ATS / social site."""
    if not url:
        return None
    u = urlparse(url if "://" in url else "https://" + url)
    if not u.hostname or NOT_COMPANY.search(u.hostname):
        return None
    return f"{u.scheme or 'https'}://{u.hostname}"


def _tokens(company):
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    return [w for w in words if len(w) >= 3 and w not in CO_STOP]


def looks_like(company, site):
    """The domain must carry the company's name (or its initials) — avoids emailing a different company."""
    dom = base_domain(urlparse(site).hostname).split(".")[0]
    toks = _tokens(company)
    if not toks:
        return False
    joined = "".join(re.findall(r"[a-z0-9]+", (company or "").lower()))
    if (len(dom) >= 6 and joined.startswith(dom)) or (len(dom) >= 4 and dom in joined and len(dom) >= 0.6 * len(joined)):
        return True
    if any(len(t) >= 4 and t in dom for t in toks) or (len(toks[0]) >= 3 and dom.startswith(toks[0])):
        return True
    initials = "".join(t[0] for t in toks)
    return len(initials) >= 3 and dom.startswith(initials)


def rank(email):
    local = email.split("@")[0]
    if HR_LOCAL.search(local):
        return 0
    if GENERIC_LOCAL.match(local):
        return 1
    return 9  # a person's or a department's own mailbox (sales, support...) -> not used


class Finder:
    def __init__(self, log=print, max_lookups=80, max_seconds=900):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.log, self.left, self.deadline = log, max_lookups, time.time() + max_seconds
        self.search_fails = 0
        self.stats = {"lookups": 0, "found": 0, "search_ok": 0, "search_fail": 0}

    def _get(self, url):
        try:
            r = self.s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                return None, None
            data = r.raw.read(MAX_BYTES, decode_content=True)
            return (data.decode(r.encoding or "utf-8", "ignore") if isinstance(data, bytes) else str(data)), r.url
        except Exception:
            return None, None

    def search(self, company, city):
        """Company website from a free web search (DuckDuckGo HTML, then Bing). Only name-matching domains."""
        if self.search_fails >= 3:
            return []
        q = f"{company} {city} official website"
        out = []
        for url, pat in (("https://html.duckduckgo.com/html/?q=", r'class="result__a"[^>]*href="([^"]+)"'),
                         ("https://lite.duckduckgo.com/lite/?q=", r'class=.result-link.[^>]*href="([^"]+)"|<a rel="nofollow" href="([^"]+)"'),
                         ("https://www.bing.com/search?setlang=en&q=", r'<li class="b_algo".*?<a[^>]+href="(https?://[^"]+)"')):
            page, _ = self._get(url + requests.utils.quote(q))
            if not page:
                continue
            for href in re.findall(pat, page, re.S)[:8]:
                href = html.unescape(next((h for h in href if h), "") if isinstance(href, tuple) else href)
                if "uddg=" in href:
                    href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
                site = company_site(href)
                if site and looks_like(company, site) and site not in out:
                    out.append(site)
            if out:
                break
            time.sleep(1)
        self.stats["search_ok" if out else "search_fail"] += 1
        self.search_fails = 0 if out else self.search_fails + 1
        return out[:2]

    def crawl(self, site):
        dom = base_domain(urlparse(site).hostname)
        seen, queue, best = set(), [urljoin(site, p) for p in PAGES], None
        fetched = 0
        while queue and fetched < MAX_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            page, final = self._get(url)
            fetched += 1
            if not page:
                continue
            if final and base_domain(urlparse(final).hostname) != dom:
                continue  # redirected to another company / parking page
            if fetched == 1:  # follow the home page's own career/contact links first
                for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I)[:400]:
                    if re.search(r"career|job|vacanc|join|recruit|contact|وظائف|اتصل", href + " " + label, re.I):
                        link = urljoin(final or url, html.unescape(href))
                        if base_domain(urlparse(link).hostname) == dom and link not in seen:
                            queue.insert(0, link)
            mails = re.findall(r'mailto:([^"?>\s]+)', page, re.I)
            for e in emails_in(page + " " + " ".join(unquote(m) for m in mails)):
                if base_domain(e.split("@")[1]) != dom:
                    continue
                r = rank(e)
                if r < 9 and (best is None or r < best[0]):
                    best = (r, e)
            if best and best[0] == 0:
                break
        return best[1] if best else None

    def find(self, company, city, hints=()):
        """-> (site, email) ; either may be None. hints: URLs known to belong to the company."""
        if not company or self.left <= 0 or time.time() > self.deadline:
            return None, None
        self.left -= 1
        self.stats["lookups"] += 1
        sites = [s for s in (company_site(h) for h in hints) if s]
        if not sites:
            sites = self.search(company, city)
        for site in dict.fromkeys(sites):
            email = self.crawl(site)
            if email:
                self.stats["found"] += 1
                return site, email
        return (sites[0] if sites else None), None
