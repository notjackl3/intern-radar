"""Turn the firehose into the handful of roles actually worth your evening.

The hard part here is deciding what counts as Canadian. Naive city-name
matching does not work: Burlington, London, Hamilton, Windsor, Victoria,
Waterloo, Kingston and Richmond all exist on both sides of the border, and a
US listing like "Palo Alto, CA; Burlington, MA" will happily match a keyword
list. So the rule is: a location is Canadian only if it carries a Canadian
QUALIFIER — the word Canada, a province name, or a province code in the
`City, XX` position — or names a city that exists nowhere else.
"""
from __future__ import annotations

import re

# Province codes as they appear in a listing: after a comma, or at the end.
# Matched case-sensitively on the uppercase form to avoid firing on prose
# ("on site", "ab initio", "PE ratio").
PROV_CODES = r"(?:ON|BC|AB|QC|MB|SK|NS|NB|NL|PE|PEI|YT|NT|NU)"
PROV_PATTERN = re.compile(rf",\s*{PROV_CODES}\b")

PROVINCES = [
    "ontario", "british columbia", "alberta", "quebec", "québec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon", "nunavut", "northwest territories",
]

# Cities that are unambiguously Canadian — safe to match on their own, for
# sources that write a bare city with no province.
UNAMBIGUOUS = [
    "toronto", "montreal", "montréal", "vancouver", "ottawa", "calgary",
    "edmonton", "mississauga", "markham", "brampton", "vaughan", "oakville",
    "burnaby", "kitchener", "saskatoon", "winnipeg", "halifax", "moncton",
    "fredericton", "gatineau", "sherbrooke", "laval", "regina", "scarborough",
    "etobicoke", "north york", "kanata", "oshawa", "barrie", "guelph",
    "st. john's", "trois-rivières", "chicoutimi", "lethbridge", "nanaimo",
    "coquitlam", "surrey, bc", "richmond hill", "newmarket, on", "milton, on",
]

# Cities that exist in both countries. These are deliberately NOT in the list
# above; they only count when a province qualifier is also present, which the
# PROV_PATTERN / "canada" checks already handle:
#   London, Hamilton, Windsor, Victoria, Waterloo, Kingston, Cambridge,
#   Richmond, Burlington, Sarnia, Chatham, Stratford, Peterborough, Woodstock.

# Excluded first: a "Senior Manager, Intern Programs" is not an internship.
EXCLUDE_TITLE = [
    "senior", "sr.", "staff", "principal", "lead ", "manager", "director",
    "head of", "vp ", "vice president", "architect", "phd required",
    "postdoc", "faculty", "professor", "recruiter",
]


def _is_canadian_part(part: str) -> bool:
    low = part.lower()
    if "canada" in low:
        return True
    if any(p in low for p in PROVINCES):
        return True
    if PROV_PATTERN.search(part):
        return True
    return any(c in low for c in UNAMBIGUOUS)


def is_canadian(loc: str) -> bool:
    """True if ANY location in a multi-location listing is Canadian.

    Locations arrive as "Toronto, ON, Canada" or, for multi-site reqs, as a
    ';'-joined list. Each part is judged on its own so that one Canadian site
    in a list of eight US ones still counts — but a US-only list never does.
    """
    if not loc:
        return False
    parts = [p.strip() for p in re.split(r"[;|]", loc) if p.strip()]
    return any(_is_canadian_part(p) for p in (parts or [loc]))


def title_matches(title: str, include: list[str]) -> bool:
    low = (title or "").lower()
    if any(x in low for x in EXCLUDE_TITLE):
        return False
    return any(x in low for x in include)


def term_matches(job: dict, wanted: list[str]) -> bool:
    """Sources that carry a term (Simplify) are filtered on it. Sources that
    don't (every raw ATS endpoint) fall back to the title, then pass through —
    an unlabelled intern req in Canada is worth seeing either way."""
    if not wanted:
        return True
    hay = f"{job.get('term') or ''} {job.get('title') or ''}".lower()
    if job.get("term"):
        return any(w.lower() in hay for w in wanted)
    # No term metadata: only reject if the title explicitly names a term we
    # did not ask for.
    if re.search(r"(summer|winter|fall|spring)\s*20\d\d", hay):
        return any(w.lower() in hay for w in wanted)
    return True


def domain_matches(job: dict, include: list[str], exclude: list[str]) -> bool:
    """Is this a SOFTWARE-ish role? ANDed with the intern-level filter.

    Without this, "intern" alone lets through capital markets, internal audit,
    HR, marketing and mechanical engineering — on the first real run that was
    172 of 271 roles.

    The title decides a rejection (a "Sales Systems & Data Governance Intern"
    is a sales job however much it says "data"), but a match may come from the
    description too, since Workday and the Simplify sweep publish no
    description and their titles are often vague.
    """
    if not include:
        return True
    title = (job.get("title") or "").lower()
    if any(x in title for x in exclude):
        return False
    hay = f"{title} {(job.get('description') or '')[:400].lower()}"
    return any(x in hay for x in include)


def apply_filters(jobs, cfg) -> list[dict]:
    include = [x.lower() for x in cfg.get("title_include_any", [])]
    dom_in = [x.lower() for x in cfg.get("domain_include_any", [])]
    dom_out = [x.lower() for x in cfg.get("domain_exclude_any", [])]
    terms = cfg.get("terms", [])
    canada_only = cfg.get("canada_only", True)

    out, seen_urls = [], set()
    for j in jobs:
        if canada_only and not is_canadian(j.get("location", "")):
            continue
        if not title_matches(j.get("title", ""), include):
            continue
        if not domain_matches(j, dom_in, dom_out):
            continue
        if not term_matches(j, terms):
            continue
        url = (j.get("url") or "").split("?")[0]
        if url in seen_urls:  # same req surfaced by two sources
            continue
        seen_urls.add(url)
        out.append(j)
    return out
