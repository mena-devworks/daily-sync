"""One-off probe (workflow input "report"): can the runner read these job sites, and do their ads carry an HR email?
For each listing page: HTTP status, job links found, then opens up to 6 job pages and counts ones with an email."""
import re, time
from urllib.parse import urljoin, urlparse

import requests

from .sources import EMAIL_RE, BAD_EMAIL

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
PAGES = [
    "https://uae.tanqeeb.com/en", "https://saudi.tanqeeb.com/en", "https://egypt.tanqeeb.com/en",
    "https://www.expatriates.com/classifieds/uae/jobs/", "https://www.expatriates.com/classifieds/saudi-arabia/jobs/",
    "https://www.mourjan.com/ae/jobs/en/", "https://www.mourjan.com/sa/jobs/en/",
    "https://www.drjobpro.com/en-ae/jobs", "https://www.laimoon.com/uae/jobs",
    "https://www.naukrigulf.com/customer-service-jobs-in-dubai", "https://www.bayt.com/en/uae/jobs/customer-service-jobs-in-dubai/",
    "https://www.gulftalent.com/uae/jobs/title/customer-service", "https://dubai.dubizzle.com/jobs/",
    "https://wuzzuf.net/search/jobs/?q=customer%20service", "https://forasna.com/",
    "https://www.dubaicareers.ae/", "https://www.gulfjobsmarket.com/", "https://www.jobzguru.com/",
]
JOBLINK = re.compile(r"(job|vacanc|wazifa|wadhifa|/ad/|classified|/[a-z0-9-]+-\d{4,})", re.I)


def emails(html, host):
    out = set()
    for e in EMAIL_RE.findall(html or ""):
        e = e.lower().strip(".")
        if BAD_EMAIL.search(e) or host.split(".")[-2] in e.split("@")[1] or re.search(r"\.(png|jpg|gif|webp|svg)$", e):
            continue
        out.add(e)
    return out


def main():
    s = requests.Session(); s.headers.update(UA)
    for url in PAGES:
        host = urlparse(url).hostname
        try:
            r = s.get(url, timeout=25)
        except Exception as e:
            print(f"{host}: ERROR {type(e).__name__}"); continue
        links = []
        for h in re.findall(r'href="([^"#]+)"', r.text):
            u = urljoin(r.url, h)
            if urlparse(u).hostname and host.split(".")[-2] in urlparse(u).hostname and JOBLINK.search(urlparse(u).path) \
                    and len(urlparse(u).path) > 12 and u not in links:
                links.append(u)
        with_mail, sample = 0, []
        for u in links[:6]:
            try:
                d = s.get(u, timeout=20); m = emails(d.text, host)
                if m:
                    with_mail += 1; sample.append(sorted(m)[0].split('@')[1])  # domain only in public logs
            except Exception:
                pass
            time.sleep(1)
        print(f"{host}: HTTP {r.status_code} {len(r.text)//1024}KB | job links {len(links)} | emails on listing {len(emails(r.text, host))} "
              f"| job pages with email {with_mail}/{min(6, len(links))} {sample[:3]} | e.g. {links[:2]}")


if __name__ == "__main__":
    main()
