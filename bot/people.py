"""MixRank people lookup — who to actually talk to at an employer.

WHY THIS IS A SEPARATE THING FROM THE JOB FEED
----------------------------------------------
MixRank has no job-search endpoint. `/jobs`, `/job_listings`, `/companies/{id}/jobs`
and every variant 404, and `/linkedin/job` only resolves a job you can already
name by LinkedIn id. So MixRank cannot find postings and is not a second source
for the feed. What it has is people, and that is the half of a job search the
feed can't touch: a posting tells you a role exists, a name tells you who reads
the applications.

WHAT THE API ACTUALLY SUPPORTS (verified 9 Nov 2026, not from docs)
-------------------------------------------------------------------
    /companies/match?name=|url=   exactly one result — deterministic resolution
    /person/search?search=<text>  FREE TEXT ONLY, returns a bare JSON array
    /person/{id}                  positions[] (start_date/is_current/title),
                                  education[], emails[]

The trap: **there is no company filter on person search.** MixRank silently
ignores unrecognised parameters rather than rejecting them, so a filter that
looks like it works may do nothing at all. Measured:

    search=recruiter                          -> Apple, Amazon, BNY, Spherion
    search=recruiter&linkedin_company_id=...  -> IDENTICAL list (ignored)
    search=recruiter&company_id=82484450      -> Aptean, Zoox, Netflix (not Shopify)

So the company goes in the free-text query and the result set is filtered HERE,
against `linkedin.org` and the current position. For "Shopify technical
recruiter" that was 9 of 20 genuinely at Shopify. Anything this module returns
has been checked; nothing is trusted because the API ranked it highly.

COST
----
Every MixRank call takes roughly 8 seconds. A Discord interaction has to be
answered inside 15 minutes but a human gives up in about 30 seconds, so the
budget is: one search, then a bounded handful of profile fetches run
concurrently. Profiles are cached, because the same employer gets looked up
repeatedly while someone works through a shortlist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

log = logging.getLogger("people")

BASE = "https://api.mixrank.com/v2/json"
TIMEOUT = 45
# Concurrency against one vendor's API. Four is enough to keep a /who under
# ~20s while staying obviously polite — this is someone else's rate limit.
CONCURRENCY = 4
MAX_PROFILES = 8          # how many candidates we pay 8s each to open
SEARCH_PAGE = 25          # how many the free-text search returns to filter down

# Defaults to the user's own school so alumni surface without being asked for;
# `/who school:` overrides it per call.
ALUMNI_SCHOOL = os.environ.get("ALUMNI_SCHOOL", "University of Toronto")

# Nobody types "University of Toronto" into a Discord command; they type UofT.
# Alumni matching is a substring test against LinkedIn's school name, and
# MixRank normalises those to the full official form — so "uoft" matched
# nothing at all, silently. A filter that quietly matches nothing is worse
# than one that errors, because it reads as "no alumni work here".
SCHOOL_ALIASES = {
    "uoft": "University of Toronto",
    "uot": "University of Toronto",
    "utoronto": "University of Toronto",
    "toronto": "University of Toronto",
    "uwaterloo": "University of Waterloo",
    "waterloo": "University of Waterloo",
    "uw": "University of Waterloo",
    "ubc": "University of British Columbia",
    "sfu": "Simon Fraser University",
    "mcgill": "McGill University",
    "queens": "Queen's University",
    "mcmaster": "McMaster University",
    "western": "Western University",
    "uwo": "Western University",
    "uottawa": "University of Ottawa",
    "carleton": "Carleton University",
    "yorku": "York University",
    "york": "York University",
    "tmu": "Toronto Metropolitan University",
    "ryerson": "Toronto Metropolitan University",
    "ualberta": "University of Alberta",
    "ucalgary": "University of Calgary",
    "concordia": "Concordia University",
    "dal": "Dalhousie University",
    "dalhousie": "Dalhousie University",
    "polymtl": "Polytechnique Montréal",
    "mit": "Massachusetts Institute of Technology",
    "cmu": "Carnegie Mellon University",
    "ucla": "University of California, Los Angeles",
    "berkeley": "University of California, Berkeley",
    "ucb": "University of California, Berkeley",
}


def resolve_school(text: str) -> str:
    """Turn whatever a person typed into the name LinkedIn actually uses.

    "U of T", "UofT", "u-of-t" all collapse to the same key, so the alias
    table only needs the canonical spelling of each abbreviation.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    return SCHOOL_ALIASES.get(key, raw)


def school_matches(school_name: str, wanted: str) -> bool:
    """Does this education entry count as `wanted`?

    Checks the resolved name AND the raw input, so an alias still works for a
    school not in the table ("Sheridan") and a campus variant still matches
    ("University of Toronto Mississauga" for "UofT").
    """
    hay = (school_name or "").lower()
    if not hay:
        return False
    for cand in {resolve_school(wanted).lower(), (wanted or "").strip().lower()}:
        if cand and cand in hay:
            return True
    return False

_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 60 * 60 * 6


def enabled() -> bool:
    return bool(os.environ.get("MIXRANK_KEY"))


def status() -> str:
    return ("people lookup on (MixRank)" if enabled()
            else "people lookup off (no MIXRANK_KEY set)")


def _key() -> str:
    return os.environ.get("MIXRANK_KEY") or ""


def _cached(name: str):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(name: str, value):
    _cache[name] = (time.time(), value)
    return value


async def _get(session, path: str, params: dict | None = None):
    """GET from MixRank. Returns None on any failure — a people lookup that
    breaks must degrade to "couldn't find anyone", never to a broken command."""
    import aiohttp
    url = f"{BASE}/{_key()}/{path}"
    try:
        async with session.get(url, params=params or {},
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            body = await r.text()
            if r.status != 200:
                # 404 is MixRank's "no results", not an error worth shouting about.
                if r.status != 404:
                    log.warning("mixrank %s -> %s: %s", path, r.status, body[:160])
                return None
            return json.loads(body)
    except asyncio.TimeoutError:
        log.warning("mixrank %s timed out after %ss", path, TIMEOUT)
        return None
    except Exception as e:
        log.warning("mixrank %s failed: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# company resolution
# ---------------------------------------------------------------------------
async def resolve_company(session, name: str) -> dict | None:
    """Resolve a company name to MixRank's canonical record.

    `/companies/match` returns exactly one result, unlike `/companies?search=`
    which returns a ranked guess list ("Jobber" matched a Pune recruitment
    agency third). Deterministic beats ranked when the answer feeds a filter.
    """
    if not enabled() or not (name or "").strip():
        return None
    ck = f"co:{name.lower().strip()}"
    if (hit := _cached(ck)) is not None:
        return hit or None
    data = await _get(session, "companies/match", {"name": name.strip()})
    results = (data or {}).get("results") or []
    return _store(ck, results[0] if results else {}) or None


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Company names arrive with suffixes and punctuation that differ between
    the feed ("Royal Bank of Canada (Early Talent)") and LinkedIn ("RBC")."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|sa|nv|gmbh)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _same_company(a: str, b: str) -> bool:
    """Deliberately loose in ONE direction only: one name must contain the
    other. "Shopify" matches "Shopify Inc." but not "Shopify Partners Agency"
    unless that string really does contain the whole name."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


async def search_people(session, company: str, role: str = "") -> list[dict]:
    """Free-text search, then filter to people actually at `company`."""
    if not enabled():
        return []
    query = f"{company} {role}".strip()
    ck = f"ps:{query.lower()}"
    if (hit := _cached(ck)) is not None:
        return hit
    data = await _get(session, "person/search",
                      {"search": query, "page_size": str(SEARCH_PAGE)})
    rows = data if isinstance(data, list) else (data or {}).get("results") or []
    kept = []
    for p in rows:
        li = p.get("linkedin") or {}
        if _same_company(company, li.get("org") or ""):
            kept.append(p)
    return _store(ck, kept)


async def person_detail(session, pid) -> dict | None:
    ck = f"pd:{pid}"
    if (hit := _cached(ck)) is not None:
        return hit or None
    data = await _get(session, f"person/{pid}")
    return _store(ck, data or {}) or None


def _months_since(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(str(date_str)[:10]).replace(tzinfo=timezone.utc)
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    return max(0, (now.year - dt.year) * 12 + (now.month - dt.month))


def tenure_label(months: int | None) -> str:
    if months is None:
        return "tenure unknown"
    if months < 1:
        return "just joined"
    if months < 12:
        return f"{months}mo there"
    return f"{months // 12}y{'+' if months % 12 else ''} there"


def domain_of(website: str | None) -> str:
    """Registrable-ish domain from a website URL: http://www.jobber.com -> jobber.com"""
    d = re.sub(r"^https?://", "", (website or "").strip().lower()).split("/")[0]
    return re.sub(r"^www\.", "", d)


def _is_work_email(email: str, company: str, company_domain: str) -> bool:
    """Is this address at the employer we asked about?

    Strictly: a non-matching address must be DROPPED, not shown. MixRank
    returns whatever addresses it holds for a person, including ones from
    previous jobs and personal accounts — a /who for Jobber was offering
    `a.person@othercorp.com` and `b.person@thirdparty.com`. Printing those is
    both wrong (you'd email the wrong company) and a small privacy leak of
    someone's unrelated address.

    Matching is on the domain's root token rather than equality, because the
    two disagree constantly: MixRank lists Jobber's site as jobber.com while
    its staff are on getjobber.com, so `a.person@getjobber.com` is correct and
    an equality check would reject it.
    """
    dom = (email or "").split("@")[-1].lower()
    if not dom:
        return False
    root = dom.rsplit(".", 1)[0].replace(".", "")
    cd = (company_domain or "").rsplit(".", 1)[0].replace(".", "")
    tokens = [t for t in _norm(company).split() if len(t) > 2]
    if cd and (cd in root or root in cd):
        return True
    return any(t in root for t in tokens)


def summarise(detail: dict, company: str, school: str,
              company_domain: str = "") -> dict:
    """Flatten a profile into what a person deciding whether to reach out needs."""
    li = (detail or {}).get("linkedin") or {}
    positions = li.get("positions") or []

    # The current role AT THIS COMPANY, not merely any current role — people
    # keep side projects and volunteer posts flagged current for years.
    here = [p for p in positions
            if p.get("is_current") and _same_company(company, p.get("company_name") or "")]
    if not here:
        here = [p for p in positions if _same_company(company, p.get("company_name") or "")]
    cur = here[0] if here else None

    months = _months_since(cur.get("start_date")) if cur else None
    alum = [e for e in (li.get("education") or [])
            if school and school_matches(e.get("school_name") or "", school)]

    emails = [e.get("email") for e in (detail or {}).get("emails") or []
              if e.get("email")]
    work = [e for e in emails if _is_work_email(e, company, company_domain)]

    return {
        "name": (li.get("name") or {}).get("full") or "Unknown",
        "title": (cur or {}).get("title") or li.get("title") or li.get("headline") or "",
        "location": ((li.get("location") or {}).get("text") or ""),
        "linkedin": li.get("url") or "",
        "started": (cur or {}).get("start_date"),
        "months": months,
        "is_current": bool(cur and cur.get("is_current")),
        "alumni": bool(alum),
        "alumni_detail": (f"{alum[0].get('school_name')}"
                          f"{' — ' + alum[0]['field_of_study'] if alum[0].get('field_of_study') else ''}"
                          if alum else ""),
        # No fallback to emails[0]: see _is_work_email. A wrong address is
        # worse than none.
        "email": work[0] if work else "",
        "recruiter": _is_recruiter((cur or {}).get("title") or li.get("headline") or ""),
    }


RECRUITER_RE = re.compile(
    r"recruit|talent acquisition|talent partner|sourcer|campus|university relations|"
    r"people ops|people partner|hr business partner", re.I)


def _is_recruiter(title: str) -> bool:
    return bool(RECRUITER_RE.search(title or ""))


async def who(session, company: str, role: str = "",
              school: str = ALUMNI_SCHOOL, limit: int = MAX_PROFILES) -> dict:
    """The whole lookup: resolve, search, filter, then open the best profiles.

    Returns {"company": canonical-or-input, "people": [...], "searched": N}.
    Profiles are opened concurrently because each costs ~8s serially, and a
    command nobody waits for is a command nobody uses.
    """
    canonical = company
    company_domain = ""
    co = await resolve_company(session, company)
    if co:
        canonical = co.get("name") or company
        company_domain = domain_of(((co.get("linkedin") or {}).get("website")))

    rows = await search_people(session, canonical, role)
    if not rows and canonical != company:
        rows = await search_people(session, company, role)

    # Recruiters first: they are the ones whose job is to answer strangers.
    def rank(p):
        li = p.get("linkedin") or {}
        return (0 if _is_recruiter(li.get("title") or li.get("headline") or "") else 1,
                -(li.get("connections") or 0))

    rows.sort(key=rank)
    chosen = rows[:limit]

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(p):
        async with sem:
            d = await person_detail(session, p.get("id"))
            return summarise(d, canonical, school, company_domain) if d else None

    people = [x for x in await asyncio.gather(*(one(p) for p in chosen)) if x]
    # Alumni first, then recruiters, then whoever arrived most recently —
    # a new recruiter has the emptiest pipeline and the most reason to reply.
    people.sort(key=lambda x: (not x["alumni"], not x["recruiter"],
                               x["months"] if x["months"] is not None else 999))
    return {"company": canonical, "people": people, "searched": len(rows)}
