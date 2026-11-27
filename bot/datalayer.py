"""Live job search through Datalayer (https://docs.datalayer.so).

The saved feed is at most ~20 minutes old. Datalayer adds roles the collector
never sees (its postings come mostly from LinkedIn) and checks the market
right now. `/search`, `/prefs` and the digest call it; with no
DATALAYER_API_KEY every function here returns [] and the bot behaves exactly
as it did without it.

HOW THE SEARCH IS SHAPED
------------------------
Measured 26 Nov 2026 against the live API:

  * `query` behaves like AND over every word, across title AND description.
    A whole sentence ("backend infrastructure internship Toronto") returned 1
    posting; the single word "backend" returned 32, 23 of the first 25 real
    software roles. So the member's query is never sent as typed. Each role
    family they asked for becomes ONE short keyword, and each keyword is its
    own call.
  * `seniority: intern` really does return internships (552 in Canada over
    14 days) — but every kind: truck technicians, sales, social media. Only 1
    in 25 was software. So every result must also name a software role family
    in its TITLE, the same vocabulary the bot ranks on, or it is dropped.
  * The same posting comes back once per location. Deduped on company + title.

COST: 1 credit per call that returns at least one posting; an empty page and
a failed call cost 0, and an identical call within 24h is free (so the same
member re-running the same search, or two members with the same prefs, pay
once). There is deliberately no cap here — the owner asked for none.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re

import aiohttp

import matching as M

log = logging.getLogger("datalayer")

URL = "https://api.datalayer.so/v1/jobs/postings"
TIMEOUT = 30
PAGE = 25                      # the API maximum
CONCURRENCY = 4                # well under the 60 requests/minute account limit

# One search word per role family: the word employers actually put in titles,
# short enough that the API's AND-over-words doesn't empty the result.
FAMILY_TERM = {
    "swe": "software",
    "backend": "backend",
    "frontend": "frontend",
    "fullstack": "full stack",
    "ml": "machine learning",
    "data": "data",
    "infra": "infrastructure",
    "security": "security",
    "qa": "QA",
}
# No query at all: cover the bot's core families.
DEFAULT_TERMS = ["software", "developer", "data", "machine learning"]

LEVEL_SENIORITY = {"intern": "intern", "newgrad": "entry level"}
COUNTRY_CODE = {"ca": "CA", "us": "US"}       # "other" has no single code

# Seniority words that mean the label is wrong for this bot, whatever
# Datalayer says. Numerals only matter for internships.
_NEVER = re.compile(r"\bsenior\b|\bsr\.?\b|\bstaff\b|\bprincipal\b|\bmanager\b|"
                      r"\bdirector\b|\blead\b", re.I)


def enabled() -> bool:
    return bool(os.environ.get("DATALAYER_API_KEY"))


def terms_for(q: dict | None) -> list[str]:
    """The search words for a parsed query: one per family, plus up to two of
    the member's own free keywords ("rust", "fintech"), else the defaults."""
    q = q or {}
    out = [FAMILY_TERM[f] for f in q.get("families") or [] if f in FAMILY_TERM]
    out += [w for w in (q.get("keywords_any") or [])[:2] if len(w) >= 3]
    seen, uniq = set(), []
    for t in out or DEFAULT_TERMS:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def _job(company: str | None, p: dict, level: str, cc: str) -> dict | None:
    title = (p.get("title") or "").strip()
    if not title or p.get("status") == "closed":
        return None
    if not M.families_in_title(title):          # not a software-ish role
        return None
    if _NEVER.search(title):
        return None
    if level == "intern" and M.MID_LEVEL_RE.search(title):
        return None
    url = p.get("source_url") or ""
    key = url or f"{company}|{title}|{p.get('location')}"
    return {
        "id": "dl-" + hashlib.sha1(key.encode()).hexdigest()[:16],
        "source": "datalayer",
        "company": company or "",
        "title": title,
        "location": p.get("location") or "",
        "url": url,
        "posted_at": p.get("posted_at"),
        "term": None,
        "description": p.get("description"),
        "salary": p.get("salary"),
        "level": level,
        "countries": [cc.lower()],
    }


async def _call(session, sem, body: dict) -> list[tuple[str | None, dict]]:
    headers = {"Authorization": f"Bearer {os.environ['DATALAYER_API_KEY']}",
               "Content-Type": "application/json"}
    async with sem:
        try:
            async with session.post(URL, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    err = (data or {}).get("error") or {}
                    log.warning("datalayer %s: %s %s", r.status, err.get("code"),
                                err.get("message"))
                    return []
        except Exception as e:
            log.warning("datalayer call failed: %s", e)
            return []
    meta = data.get("meta") or {}
    log.info("datalayer %r %s/%s: %s credit(s)", body.get("query"),
             body.get("seniority"), body.get("country"), meta.get("credits_used"))
    return [(c.get("company_name"), p)
            for c in (data.get("data") or {}).get("companies") or []
            for p in c.get("postings") or []]


async def search(q: dict | None, levels: list[str], countries: list[str],
                 days: int = 30) -> list[dict]:
    """Live roles for this query, level(s) and country(ies), in feed format.

    One call per (search word x level x country). Never raises; returns []
    without a key or when every call fails."""
    if not enabled():
        return []
    days = max(1, min(int(days), 90))
    combos = [(t, lv, COUNTRY_CODE[c]) for t in terms_for(q)
              for lv in levels if lv in LEVEL_SENIORITY
              for c in countries if c in COUNTRY_CODE]
    if not combos:
        return []
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as s:
        pages = await asyncio.gather(*[
            _call(s, sem, {"query": t, "seniority": LEVEL_SENIORITY[lv],
                           "country": cc, "posted_within_days": days,
                           "limit": PAGE, "max_credits": 1})
            for t, lv, cc in combos])
    out, seen = [], set()
    for (t, lv, cc), rows in zip(combos, pages):
        for company, p in rows:
            j = _job(company, p, lv, cc)
            if not j:
                continue
            k = (j["company"].lower(), j["title"].lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(j)
    return out


def _url_key(u: str | None) -> str:
    return (u or "").split("?")[0].rstrip("/").lower()


def merge(feed: list[dict], live: list[dict]) -> tuple[list[dict], int]:
    """Feed plus the live roles it doesn't already have. Returns (merged,
    number of live roles added). The feed's copy wins: it carries `term` and
    the collector's own level tag."""
    urls = {_url_key(j.get("url")) for j in feed if j.get("url")}
    names = {((j.get("company") or "").lower(), (j.get("title") or "").lower())
             for j in feed}
    extra = [j for j in live
             if _url_key(j.get("url")) not in urls
             and (j["company"].lower(), j["title"].lower()) not in names]
    return feed + extra, len(extra)
