"""Turn "Cohere", or a careers URL, into a watchlist entry — and prove it works.

This is what `/watch` runs on. It is deliberately NOT an LLM. The job is
identifier resolution, and the four applicant tracking systems we poll are
self-describing: a board URL contains the token, and the API tells you plainly
whether that token is real.

The thing that makes this safe is a quirk worth stating up front:

    a WRONG identifier 404s on all four systems.
    a RIGHT identifier that happens to have no open roles returns 200 and [].

So a 200 is proof the board exists, and only a 200 may be written to the
watchlist. That distinction is the whole ballgame — it is the difference
between subscribing to a real employer and adding a line that silently polls
nothing forever. `EMPTY` is therefore reported as a real, acceptable outcome
("this board is quiet right now") rather than as a failure.

Guessing from a name is best-effort and always ends in a human confirming a
sample of real job titles. Workday is not guessable at all: tenant, shard and
site are three independent unknowns, so /watch asks for the URL.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# What a resolution looks like
# --------------------------------------------------------------------------
OK, EMPTY, NOT_FOUND, UNSUPPORTED = "ok", "empty", "not_found", "unsupported"


@dataclass
class Resolution:
    status: str                       # OK | EMPTY | NOT_FOUND | UNSUPPORTED
    entry: dict | None = None         # the watchlist line, ready to write
    ats: str = ""
    total: int = 0                    # roles on the whole board
    sample: list[dict] = field(default_factory=list)   # [{title, location}]
    canadian: int = 0                 # of the sample, how many look Canadian
    evidence: str = ""                # the API URL actually called
    note: str = ""

    @property
    def writable(self) -> bool:
        return self.status in (OK, EMPTY) and self.entry is not None


# --------------------------------------------------------------------------
# URL -> entry.  Every board URL carries its own identifier.
# --------------------------------------------------------------------------
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")

# Greenhouse's embed board is disallowed by robots.txt and "embed" is not a
# real employer token — someone pasting that URL means the `for=` parameter.
_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io",
             "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
_LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
_ASHBY_HOSTS = {"jobs.ashbyhq.com"}

# Recognised, but we do not poll them. Naming them explicitly is the point:
# "I know exactly what this is and why it isn't supported" is a far better
# answer than "couldn't find that company".
UNPOLLABLE = {
    "careers.smartrecruiters.com": (
        "SmartRecruiters — the API works, but api.smartrecruiters.com serves a "
        "blanket-disallow robots.txt, so this radar doesn't poll it by choice."),
    "jobs.smartrecruiters.com": ("SmartRecruiters — see above."),
    "career.successfactors.com": "SAP SuccessFactors — no public JSON API.",
    "jobs.sap.com": "SAP SuccessFactors — no public JSON API.",
    "careers.oracle.com": "Oracle Recruiting — no public JSON API.",
    "taleo.net": "Taleo — no public JSON API.",
    "avature.net": "Avature — no public JSON API.",
    "icims.com": "iCIMS — no public JSON API.",
    "phenompeople.com": "Phenom — no public JSON API.",
    "myworkdaysite.com": (
        "This is a Workday *site* URL, which doesn't expose the tenant. Open a "
        "job posting and copy the `*.myworkdayjobs.com` URL instead."),
}


def parse_url(text: str) -> Resolution | None:
    """Recognise a careers URL. Returns None if it isn't a URL at all."""
    text = text.strip()
    if not re.match(r"^(https?://|www\.)", text, re.I):
        if "." not in text.split()[0] or " " in text.strip():
            return None
        text = "https://" + text
    if not text.lower().startswith("http"):
        text = "https://" + text

    u = urlparse(text)
    host = (u.netloc or "").lower().split(":")[0]
    parts = [p for p in u.path.split("/") if p]

    for bad_host, why in UNPOLLABLE.items():
        if host == bad_host or host.endswith("." + bad_host) or bad_host in host:
            return Resolution(status=UNSUPPORTED, note=why)

    if host in _GH_HOSTS:
        token = parts[0] if parts else ""
        if token in ("embed", "job_board"):
            token = (parse_qs(u.query).get("for") or [""])[0]
        if token:
            return Resolution(status=OK, ats="greenhouse",
                              entry={"ats": "greenhouse", "token": token})

    if host in _LEVER_HOSTS and parts:
        return Resolution(status=OK, ats="lever",
                          entry={"ats": "lever", "token": parts[0]})

    if host in _ASHBY_HOSTS and parts:
        return Resolution(status=OK, ats="ashby",
                          entry={"ats": "ashby", "token": parts[0]})

    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m and parts:
        tenant, shard = m.group(1), m.group(2)
        # .../en-US/SiteName/job/...  — the locale segment is optional.
        segs = [p for p in parts if not _LOCALE.match(p)]
        # Drop everything from /job/ onward; the site is the segment before it.
        if "job" in segs:
            segs = segs[:segs.index("job")]
        if segs:
            return Resolution(status=OK, ats="workday",
                              entry={"ats": "workday", "tenant": tenant,
                                     "shard": shard, "site": segs[0]})
        return Resolution(status=UNSUPPORTED, note=(
            f"That's Workday tenant `{tenant}` on `{shard}`, but the URL has no "
            "site segment. Open an actual job posting and paste that URL."))

    return Resolution(status=UNSUPPORTED, note=(
        f"`{host}` isn't one of the four systems this radar can poll "
        "(Greenhouse, Lever, Ashby, Workday). If the employer has a board on "
        "one of those, paste that URL instead."))


# --------------------------------------------------------------------------
# Name -> candidate tokens.  Guessing, clearly labelled as guessing.
# --------------------------------------------------------------------------
_STRIP_WORDS = ("inc", "incorporated", "corp", "corporation", "ltd", "limited",
                "llc", "co", "company", "technologies", "technology", "labs",
                "software", "systems", "group", "the")


def _ascii(s: str) -> str:
    """Arc'teryx -> arcteryx, Mirego -> mirego, Déjà -> deja."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def slug_candidates(name: str) -> list[str]:
    base = _ascii(name).lower().strip()
    words = [w for w in re.split(r"[^a-z0-9]+", base) if w]
    if not words:
        return []
    trimmed = [w for w in words if w not in _STRIP_WORDS] or words

    out = [
        "".join(words),          # stackadapt
        "".join(trimmed),        # stackadapt (minus Inc/Ltd)
        "-".join(trimmed),       # top-hat
        trimmed[0],              # cohere      (first word alone)
        "".join(trimmed) + "inc",
        "".join(trimmed) + "hq",
    ]
    # ".com" really is part of some Lever tokens (arcteryx.com).
    out.append("".join(trimmed) + ".com")
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# --------------------------------------------------------------------------
# Probing.  `fetch` is injected so this module stays testable without network.
# --------------------------------------------------------------------------
GH = "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"
LEVER = "https://api.lever.co/v0/postings/{t}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=false"
WORKDAY = "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

# Advisory only — it tells the human "12 of these 40 look Canadian" so they can
# judge whether the board is worth watching. collector/filters.py holds the
# real filter; this one is allowed to be rough and must never gate a write.
_CA = re.compile(
    r"canada|canadian|, ?(ON|BC|AB|QC|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b"
    r"|ontario|british columbia|alberta|quebec|québec|manitoba|saskatchewan"
    r"|nova scotia|toronto|vancouver|montr|ottawa|waterloo|calgary|edmonton"
    r"|mississauga|kitchener|halifax|winnipeg|victoria|burnaby|markham", re.I)


def looks_canadian(loc: str | None) -> bool:
    return bool(loc and _CA.search(loc))


def _normalise(ats: str, data) -> list[dict]:
    """Whatever the board returned -> [{title, location}]."""
    if ats == "greenhouse":
        return [{"title": j.get("title", ""),
                 "location": (j.get("location") or {}).get("name", "")}
                for j in (data or {}).get("jobs", [])]
    if ats == "lever":
        return [{"title": j.get("text", ""),
                 "location": (j.get("categories") or {}).get("location", "")}
                for j in (data or [])]
    if ats == "ashby":
        return [{"title": j.get("title", ""), "location": j.get("location", "")}
                for j in (data or {}).get("jobs", [])]
    if ats == "workday":
        return [{"title": j.get("title", ""), "location": j.get("locationsText", "")}
                for j in (data or {}).get("jobPostings", [])]
    return []


def api_url(entry: dict) -> str:
    if entry["ats"] == "workday":
        return WORKDAY.format(**entry)
    return {"greenhouse": GH, "lever": LEVER, "ashby": ASHBY}[entry["ats"]].format(
        t=entry["token"])


async def probe(fetch, entry: dict) -> Resolution:
    """`fetch(url, method, json) -> (status_code, parsed_body)`.

    A 404 means the identifier is wrong. A 200 with no jobs means the board is
    real and quiet — a legitimate thing to subscribe to, and NOT an error.
    """
    url = api_url(entry)
    payload = ({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
               if entry["ats"] == "workday" else None)
    code, data = await fetch(url, "POST" if payload else "GET", payload)

    if code == 404 or code == 410:
        return Resolution(status=NOT_FOUND, ats=entry["ats"], evidence=url,
                          note=f"`{url}` returned {code} — no such board.")
    if code != 200:
        return Resolution(status=NOT_FOUND, ats=entry["ats"], evidence=url,
                          note=f"`{url}` returned HTTP {code}.")

    jobs = _normalise(entry["ats"], data)
    total = int((data or {}).get("total") or 0) if entry["ats"] == "workday" else len(jobs)
    ca = sum(1 for j in jobs if looks_canadian(j.get("location")))
    return Resolution(
        status=OK if jobs else EMPTY, entry=entry, ats=entry["ats"],
        total=max(total, len(jobs)), sample=jobs[:6], canadian=ca, evidence=url,
        note="" if jobs else
             "The board exists but has no open roles right now. That is fine to "
             "watch — you'll be told the moment it posts one.")


async def resolve(fetch, text: str, guess_limit: int = 6) -> Resolution:
    """A URL resolves exactly. A name is guessed, then proven or rejected."""
    parsed = parse_url(text)
    if parsed and parsed.status == UNSUPPORTED:
        return parsed
    if parsed and parsed.entry:
        return await probe(fetch, parsed.entry)

    tried = []
    for token in slug_candidates(text)[:guess_limit]:
        for ats in ("greenhouse", "lever", "ashby"):
            r = await probe(fetch, {"ats": ats, "token": token})
            tried.append(f"{ats}/{token}")
            if r.status in (OK, EMPTY):
                r.note = (f"Guessed from the name — **confirm the roles below are "
                          f"really {text}** before subscribing.\n" + r.note).strip()
                return r
    return Resolution(status=NOT_FOUND, note=(
        f"No Greenhouse, Lever or Ashby board answered for **{text}**.\n"
        "Tokens can't be guessed reliably, and Workday needs its URL regardless "
        "(the tenant, shard and site are three separate unknowns). Open the "
        "employer's careers page, click any job, and paste that URL.\n"
        f"_Tried {len(tried)} identifiers._"))
