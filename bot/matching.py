"""Role families, city normalising, query parsing and the relevance scorer.

WHY THIS EXISTS, in one number
------------------------------
`/search` used to be a substring match over title + company. Measured against
the live 1,623-role feed on 9 Nov 2026:

    "backend security"  ->  0 roles      (backend=37, security=9, both=0)
    "ai engineer"       ->  2 roles      ...out of 34 real AI/ML intern roles

The misses are titled "Deep Learning Research Intern", "AI/ML Research Intern",
"Data Science Co-op", "2027 Intern, PhD Research Scientist". A literal match
finds two of thirty-four, and an AND of two words finds nothing at all.

THE THREE RULES THAT FALL OUT OF THE DATA
-----------------------------------------
1. **OR, never AND.** "backend security" means "either of these interests me",
   because no single title carries both words. ANDing user words is how you
   build a search box that always returns nothing.

2. **Title-only.** `description` is populated on 7% of the feed (107/1623) —
   Workday and the Simplify sweep publish none. There is no body text to be
   clever about, so matching happens on the title and nothing else pretends
   otherwise.

3. **Score, don't filter.** Canada + intern is 150 roles *in total*; add one
   keyword and it is 1-15. Hard-ANDing a role family, a city and a term leaves
   most people with an empty digest most days, and a digest that is usually
   empty gets muted. So only COUNTRY and LEVEL are hard filters (people are
   genuinely absolute about those); role, city, term and freshness are weights,
   and everyone gets their top N ranked rather than a slice that may be empty.

Deliberately NOT here: internship length. Only 18 of 1,623 titles name a
duration (1.1%), and the 7% of roles carrying a description are the same ones.
There is no field for it, so there is nothing to filter on — see the project
notes rather than guessing from the title.

Everything in this module is pure and dependency-free, so it runs identically
in the bot, in the collector, and under `python tests/test_matching.py`.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# role families
# ---------------------------------------------------------------------------
# These are the groupings already present in config.yml's `domain_include_any`,
# which was one flat list. Flat is right for "is this a software job at all"
# (the collector's question) but useless for "is this the KIND of software job
# I want" (the member's question), which needs the names.
#
# `aliases` are what a person types; `terms` are what employers actually write
# in titles. Both are substring-matched against the lowercased title.
ROLE_FAMILIES = {
    "swe": {
        "label": "software engineering",
        # NOT "engineering" or "engineer": they appear inside "data
        # engineering", "security engineering" and "firmware engineer", so a
        # query for "data engineering in Vancouver" would light up the whole
        # software family and rank firmware roles above the data ones asked for.
        "aliases": ["swe", "software", "software engineer", "software developer",
                    "developer", "programmer"],
        "terms": ["software", "swe", "sde", "developer", "développeur",
                  "developpeur", "logiciel", "programmer", "application developer",
                  "systems developer", "web developer", "mobile developer",
                  "game developer", "compiler", "embedded software", "firmware",
                  "android", "ios"],
    },
    "backend": {
        "label": "backend",
        "aliases": ["backend", "back-end", "back end", "server", "api"],
        "terms": ["backend", "back-end", "back end", "distributed systems",
                  "api ", "server"],
    },
    "frontend": {
        "label": "frontend",
        "aliases": ["frontend", "front-end", "front end", "ui", "web"],
        "terms": ["frontend", "front-end", "front end", "web developer", "ui ",
                  "react"],
    },
    "fullstack": {
        "label": "full stack",
        "aliases": ["fullstack", "full stack", "full-stack"],
        "terms": ["full stack", "full-stack", "fullstack"],
    },
    "ml": {
        "label": "AI / ML",
        "aliases": ["ml", "ai", "a.i.", "ai/ml", "machine learning",
                    "artificial intelligence", "ai engineer", "ml engineer",
                    "deep learning", "llm", "nlp", "computer vision",
                    "genai", "gen ai", "generative ai", "research"],
        # This list is why "AI engineer intern" now finds "Deep Learning
        # Research Intern" and "2027 Intern, PhD Research Scientist".
        "terms": ["machine learning", "ml engineer", "ml intern", "ml/ai",
                  "ai/ml", "artificial intelligence", "ai engineer", "ai intern",
                  "ai developer", "ai software", "ai research", "deep learning",
                  "nlp", "natural language", "computer vision", "llm",
                  "generative", "applied scien", "research scien", "robotics",
                  "perception", "embodied"],
    },
    "data": {
        "label": "data",
        "aliases": ["data", "data science", "data scientist", "data engineer",
                    "data analyst", "analytics", "bi"],
        "terms": ["data scien", "data engineer", "data analyst", "data analytics",
                  "analytics engineer", "business intelligence", "données",
                  "big data", "database", "data platform"],
    },
    "infra": {
        "label": "infra / platform",
        "aliases": ["infra", "infrastructure", "devops", "sre", "platform",
                    "cloud", "systems", "network"],
        "terms": ["devops", "sre", "site reliability", "platform engineer",
                  "infrastructure", "cloud engineer", "cloud developer",
                  "systems engineer", "network engineer", "distributed systems",
                  "kubernetes"],
    },
    "security": {
        "label": "security",
        "aliases": ["security", "cyber", "cybersecurity", "appsec", "infosec"],
        "terms": ["security", "cybersecurity", "cyber security", "appsec",
                  "cryptograph", "penetration"],
    },
    "qa": {
        "label": "QA / test",
        "aliases": ["qa", "test", "testing", "sdet", "quality"],
        "terms": ["qa engineer", "quality assurance", "test engineer",
                  "test automation", "sdet", "quality engineer"],
    },
}

# An alias that is a bare, very short token is only honoured as a WHOLE WORD.
# Without this, "ai" fires on "maintain" and "ml" on "html", and the person who
# typed "data" gets handed every "Validation Intern" in the feed.
_WORDY_ALIASES = {"ml", "ai", "ui", "qa", "bi", "sre", "dev", "api", "web",
                  "test", "data", "cloud", "server", "research", "engineering"}


def _contains(haystack: str, needle: str) -> bool:
    """Substring match, but whole-word for the short/ambiguous tokens."""
    if needle in _WORDY_ALIASES or len(needle) <= 3:
        return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None
    return needle in haystack


def families_in_title(title: str) -> set[str]:
    """Which role families this job title belongs to (possibly several)."""
    low = (title or "").lower()
    return {key for key, fam in ROLE_FAMILIES.items()
            if any(_contains(low, t) for t in fam["terms"])}


# ---------------------------------------------------------------------------
# cities
# ---------------------------------------------------------------------------
# Toronto appears as 21 DISTINCT strings across 62 roles in the live feed —
# "Toronto, ON, Canada", "Toronto, Ontario", "Toronto, ON", "Toronto, CAN",
# bare "Toronto". A city filter that matches raw location strings silently
# misses two thirds of the roles in the city the member actually asked for,
# which is the worst kind of bug: it looks like "nothing is hiring there".
CITY_ALIASES = {
    "toronto": ["toronto", "north york", "scarborough", "etobicoke"],
    "waterloo": ["waterloo", "kitchener", "cambridge, on"],
    "vancouver": ["vancouver", "burnaby", "richmond, bc", "surrey, bc",
                  "coquitlam"],
    "montreal": ["montreal", "montréal", "laval", "longueuil"],
    "ottawa": ["ottawa", "kanata", "gatineau"],
    "calgary": ["calgary"],
    "edmonton": ["edmonton"],
    "mississauga": ["mississauga", "oakville", "brampton", "milton, on"],
    "markham": ["markham", "richmond hill", "vaughan", "newmarket, on"],
    "winnipeg": ["winnipeg"],
    "halifax": ["halifax"],
    "quebec city": ["quebec city", "québec city", "ville de québec"],
    "victoria": ["victoria, bc"],
    "new york": ["new york", "nyc", "manhattan", "brooklyn"],
    "san francisco": ["san francisco", "bay area", "palo alto", "mountain view",
                      "sunnyvale", "cupertino", "menlo park", "san jose"],
    "seattle": ["seattle", "bellevue", "redmond"],
    "boston": ["boston", "cambridge, ma"],
    "remote": ["remote", "work from home", "anywhere"],
}

# Order matters only for display; "remote" is checked like any other.
CITY_KEYS = list(CITY_ALIASES)


def cities_in_location(location: str) -> set[str]:
    """Canonical city keys a location string covers.

    Multi-site reqs arrive ';'-joined, and a role listing both Toronto and
    New York genuinely belongs to someone watching either.
    """
    low = (location or "").lower()
    if not low:
        return set()
    out = set()
    for key, variants in CITY_ALIASES.items():
        if any(v in low for v in variants):
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# query parsing — the deterministic half
# ---------------------------------------------------------------------------
# This is the fallback when no OPENAI_API_KEY is configured, and the sanity
# check on the model's output when there is one. The vocabulary is small and
# closed (9 families, ~18 cities, 4 seasons, 2 levels), so a hand-written
# parser handles most of how a person actually phrases this at zero cost and
# zero latency.
# The four-digit year MUST come first in the alternation. With `'?\d{2}` first,
# "winter 2026" matches the "20" and becomes "Winter 2020" — a silently wrong
# term that would filter against a cycle three years in the past.
TERM_RE = re.compile(r"\b(summer|winter|fall|autumn|spring)\s*(\d{4}|'\d{2})?", re.I)
_SEASON_FIX = {"autumn": "Fall"}

INTERN_WORDS = re.compile(
    r"\bintern(ship)?s?\b|\bco-?\s?ops?\b|\bstudent\b|\bstagiaire\b", re.I)
NEWGRAD_WORDS = re.compile(
    r"\bnew[\s-]?grad(uate)?s?\b|\bentry[\s-]?level\b|\bjunior\b|\bgrad program\b",
    re.I)

# Words that carry no filtering signal but appear constantly in how people
# phrase a search. Left in `keywords`, they match nothing and only add noise.
STOPWORDS = {
    "a", "an", "and", "any", "anything", "are", "as", "at", "be", "but", "by",
    "can", "do", "find", "for", "from", "get", "give", "i", "in", "internships",
    "is", "it", "job", "jobs", "like", "looking", "me", "my", "near", "of", "on",
    "only", "or", "position", "positions", "role", "roles", "show", "some",
    "something", "that", "the", "them", "there", "these", "this", "to", "want",
    "with", "work", "would", "next", "please", "new", "open", "available",
    # Words that appear in nearly every title and so discriminate nothing.
    # Left as free keywords they are actively harmful: "data engineering in
    # Vancouver" scored "engineering" (3.0) above the city the member actually
    # named (2.5), pushing Toronto roles above the Vancouver ones.
    "engineer", "engineers", "engineering", "intern", "interns", "internship",
    "co-op", "coop", "student", "students", "grad", "graduate", "junior",
    "entry", "level", "software",
}


def parse_query(text: str, *, year_hint: int | None = None) -> dict:
    """Turn free text into a structured query. Never raises; always returns
    every key, so callers don't have to defend against a partial dict.

        >>> q = parse_query("ML internships in Toronto for next summer")
        >>> sorted(q["families"]), q["cities"], q["level"]
        (['ml'], ['toronto'], 'intern')
    """
    raw = (text or "").strip()
    low = raw.lower()

    families = {key for key, fam in ROLE_FAMILIES.items()
                if any(_contains(low, a) for a in fam["aliases"])}

    cities = {key for key, variants in CITY_ALIASES.items()
              if any(v.split(",")[0] in low for v in variants)}

    terms = []
    for m in TERM_RE.finditer(raw):
        season = m.group(1).lower()
        season = _SEASON_FIX.get(season, season.capitalize())
        year = (m.group(2) or "").lstrip("'")
        if len(year) == 2:
            year = "20" + year
        if not year:
            # "next summer" with no year: the feed is overwhelmingly one cycle
            # ahead, so prefer the caller's hint over guessing wrong.
            year = str(year_hint) if year_hint else ""
        terms.append(f"{season} {year}".strip())

    level = None
    if INTERN_WORDS.search(raw):
        level = "intern"
    elif NEWGRAD_WORDS.search(raw):
        level = "newgrad"

    # Whatever is left over, minus the words we already consumed, becomes
    # free keywords. These catch the long tail a family list can't know about
    # ("rust", "compiler", "fintech").
    consumed = set()
    for key in families:
        for a in ROLE_FAMILIES[key]["aliases"]:
            consumed.update(a.split())
    for key in cities:
        for v in CITY_ALIASES[key]:
            consumed.update(v.replace(",", " ").split())
    keywords = [w for w in re.findall(r"[a-z0-9+#.]{2,}", low)
                if w not in STOPWORDS and w not in consumed
                and not TERM_RE.fullmatch(w) and not INTERN_WORDS.fullmatch(w)]

    return {
        "raw": raw,
        "families": sorted(families),
        "keywords_any": keywords,
        "keywords_none": [],
        "cities": sorted(cities),
        "terms": terms,
        "level": level,
        # Free text to try against the EMPLOYER name. `/search RBC` is a real
        # and common thing to type, and "RBC" is not a role family, a city or
        # a title keyword — without this it would score zero everywhere.
        "company": raw,
    }


def empty_query() -> dict:
    return {"raw": "", "families": [], "keywords_any": [], "keywords_none": [],
            "cities": [], "terms": [], "level": None, "company": ""}


def merge_query(base: dict | None, override: dict | None) -> dict:
    """`override` wins field by field, but only where it actually said something.

    This is how a member's hand-edited keywords survive a re-parse, and how an
    LLM parse is layered over the deterministic one without erasing what the
    deterministic pass got right.
    """
    out = dict(empty_query())
    for src in (base or {}, override or {}):
        for k, v in (src or {}).items():
            if k not in out:
                continue
            if v in (None, [], ""):
                continue
            out[k] = v
    return out


def describe_query(q: dict) -> str:
    """One human line, for the embed footer and /prefs output."""
    q = merge_query(empty_query(), q)
    bits = []
    if q["families"]:
        bits.append(" + ".join(ROLE_FAMILIES[f]["label"] for f in q["families"]
                               if f in ROLE_FAMILIES))
    if q["keywords_any"]:
        bits.append("“" + ", ".join(q["keywords_any"][:4]) + "”")
    if q["cities"]:
        bits.append("in " + " / ".join(c.title() for c in q["cities"]))
    if q["terms"]:
        bits.append(" / ".join(q["terms"]))
    if q["keywords_none"]:
        bits.append("not " + ", ".join(q["keywords_none"][:3]))
    return " · ".join(b for b in bits if b) or "anything"


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
W_FAMILY = 4.0        # the member named this kind of work
W_KEYWORD = 3.0       # an explicit word of theirs is in the title
W_COMPANY = 6.0       # they named an employer — that is the most specific
                      # thing a person can ask for, so it outranks everything
W_CITY = 2.5
W_TERM = 1.0
W_FRESH = 2.0         # at most; decays over two weeks
W_UNDATED = 0.4       # a Workday role with no date is not "old", just unknown
W_MID_LEVEL = 5.0     # demotes, never excludes — some employers really do
                      # title a new-grad req "Engineer II"

# Roman numerals II+ and explicit seniority. `filters.EXCLUDE_TITLE` already
# drops "senior"/"staff" at collection time, so in practice this catches the
# numeral forms that slip through.
# Roman numerals only. A bare "2"/"3" was deliberately left out: it buys
# almost nothing and risks firing on real title text ("Term 2", "Intern 2").
MID_LEVEL_RE = re.compile(
    r"\b(ii|iii|iv)\b|\bsenior\b|\bsr\.?\b|\bstaff\b|\bmid[\s-]level\b", re.I)


def score(job: dict, q: dict | None) -> tuple[float | None, list[str]]:
    """Relevance of one job to one query. Higher is better.

    Returns `(None, ...)` — not a negative number — for a HARD exclusion.
    These are genuinely different things and conflating them is a bug: a
    demoted role (mid-level title, wrong city) carries a penalty and may well
    end up below zero, but it must still be rankable, whereas a
    `keywords_none` hit must vanish. Using "negative" to mean "excluded" made
    the mid-level penalty silently delete roles instead of demoting them.

    An empty query scores every job on freshness alone, which is exactly what
    a bare /latest should do.
    """
    q = merge_query(empty_query(), q)
    title = (job.get("title") or "")
    low = title.lower()

    # The only hard rejection in here. A member who says "not security" means
    # it, and ranking such a role 9th is not good enough.
    for bad in q["keywords_none"]:
        if bad and bad.lower() in low:
            return None, ["excluded"]

    pts = 0.0
    why = []

    fams = families_in_title(title)
    hit_fams = fams & set(q["families"])
    if hit_fams:
        pts += W_FAMILY * min(len(hit_fams), 2)
        why.append("/".join(sorted(ROLE_FAMILIES[f]["label"] for f in hit_fams)))

    hit_kw = [w for w in q["keywords_any"] if w and _contains(low, w.lower())]
    if hit_kw:
        pts += W_KEYWORD * min(len(hit_kw), 3)
        why.append(", ".join(hit_kw[:3]))

    # Employer name. Only whole phrases of 2+ characters, so a one-letter
    # query doesn't match every company with that letter in it.
    comp = (q.get("company") or "").strip().lower()
    if len(comp) >= 2 and comp in (job.get("company") or "").lower():
        pts += W_COMPANY
        why.append(job.get("company") or comp)

    if q["cities"]:
        hit_city = cities_in_location(job.get("location", "")) & set(q["cities"])
        if hit_city:
            pts += W_CITY
            why.append(sorted(hit_city)[0].title())

    if q["terms"]:
        hay = f"{job.get('term') or ''} {title}".lower()
        if any(t.lower() in hay for t in q["terms"]):
            pts += W_TERM
            why.append(next(t for t in q["terms"] if t.lower() in hay))

    # Mid-level roles leaking into the intern pool. The collector tags level
    # from the title, and its new-grad regex has `engineer i\b`, which does
    # NOT match "Engineer II" — so "Software Engineer II, Backend" arrives
    # tagged `intern` and, being a genuine backend role, ranked FIRST for
    # anyone searching "backend". Penalising it here fixes the ranking without
    # touching the collector's classification, which feeds the shared channel
    # for everyone and would need a re-seed to change.
    if job.get("level") != "newgrad" and MID_LEVEL_RE.search(title):
        pts -= W_MID_LEVEL

    # Freshness. Undated roles (every Workday posting — 60% of the Canadian
    # market) get a small flat credit instead of sorting dead last, because
    # "we can't read this employer's date format" is our problem, not a reason
    # to bury the role.
    h = _age_hours(job)
    if h is None:
        pts += W_UNDATED
    else:
        pts += W_FRESH * max(0.0, 1.0 - (h / (24 * 14)))

    return pts, why


def _age_hours(job: dict) -> float | None:
    """Hours since posting, or None. Duplicated from larpbuddy.age_hours so
    this module stays importable without discord.py installed."""
    from datetime import datetime, timezone
    ts = job.get("posted_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def is_query_hit(why: list[str]) -> bool:
    """Did this job match something the member actually ASKED for?

    Every job scores above zero, because freshness alone earns points — that
    is what keeps digests non-empty. So "150 results" is a true statement about
    the ranking and a lie about the search. `why` is only populated by family,
    keyword, city and term hits, never by freshness, so a non-empty `why` is
    the honest definition of "matched", and it is what the bot reports.
    """
    return bool(why)


def count_hits(scored: list[tuple[dict, float, list[str]]]) -> int:
    return sum(1 for _, _, why in scored if is_query_hit(why))


def rank(jobs: list[dict], q: dict | None, *, limit: int | None = None,
         min_score: float | None = None) -> list[tuple[dict, float, list[str]]]:
    """Score, drop exclusions, sort best-first.

    `min_score` defaults to None — no threshold at all — on purpose: with
    CA+intern at 150 roles total, a threshold that feels "reasonable" is how
    you end up shipping an empty digest. Ranking everything and taking the top
    N is strictly safer, and the score is shown so a weak match looks weak.
    """
    scored = []
    for j in jobs:
        s, why = score(j, q)
        if s is None:                       # hard exclusion, not a low score
            continue
        if min_score is not None and s < min_score:
            continue
        scored.append((j, s, why))
    scored.sort(key=lambda t: (-t[1], -(t[0].get("posted_at") is not None),
                               t[0].get("company") or ""))
    return scored[:limit] if limit else scored
