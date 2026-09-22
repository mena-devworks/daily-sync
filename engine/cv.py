"""CV: structured base profile (checked against the original), per-field tailoring with safety rules, PDF."""
import difflib, hashlib, io, json, re

NUM_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def allowed_numbers(cv_text):
    """Numbers a rewrite may use: those in the CV, ignoring phone numbers."""
    return set(NUM_RE.findall(PHONE_RE.sub(" ", cv_text or "")))


class CVNotReady(Exception):
    pass


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def cv_hash(text):
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


BASE_PROMPT = """Extract this CV into JSON. Copy facts EXACTLY as written (names, companies, job titles, dates, degrees).
Do not invent anything. Unknown -> empty string / empty list.
Schema: {"name":"","email":"","phone":"","location":"","nationality":"","headline":"","summary":"",
"skills":[""],"languages":[""],"certifications":[""],
"experience":[{"title":"","company":"","location":"","start":"","end":"","bullets":[""]}],
"education":[{"degree":"","school":"","start":"","end":""}]}
CV:
<<<
%s
>>>"""


def build_base(llm, cv_text):
    if not cv_text or len(cv_text.strip()) < 200:
        raise CVNotReady("CV text missing or too short")
    base = llm.json(BASE_PROMPT % cv_text[:15000], temperature=0)
    src = norm(cv_text)
    problems = []
    if not base.get("name") or norm(base["name"]).split()[0] not in src:
        problems.append("name")
    for e in base.get("experience") or []:
        for k in ("company", "title"):
            if e.get(k) and norm(e[k]) not in src:
                problems.append(f"{k}: {e[k]}")
        for k in ("start", "end"):
            if e.get(k) and not all(n in cv_text for n in NUM_RE.findall(e[k])):
                problems.append(f"date: {e[k]}")
    for e in base.get("education") or []:
        if e.get("school") and norm(e["school"]) not in src:
            problems.append(f"school: {e['school']}")
    if not (base.get("experience") or base.get("education")):
        problems.append("no experience/education found")
    if problems:
        raise CVNotReady("CV read check failed: " + "; ".join(problems[:5]))
    return base


TAILOR_PROMPT = """You tailor a CV for jobs in the field "%s". Rewrite ONLY: headline, summary (3-4 lines),
strengths (4-6 short points), the ORDER of skills (most relevant first), and the wording of each experience's bullets
to highlight what matters for this field.
HARD RULES: never add employers, job titles, dates, degrees, certifications, tools, skills or numbers that are not in
the CV. Never claim experience the person does not have. Keep the same number of experience entries, in the same order.
Keep it truthful and professional English. %s
Return JSON: {"headline":"","summary":"","strengths":[""],"skills":[""],"bullets":[[""]]}
("bullets" = one list per experience entry, same order.)
CV JSON:
%s"""


def _guard_numbers(text, allowed):
    """Drop sentences that contain a number not present in the original CV."""
    parts = re.split(r"(?<=[.;!])\s+", text or "")
    keep = [p for p in parts if all(n in allowed for n in NUM_RE.findall(p))]
    return " ".join(keep).strip()


def _blob(p):
    return " ".join([p.get("headline", ""), p.get("summary", ""), " ".join(p.get("strengths") or []),
                     " ".join(b for e in p.get("experience") or [] for b in e.get("bullets") or [])])


def tailor(llm, base, cv_text, field, min_difference=0.25, attempts=2):
    allowed = allowed_numbers(cv_text)
    base_skills = {norm(s): s for s in base.get("skills") or []}
    best, best_diff = None, -1
    for i in range(attempts):
        extra = "" if i == 0 else "The previous version was too close to the original: rewrite more strongly for this field."
        t = llm.json(TAILOR_PROMPT % (field, extra, json.dumps(base, ensure_ascii=False)[:14000]), temperature=0.5 + 0.2 * i)
        p = json.loads(json.dumps(base))  # facts always come from the base, never from the AI
        p["field"] = field
        p["headline"] = _guard_numbers(t.get("headline") or base.get("headline", ""), allowed)[:120]
        p["summary"] = _guard_numbers(t.get("summary") or base.get("summary", ""), allowed)
        p["strengths"] = [s for s in (_guard_numbers(x, allowed) for x in (t.get("strengths") or [])[:6]) if s]
        order = [base_skills[norm(s)] for s in t.get("skills") or [] if norm(s) in base_skills]
        p["skills"] = list(dict.fromkeys(order + list(base_skills.values())))
        for e, nb in zip(p.get("experience") or [], t.get("bullets") or []):
            nb = [b for b in (_guard_numbers(x, allowed) for x in nb or []) if b]
            if nb:
                e["bullets"] = nb[:8]
        diff = 1 - difflib.SequenceMatcher(None, _blob(base), _blob(p)).ratio()
        if diff > best_diff:
            best, best_diff = p, diff
        if diff >= min_difference:
            break
    best["difference"] = round(best_diff, 2)
    return best


# ---------------- PDF ----------------
def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    try:
        pdfmetrics.registerFont(TTFont("DV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont("DVB", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
        return "DV", "DVB"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def render_pdf(p):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from xml.sax.saxutils import escape as x
    f, fb = _fonts()
    st = {
        "name": ParagraphStyle("n", fontName=fb, fontSize=18, leading=22),
        "head": ParagraphStyle("h", fontName=f, fontSize=11, leading=14, textColor=colors.HexColor("#1f4e79")),
        "sec": ParagraphStyle("s", fontName=fb, fontSize=11, leading=14, spaceBefore=8, spaceAfter=3, textColor=colors.HexColor("#1f4e79")),
        "b": ParagraphStyle("b", fontName=f, fontSize=9.5, leading=12.5),
        "bb": ParagraphStyle("bb", fontName=fb, fontSize=10, leading=13),
    }
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"{p.get('name','')} - CV", author=p.get("name", ""))
    E = [Paragraph(x(p.get("name", "")), st["name"])]
    if p.get("headline"):
        E.append(Paragraph(x(p["headline"]), st["head"]))
    contact = " | ".join(v for v in [p.get("email"), p.get("phone"), p.get("location"),
                                      ("Nationality: " + p["nationality"]) if p.get("nationality") else ""] if v)
    if contact:
        E.append(Paragraph(x(contact), st["b"]))
    bullets = lambda items: ListFlowable([ListItem(Paragraph(x(i), st["b"]), leftIndent=10) for i in items], bulletType="bullet", start="•", leftIndent=10)
    if p.get("summary"):
        E += [Paragraph("PROFESSIONAL SUMMARY", st["sec"]), Paragraph(x(p["summary"]), st["b"])]
    if p.get("strengths"):
        E += [Paragraph("KEY STRENGTHS", st["sec"]), bullets(p["strengths"])]
    if p.get("experience"):
        E.append(Paragraph("EXPERIENCE", st["sec"]))
        for e in p["experience"]:
            dates = " – ".join(v for v in [e.get("start"), e.get("end")] if v)
            line = " — ".join(v for v in [e.get("title"), e.get("company")] if v)
            E.append(Paragraph(x(line) + (f" <font name='{f}' size='9'>({x(dates)})</font>" if dates else ""), st["bb"]))
            if e.get("location"):
                E.append(Paragraph(x(e["location"]), st["b"]))
            if e.get("bullets"):
                E.append(bullets(e["bullets"]))
            E.append(Spacer(1, 4))
    if p.get("education"):
        E.append(Paragraph("EDUCATION", st["sec"]))
        for e in p["education"]:
            dates = " – ".join(v for v in [e.get("start"), e.get("end")] if v)
            E.append(Paragraph(x(" — ".join(v for v in [e.get("degree"), e.get("school")] if v)) + (f" ({x(dates)})" if dates else ""), st["b"]))
    for key, label in (("skills", "SKILLS"), ("certifications", "CERTIFICATIONS"), ("languages", "LANGUAGES")):
        if p.get(key):
            E += [Paragraph(label, st["sec"]), Paragraph(x(" • ".join(p[key])), st["b"])]
    doc.build(E)
    pdf = buf.getvalue()
    check_pdf(pdf, p)
    return pdf


def check_pdf(pdf, p):
    from pypdf import PdfReader
    text = norm(" ".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(pdf)).pages))
    missing = [v for v in [p.get("name")] + [e.get("company") for e in p.get("experience") or []] if v and norm(v) not in text]
    if missing or len(pdf) < 1500:
        raise CVNotReady("PDF check failed: " + ", ".join(missing[:3]))
