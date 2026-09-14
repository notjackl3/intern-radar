"""Source adapters. Every adapter yields normalised dicts:

    {id, source, company, title, location, url, posted_at, term}

`posted_at` is an ISO-8601 string or None. `id` must be stable across runs —
it is what the dedupe layer diffs on.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

log = logging.getLogger("adapters")

UA = "intern-radar/1.0 (personal job-alert bot; +https://github.com/)"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}
TIMEOUT = 30

# Courtesy delay, enforced PER HOST. Adapters run concurrently across hosts
# (that is what keeps a run under a minute) but never concurrently against the
# same host, and never faster than one request per DELAY seconds to it.
DELAY = 1.2

_host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_host_last: dict[str, float] = {}
_registry_lock = threading.Lock()


class _Host:
    """Serialise + rate-limit requests to one hostname."""

    def __init__(self, url: str):
        self.host = urlparse(url).netloc
        with _registry_lock:
            self.lock = _host_locks[self.host]

    def __enter__(self):
        self.lock.acquire()
        gap = time.monotonic() - _host_last.get(self.host, 0.0)
        if gap < DELAY:
            time.sleep(DELAY - gap)
        return self

    def __exit__(self, *exc):
        _host_last[self.host] = time.monotonic()
        self.lock.release()
        return False


def _unescape(html: str | None) -> str | None:
    """Greenhouse returns the description HTML-entity-escaped."""
    if not html:
        return None
    import html as _h
    import re as _re
    text = _re.sub(r"<[^>]+>", " ", _h.unescape(html))
    return _re.sub(r"\s+", " ", text).strip()[:6000]


def _stable_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _iso(ts) -> str | None:
    """Accept epoch seconds, epoch millis, or an ISO string."""
    if ts in (None, "", 0):
        return None
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e11:  # Lever hands back epoch MILLIseconds
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return str(ts)
    except Exception:
        return None


def _get(url, **kw):
    with _Host(url):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _post(url, payload, **kw):
    h = dict(HEADERS)
    h["Content-Type"] = "application/json"
    h["Accept-Language"] = "en-US"
    with _Host(url):
        r = requests.post(url, headers=h, json=payload, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# Greenhouse — documented, unauthenticated. One GET returns the whole board.
# --------------------------------------------------------------------------
def greenhouse(company: str, token: str):
    # content=true adds the full description at no extra request. Greenhouse
    # HTML-entity-escapes it, so decode before reading it.
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    for j in _get(url).json().get("jobs", []):
        yield {
            "id": _stable_id("gh", token, j["id"]),
            "source": "greenhouse",
            "company": company,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": _iso(j.get("updated_at")),
            "term": None,
            "description": _unescape(j.get("content")),
        }


# --------------------------------------------------------------------------
# Lever — documented. createdAt is epoch MILLIseconds, not seconds.
# --------------------------------------------------------------------------
def lever(company: str, token: str):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    for j in _get(url).json():
        cat = j.get("categories") or {}
        yield {
            "id": _stable_id("lev", token, j.get("id")),
            "source": "lever",
            "company": company,
            "title": j.get("text", ""),
            "location": cat.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "posted_at": _iso(j.get("createdAt")),
            "term": None,
            "description": j.get("descriptionPlain"),
        }


# --------------------------------------------------------------------------
# Ashby — documented. isListed guards against unlisted/internal roles.
# --------------------------------------------------------------------------
def ashby(company: str, token: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    for j in _get(url).json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        yield {
            "id": _stable_id("ash", token, j.get("id")),
            "source": "ashby",
            "company": company,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": _iso(j.get("publishedAt")),
            "term": None,
            "description": j.get("descriptionPlain"),
        }


# --------------------------------------------------------------------------
# SmartRecruiters — documented. NOTE: a bad company id returns HTTP 200 with
# zero results rather than a 404, so a typo watches nothing, silently.
# --------------------------------------------------------------------------
def smartrecruiters(company: str, token: str):
    offset, limit, seen_any = 0, 100, False
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{token}"
               f"/postings?limit={limit}&offset={offset}")
        data = _get(url).json()
        content = data.get("content", [])
        for j in content:
            seen_any = True
            loc = j.get("location") or {}
            yield {
                "id": _stable_id("sr", token, j.get("id")),
                "source": "smartrecruiters",
                "company": company,
                "title": j.get("name", ""),
                "location": ", ".join(
                    x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
                ),
                "url": (f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}"),
                "posted_at": _iso(j.get("releasedDate")),
                "term": None,
            }
        offset += limit
        if offset >= data.get("totalFound", 0) or not content:
            break
    if not seen_any:
        log.warning("smartrecruiters/%s returned zero postings — check the token", token)


# --------------------------------------------------------------------------
# Workday — 60%% of the Canadian intern market and the fiddliest.
#   * limit is HARD CAPPED AT 20. Ask for 21 and you get an empty array with
#     HTTP 200 and no error at all.
#   * postedOn is a localised display string ("Posted 3 Days Ago"), useless as
#     a date. We diff on externalPath instead.
#   * tenant / shard / site are unguessable — they come from the careers URL:
#       https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/...
#              ^tenant ^shard             drop locale ^ ^site
#
# Two strategies, chosen per board by its size. The first response carries
# `total`, so one request tells us which we need:
#
#   SMALL board (total <= max_pages * 20) — page straight through it. Complete
#     coverage, and for a campus site like RBCEARLYTALENT1 that is the whole
#     board in three requests.
#
#   LARGE board — paging is hopeless. Salesforce, Adobe, Cisco and Samsung each
#     carry thousands of open roles, so six pages sees 120 of 3,000 and the
#     internships are simply not in them. Ask the server instead: searchText
#     narrows at the source, and a handful of probes ("intern", "co-op",
#     "stagiaire" for the French boards, ...) pulls back tens of roles rather
#     than thousands. Probes overlap, so results are deduped on externalPath.
#
# The keyword list only has to be recall-oriented; config.yml does the real
# filtering afterwards. Missing a keyword loses roles silently, so keep it
# generous rather than precise.
# --------------------------------------------------------------------------
WORKDAY_PROBES = ("intern", "co-op", "coop", "student", "new grad", "graduate",
                  "early career", "campus", "stagiaire", "étudiant")


def workday(company: str, tenant: str, shard: str, site: str, max_pages: int = 6,
            probes: bool = True):
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    seen: set[str] = set()
    total = 0          # set by the first response; 0 means the board didn't say
    exhausted = False  # a sweep ran out of results before hitting max_pages

    def sweep(search_text: str):
        """Page one query. Records `total` from the first response and sets
        `exhausted` when the query ran dry rather than hitting the page cap."""
        nonlocal total, exhausted
        for page in range(max_pages):
            payload = {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                       "searchText": search_text}
            try:
                data = _post(api, payload).json()
            except requests.HTTPError as e:
                log.warning("workday %s/%s %r page %s: %s",
                            tenant, site, search_text, page, e)
                return
            posts = data.get("jobPostings") or []
            if page == 0 and not search_text:
                total = int(data.get("total") or 0)
            for j in posts:
                path = j.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                yield {
                    "id": _stable_id("wd", tenant, site, path),
                    "source": "workday",
                    "company": company,
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"{base}/{site}{path}",
                    "posted_at": None,  # display-string only; see note above
                    "term": None,
                }
            if len(posts) < 20:
                exhausted = True
                return

    # Read the board straight through first. For a campus site like
    # RBCEARLYTALENT1 that is the entire board in three requests, and the first
    # response also tells us how big the board really is.
    yield from sweep("")

    # Probe only when plain paging cannot have covered the board: either the
    # board declared more roles than max_pages can reach, or it declared
    # nothing and we hit the page cap still finding full pages.
    if probes and (total > max_pages * 20 or (not exhausted and not total)):
        log.info("workday %s/%s: %s roles — plain paging can't cover it, "
                 "falling back to keyword probes",
                 tenant, site, total or "unknown")
        for kw in WORKDAY_PROBES:
            yield from sweep(kw)


# --------------------------------------------------------------------------
# Simplify's public dataset — the safety net. 2,186 Canadian rows, same-day
# fresh, and it catches employers that are not on your watchlist.
# The branch is `dev`. `main` 404s.
# --------------------------------------------------------------------------
SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json"
)

# Same maintainers, same schema, different repo: full-time entry-level roles
# rather than internships. Branch is `dev` here too — `main` 404s on both.
SIMPLIFY_NEWGRAD_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)


def _simplify_rows(url: str, source: str):
    rows = requests.get(url, headers={"User-Agent": UA}, timeout=120).json()
    for j in rows:
        if not (j.get("active") and j.get("is_visible")):
            continue
        yield {
            "id": _stable_id(source, j.get("id")),
            "source": source,
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": "; ".join(j.get("locations") or []),
            "url": j.get("url", ""),
            "posted_at": _iso(j.get("date_posted")),
            "term": "; ".join(j.get("terms") or []) or None,
            "degrees": j.get("degrees") or None,
        }


def simplify(_company=None, _token=None):
    yield from _simplify_rows(SIMPLIFY_URL, "simplify")


def simplify_newgrad(_company=None, _token=None):
    yield from _simplify_rows(SIMPLIFY_NEWGRAD_URL, "simplify-newgrad")


# --------------------------------------------------------------------------
# hiring.cafe — undocumented public API, reads ~46 ATS platforms directly.
# Unofficial, so it is wrapped in a try in main.py and never fatal.
# --------------------------------------------------------------------------
def hiringcafe(_company=None, _token=None, pages: int = 2):
    url = "https://hiring.cafe/api/search-jobs"
    for page in range(pages):
        payload = {
            "size": 100,
            "page": page,
            "searchState": {
                "commitmentTypes": ["Internship"],
                "locations": [{
                    "formatted_address": "Canada",
                    "types": ["country"],
                    "address_components": [
                        {"long_name": "Canada", "short_name": "CA", "types": ["country"]}
                    ],
                }],
                "sortBy": "date",
                "dateFetchedPastNDays": 7,
            },
        }
        try:
            data = _post(url, payload).json()
        except Exception as e:
            log.warning("hiring.cafe page %s failed (%s) — unofficial API, skipping", page, e)
            return
        results = data.get("results", []) or data.get("hits", [])
        if not results:
            return
        for j in results:
            info = j.get("job_information", {}) or {}
            proc = j.get("v5_processed_job_data", {}) or {}
            yield {
                "id": _stable_id("hc", j.get("id") or j.get("apply_url")),
                "source": "hiring.cafe",
                "company": (j.get("company_name")
                            or (j.get("processed_company_data") or {}).get("name", "")),
                "title": info.get("title") or j.get("title", ""),
                "location": proc.get("formatted_workplace_location", ""),
                "url": j.get("apply_url", ""),
                "posted_at": _iso(j.get("date_posted")),
                "term": None,
            }


ATS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
}
