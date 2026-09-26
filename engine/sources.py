"""Job sources: LinkedIn + Indeed + Google Jobs through JobSpy (one search per field x city)."""
import hashlib, re

COUNTRY = {  # code -> (display name, JobSpy country_indeed)
    "AE": ("United Arab Emirates", "united arab emirates"),
    "EG": ("Egypt", "egypt"),
    "SA": ("Saudi Arabia", "saudi arabia"),
}

# One search phrase per field (kept short: job boards match OR-less phrases best)
QUERY = {
    "Accounting & Finance": "accountant", "Banking": "banking", "Sales": "sales executive", "Marketing": "marketing",
    "Customer Service": "customer service", "Administration & Secretarial": "administrative assistant",
    "Reception & Front Office": "receptionist", "Human Resources": "human resources", "IT & Software": "software developer",
    "Data & Analytics": "data analyst", "Civil Engineering": "civil engineer", "Mechanical Engineering": "mechanical engineer",
    "Electrical Engineering": "electrical engineer", "Construction & Site Management": "site engineer",
    "Project Management": "project manager", "Healthcare & Nursing": "nurse", "Pharmacy": "pharmacist",
    "Education & Teaching": "teacher", "Hospitality & Hotels": "hotel", "Tourism & Travel": "travel consultant",
    "Food & Beverage": "restaurant", "Retail": "retail sales associate", "Logistics & Supply Chain": "logistics",
    "Procurement": "procurement", "Real Estate": "real estate", "Graphic Design & Creative": "graphic designer",
    "Legal": "legal", "Security": "security guard", "Driving & Delivery": "driver",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"(no-?reply|donotreply|example\.|@sentry|\.png|\.jpg|privacy@|abuse@|webmaster@|support@linkedin|@indeed|@linkedin|accommodation|disability|reasonable|dataprotection|gdpr|legal@|press@|media@|investor|billing@|sales@|booking|@join\.com|@personio|@recruitcrm)", re.I)
# Only-for-others filters (added in Ali's tool after real mistakes)
EXCLUDE_TEXT = re.compile(
    r"\b(female (candidates )?only|females only|ladies only|women only|only females?|"
    r"(uae|emirati|saudi|ksa|gcc|western|european|filipino|indian) nationals? only|"
    r"only (uae|emirati|saudi|gcc|filipino|indian) nationals?|emiratis? only|saudis? only|"
    r"saudization|emiratisation|emiratization)\b", re.I)


def fingerprint(title, company, city):
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return hashlib.sha1(f"{norm(title)}|{norm(company)}|{norm(city)}".encode()).hexdigest()


def pick_email(text, company=""):
    from .emailfind import deobfuscate  # "hr [at] acme [dot] com" style addresses too
    for e in EMAIL_RE.findall(deobfuscate(text)):
        e = e.strip(".").lower()
        if BAD_EMAIL.search(e) or len(e) > 80:
            continue
        local, dom = e.split("@", 1)
        if re.search(r"-(doha|sg|uk|us|in|qa|kw|bh|om)$", local):  # other-country branches
            continue
        return e
    return None


def excluded(text):
    return bool(EXCLUDE_TEXT.search(text or ""))


SITES = ("linkedin", "indeed")  # google removed 26 Sep: 0 results from GitHub runners


def collect(field, country, city, hours_old=48, results=40, log=print):
    """Return list of dicts ready for the jobs table."""
    from jobspy import scrape_jobs  # imported lazily so tests run without it
    cname, indeed_country = COUNTRY[country]
    out = []
    q = QUERY.get(field, field)
    for site in SITES:
        n0 = len(out)
        try:
            df = scrape_jobs(site_name=[site], search_term=q, location=f"{city}, {cname}",
                             google_search_term=f"{q} jobs in {city}, {cname} since yesterday",  # Google Jobs: ads often carry the HR email
                             results_wanted=results, hours_old=hours_old, country_indeed=indeed_country,
                             linkedin_fetch_description=True, description_format="markdown", verbose=0)
        except Exception as e:
            log(f"  ! {site} {field}/{city}: {str(e)[:150]}")
            continue
        for _, r in df.iterrows():
            g = lambda k: (None if str(r.get(k)) in ("nan", "None", "NaT") else r.get(k))
            title, company, desc = g("title"), g("company"), g("description") or ""
            if not title:
                continue
            emails = g("emails")
            email_text = " ".join(emails) if isinstance(emails, (list, tuple)) else str(emails or "")
            out.append({
                "fp": fingerprint(title, company, city), "title": str(title)[:200], "company": (str(company)[:200] if company else None),
                "country": country, "city": city, "field": field, "url": g("job_url_direct") or g("job_url"),
                "apply_email": pick_email(email_text + " " + desc), "source": site,
                "description": desc[:6000], "posted_at": str(g("date_posted") or "")[:10] or None,
                "_hints": [u for u in (g("company_url_direct"), g("job_url_direct")) if u],  # company website, if known
            })
        log(f"  {site} {field} / {city}: {len(df)} ({sum(1 for r in out[n0:] if r['apply_email'])} with email)")
    return out
